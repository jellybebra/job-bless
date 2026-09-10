"""Single-owner password login with expiring sessions and CSRF protection."""

import asyncio
import hmac
import secrets
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from src.passwords import hash_password, valid_password_hash, verify_password

COOKIE = "job_bless_session"
LOGIN_COOKIE = "job_bless_login_csrf"
SESSION_SECONDS = 12 * 60 * 60


@dataclass
class OwnerSession:
    id: str
    csrf: str
    expires: float


class OwnerAuth:
    def __init__(self, config):
        self.config = config
        self.enabled = bool(config.password_hash or config.token)
        if config.password_hash and not valid_password_hash(config.password_hash):
            raise ValueError("WEB_PASSWORD_HASH has an invalid format; run scripts/configure_server.py")
        if config.require_auth and not self.enabled:
            raise ValueError("Server mode requires WEB_PASSWORD_HASH; run scripts/configure_server.py")
        if config.public_url:
            parsed = urlsplit(config.public_url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                    or parsed.path or parsed.query or parsed.fragment):
                raise ValueError("WEB_PUBLIC_URL must be an origin, e.g. https://jobs.example.com")
        self.sessions = {}
        self.attempts = {}
        self.login_gate = asyncio.Semaphore(2)

    @property
    def secure(self):
        return self.config.secure_cookies or self.config.public_url.startswith("https://")

    def session(self, connection):
        token = connection.cookies.get(COOKIE, "")
        session = self.sessions.get(token)
        if session and session.expires > time.time():
            return session
        self.sessions.pop(token, None)
        return None

    def same_origin(self, connection):
        expected = self.config.public_url or f"{connection.url.scheme.replace('ws', 'http')}://{connection.headers.get('host', '')}"
        origin = connection.headers.get("origin", "")
        if not origin:
            referer = urlsplit(connection.headers.get("referer", ""))
            origin = f"{referer.scheme}://{referer.netloc}" if referer.netloc else ""
        return bool(origin) and hmac.compare_digest(origin.lower().encode(), expected.lower().encode())

    def create_session(self):
        now = time.time()
        self.sessions = {key: item for key, item in self.sessions.items() if item.expires > now}
        # A personal installation needs few devices; bound memory and revoke the oldest.
        if len(self.sessions) >= 32:
            self.sessions.pop(next(iter(self.sessions)))
        session = OwnerSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now + SESSION_SECONDS)
        self.sessions[session.id] = session
        return session

    async def csrf_matches(self, request, expected):
        provided = request.headers.get("x-csrf-token", "")
        if not provided:
            # Cache the body before parsing so downstream FastAPI forms can read it too.
            if len(await request.body()) > 2 * 1024 * 1024:
                return False
            form = await request.form()
            provided = str(form.get("csrf_token", ""))
        return bool(expected and provided) and hmac.compare_digest(provided.encode(), expected.encode())

    async def guard(self, request: Request, call_next):
        # A native launcher gives the owner a private link. Exchange it for the
        # same expiring, CSRF-protected session used by password login, then strip
        # the token from the URL. Never accept it as authority for a mutation.
        bootstrap = request.query_params.get("token", "")
        if (request.method == "GET" and self.config.token and bootstrap
                and hmac.compare_digest(bootstrap.encode(), self.config.token.encode())):
            session = self.create_session()
            response = RedirectResponse(str(request.url.remove_query_params("token")), status_code=303)
            response.set_cookie(COOKIE, session.id, max_age=SESSION_SECONDS, httponly=True,
                                secure=self.secure, samesite="strict")
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
        request.state.owner_session = self.session(request)
        request.state.csrf_token = request.state.owner_session.csrf if request.state.owner_session else ""
        if not self.enabled:
            return await call_next(request)
        public = request.url.path in ("/login", "/healthz") or request.url.path.startswith("/static/")
        if not public and not request.state.owner_session:
            if request.headers.get("hx-request"):
                return HTMLResponse(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            expected = (request.cookies.get(LOGIN_COOKIE, "") if request.url.path == "/login"
                        else request.state.csrf_token)
            if not self.same_origin(request) or not await self.csrf_matches(request, expected):
                return JSONResponse({"detail": "Обновите страницу и повторите действие."}, status_code=403)
        response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response


router = APIRouter()


def login_page(request, *, error="", status_code=200):
    # Another tab (or an unauthenticated resource redirected to /login) must not
    # invalidate a form that is already open in this browser.
    nonce = request.cookies.get(LOGIN_COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce):
        nonce = secrets.token_urlsafe(32)
    response = request.app.state.templates.TemplateResponse(
        request, "login.html", {"csrf_token": nonce, "error": error}, status_code=status_code,
    )
    response.set_cookie(LOGIN_COOKIE, nonce, max_age=1800, httponly=True,
                        secure=request.app.state.auth.secure, samesite="strict")
    return response


@router.get("/login")
async def login_form(request: Request):
    auth = request.app.state.auth
    if not auth.enabled or auth.session(request):
        return RedirectResponse("/actions", status_code=303)
    return login_page(request)


@router.post("/login")
async def login(request: Request):
    auth = request.app.state.auth
    if not auth.enabled:
        return RedirectResponse("/actions", status_code=303)
    now = time.monotonic()
    auth.attempts = {key: value for key, value in auth.attempts.items() if value[1] > now}
    # Do not trust arbitrary forwarding headers; uvicorn resolves trusted proxies.
    ip = request.client.host if request.client else "unknown"
    attempts, deadline = auth.attempts.get(ip, (0, now + 900))
    if attempts >= 5 or len(auth.attempts) >= 1024:
        return login_page(request, error="Слишком много попыток. Повторите через 15 минут.", status_code=429)
    # Reserve the attempt before awaiting expensive password verification.
    auth.attempts[ip] = (attempts + 1, deadline)
    form = await request.form()
    async with auth.login_gate:
        valid = await asyncio.to_thread(verify_password, str(form.get("password", "")), auth.config.password_hash)
    if not valid:
        return login_page(request, error="Неверный пароль.", status_code=401)
    auth.attempts.pop(ip, None)
    old = auth.session(request)
    if old:
        auth.sessions.pop(old.id, None)
    session = auth.create_session()
    response = RedirectResponse("/actions", status_code=303)
    response.set_cookie(COOKIE, session.id, max_age=SESSION_SECONDS, httponly=True,
                        secure=auth.secure, samesite="strict")
    response.delete_cookie(LOGIN_COOKIE)
    return response


@router.post("/logout")
async def logout(request: Request):
    auth = request.app.state.auth
    session = auth.session(request)
    if session:
        auth.sessions.pop(session.id, None)
        if hasattr(request.app.state, "screens"):
            await request.app.state.screens.revoke_owner(session.id)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response
