"""Server-side Clerk authentication. No identity is accepted from browser headers."""

import asyncio
import base64
import re
import time
from dataclasses import dataclass

import httpx
import jwt


class AuthenticationError(Exception):
    pass


def frontend_host(key: str) -> str:
    if not re.fullmatch(r"pk_(test|live)_[A-Za-z0-9_=\-]+", key):
        raise ValueError("Invalid CLERK_PUBLISHABLE_KEY")
    encoded = key.split("_", 2)[2]
    decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    host = decoded.removesuffix("$")
    if not decoded.endswith("$") or not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or "." not in host:
        raise ValueError("Invalid Clerk frontend host")
    return host


@dataclass(frozen=True)
class Identity:
    user_id: str
    session_id: str
    email: str
    verified_emails: tuple[str, ...]


class ClerkAuth:
    def __init__(self, publishable_key, secret_key, public_url, *, client=None):
        self.host = frontend_host(publishable_key)
        self.issuer = f"https://{self.host}"
        self.secret_key = secret_key
        self.public_url = public_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=10, trust_env=False)
        self.keys = {}
        self.keys_at = 0
        self.key_lock = asyncio.Lock()
        self.sessions = {}
        self.users = {}

    async def _get(self, path):
        response = await self.client.get("https://api.clerk.com/v1" + path,
                                         headers={"Authorization": f"Bearer {self.secret_key}"})
        if response.status_code in (401, 403, 404):
            raise AuthenticationError("Clerk rejected the session")
        response.raise_for_status()
        return response.json()

    async def _key(self, kid):
        async with self.key_lock:
            now = time.monotonic()
            if now - self.keys_at > 300 or (kid not in self.keys and now - self.keys_at > 5):
                response = await self.client.get(self.issuer + "/.well-known/jwks.json")
                response.raise_for_status()
                self.keys = {key["kid"]: jwt.PyJWK.from_dict(key).key
                             for key in response.json()["keys"] if key.get("alg", "RS256") == "RS256"}
                self.keys_at = now
            if kid not in self.keys:
                raise AuthenticationError("Unknown signing key")
            return self.keys[kid]

    async def active_session(self, user_id, session_id):
        """Also bounds revocation latency for long-lived SSE/VNC connections."""
        now = time.monotonic()
        cached = self.sessions.get(session_id)
        if not cached or now - cached[0] > 15:
            status = await self._get(f"/sessions/{session_id}")
            cached = (now, status)
            self.sessions[session_id] = cached
            if len(self.sessions) > 2048:
                self.sessions = {key: value for key, value in self.sessions.items() if now - value[0] < 30}
        status = cached[1]
        if status.get("status") != "active" or status.get("user_id") != user_id:
            raise AuthenticationError("Session is no longer active")

    async def authenticate(self, connection):
        authorization = connection.headers.get("authorization", "")
        token = (authorization[7:] if authorization.startswith("Bearer ")
                 else connection.cookies.get("__session", ""))
        if not token or len(token) > 16384:
            raise AuthenticationError("Sign in required")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise AuthenticationError("Unexpected signing algorithm")
            claims = jwt.decode(token, await self._key(header.get("kid")), algorithms=["RS256"],
                                issuer=self.issuer, leeway=5,
                                options={"require": ["exp", "nbf", "iat", "sub", "sid", "iss"],
                                         "verify_aud": False})
            user_id, session_id = claims["sub"], claims["sid"]
            if (not re.fullmatch(r"user_[A-Za-z0-9]+", user_id)
                    or not re.fullmatch(r"sess_[A-Za-z0-9]+", session_id)
                    or claims.get("sts") == "pending"
                    or claims.get("azp", self.public_url) != self.public_url):
                raise AuthenticationError("Invalid session claims")
        except (jwt.PyJWTError, TypeError, KeyError) as error:
            raise AuthenticationError("Invalid session token") from error
        await self.active_session(user_id, session_id)
        now = time.monotonic()
        cached = self.users.get(user_id)
        if not cached or now - cached[0] > 60:
            cached = (now, await self._get(f"/users/{user_id}"))
            self.users[user_id] = cached
            if len(self.users) > 2048:
                self.users = {key: value for key, value in self.users.items() if now - value[0] < 120}
        user = cached[1]
        if user.get("banned") or user.get("locked"):
            raise AuthenticationError("Account is unavailable")
        addresses = user.get("email_addresses", [])
        verified = tuple(item["email_address"].lower() for item in addresses
                         if item.get("verification", {}).get("status") == "verified")
        primary = next((item["email_address"] for item in addresses
                        if item["id"] == user.get("primary_email_address_id")), "")
        return Identity(user_id, session_id, primary, verified)

    async def close(self):
        await self.client.aclose()

    async def user_enabled(self, user_id):
        """Deleted/disabled accounts must not keep running unattended schedules."""
        response = await self.client.get(f"https://api.clerk.com/v1/users/{user_id}",
                                         headers={"Authorization": f"Bearer {self.secret_key}"})
        if response.status_code == 404:
            return False
        response.raise_for_status()
        user = response.json()
        return not (user.get("banned") or user.get("locked"))
