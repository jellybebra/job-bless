"""A private worker only accepts requests authenticated by its own gateway."""

import hashlib
import hmac
import time

from fastapi.responses import JSONResponse

from src.web.auth import OwnerAuth, OwnerSession


class WorkspaceAuth(OwnerAuth):
    def __init__(self, config):
        self.config = config
        self.enabled = True
        self.sessions = {}
        self.secret = config.workspace_token
        if len(self.secret) < 32 or not config.workspace_user_id or not config.public_url:
            raise ValueError("Workspace authentication requires a private token, user ID and public URL")

    def trusted(self, connection):
        provided = connection.headers.get("x-workspace-token", "")
        return hmac.compare_digest(provided.encode(), self.secret.encode())

    def session(self, connection):
        if not self.trusted(connection):
            return None
        if connection.headers.get("x-workspace-user") != self.config.workspace_user_id:
            return None
        sid = connection.headers.get("x-workspace-session", "")
        if not sid or len(sid) > 128:
            return None
        # Stable identity and CSRF across refreshed Clerk tokens and worker restarts.
        session = self.sessions.get(sid)
        if session is None:
            csrf = hmac.new(self.secret.encode(), sid.encode(), hashlib.sha256).hexdigest()
            session = OwnerSession(sid, csrf, time.time() + 86400)
            self.sessions[sid] = session
        session.expires = time.time() + 86400
        return session

    async def guard(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if request.url.path in ("/internal/idle-status", "/internal/revoke-session") and self.trusted(request):
            return await call_next(request)
        session = self.session(request)
        if not session:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        request.state.owner_session = session
        request.state.csrf_token = session.csrf
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not self.same_origin(request) or not await self.csrf_matches(request, session.csrf):
                return JSONResponse({"detail": "Обновите страницу и повторите действие."}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "same-origin", "X-Frame-Options": "SAMEORIGIN"})
        return response
