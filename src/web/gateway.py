"""Public Clerk entry point; all application routes belong to private workers."""

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.web.clerk_auth import AuthenticationError, ClerkAuth
from src.web.workspaces import WorkspacePool

logger = logging.getLogger(__name__)
HOP_HEADERS = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "authorization", "cookie"}


def worker_headers(headers, identity, workspace):
    connection_tokens = {token.strip().lower() for token in headers.get("connection", "").split(",")}
    result = {key: value for key, value in headers.items()
              if key.lower() not in HOP_HEADERS | connection_tokens
              and not key.lower().startswith(("x-workspace-", "x-forwarded-", "sec-websocket-"))}
    result.update({"x-workspace-token": workspace.token, "x-workspace-user": identity.user_id,
                   "x-workspace-session": identity.session_id})
    return result


def create_gateway(*, auth=None, pool=None, public_url=None, publishable_key=None, transport=None):
    public_url = public_url or os.environ["WEB_PUBLIC_URL"]
    publishable_key = publishable_key or os.environ["CLERK_PUBLISHABLE_KEY"]
    auth = auth or ClerkAuth(publishable_key, os.environ["CLERK_SECRET_KEY"], public_url)
    pool = pool or WorkspacePool(
        root=os.environ.get("WORKSPACES_ROOT", "/workspaces"), host_root=os.environ["WORKSPACES_HOST_ROOT"],
        image_prefix=os.environ.get("JOB_BLESS_IMAGE_PREFIX", "ghcr.io/jellybebra/job-bless"),
        image_tag=os.environ["JOB_BLESS_IMAGE_TAG"], gateway_container=os.environ.get("GATEWAY_CONTAINER", "job-bless-gateway"),
        public_url=public_url, publishable_key=publishable_key, owner_email=os.environ.get("CLERK_OWNER_EMAIL", ""))
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, read=None), trust_env=False, transport=transport)
    watched_sessions = {}

    async def housekeeping():
        last_reap = time.monotonic()
        while True:
            await asyncio.sleep(15)
            for sid, (identity, workspace, until) in list(watched_sessions.items()):
                if until < time.monotonic():
                    watched_sessions.pop(sid, None)
                    continue
                try:
                    await auth.active_session(identity.user_id, sid)
                except AuthenticationError:
                    try:
                        response = await client.post(workspace.url + "/internal/revoke-session",
                                                     headers={"X-Workspace-Token": workspace.token},
                                                     json={"session_id": sid}, timeout=10)
                        response.raise_for_status()
                        watched_sessions.pop(sid, None)
                    except httpx.HTTPError:
                        pass
                except httpx.HTTPError:
                    pass
            if time.monotonic() - last_reap < 60:
                continue
            try:
                for workspace in await pool.running():
                    if not await auth.user_enabled(workspace.user_id):
                        await pool.deactivate(workspace)
                await pool.reap_idle()
            except Exception:
                logger.exception("Workspace maintenance failed")
            last_reap = time.monotonic()

    @asynccontextmanager
    async def lifespan(app):
        recovery = asyncio.create_task(pool.restore())
        maintenance = asyncio.create_task(housekeeping())
        yield
        recovery.cancel()
        maintenance.cancel()
        await asyncio.gather(maintenance, recovery, return_exceptions=True)
        await client.aclose()
        await auth.close()
        pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth, app.state.pool = auth, pool
    base = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    templates = Jinja2Templates(directory=base / "templates")
    templates.env.globals.update(clerk_publishable_key=publishable_key, clerk_host=auth.host, static_version="clerk-1")

    @app.get("/healthz")
    async def health():
        return {"service": "job-bless-gateway", "status": "ok"}

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @app.get("/login", response_class=HTMLResponse)
    @app.get("/signup", response_class=HTMLResponse)
    async def login(request: Request):
        return templates.TemplateResponse(request, "clerk_login.html", {},
                                           headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})

    @app.post("/internal/wake/{provider}")
    async def wake(provider: str, request: Request):
        try:
            await pool.wake(request.headers.get("x-workspace-token", ""), provider)
        except PermissionError:
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        except ValueError:
            return JSONResponse({"detail": "Unknown browser"}, status_code=404)
        except Exception:
            logger.exception("Browser startup failed")
            return JSONResponse({"detail": "Браузер временно недоступен"}, status_code=503)
        return {"ready": True}

    async def identity_for(connection):
        return await auth.authenticate(connection)

    async def monitor(identity, close):
        while True:
            await asyncio.sleep(15)
            try:
                await auth.active_session(identity.user_id, identity.session_id)
            except Exception:
                await close()
                return

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy(request: Request, path: str):
        if path.startswith("internal/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        try:
            identity = await identity_for(request)
        except AuthenticationError:
            if request.headers.get("hx-request"):
                return HTMLResponse(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        except httpx.HTTPError:
            return HTMLResponse("Сервис входа временно недоступен. Попробуйте позже.", status_code=503)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("origin") != public_url:
            return JSONResponse({"detail": "Invalid origin"}, status_code=403)
        if path == "logout":
            return RedirectResponse("/login", status_code=303)
        try:
            workspace = await pool.ensure(identity)
            watched_sessions[identity.session_id] = (identity, workspace, time.monotonic() + 1200)
            url = workspace.url + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            upstream = client.build_request(request.method, url,
                                            headers=worker_headers(request.headers, identity, workspace),
                                            content=request.stream())
            response = await client.send(upstream, stream=True)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=403)
        except Exception:
            logger.exception("Workspace is unavailable")
            return HTMLResponse("Личная рабочая среда запускается. Обновите страницу через минуту.", status_code=503,
                                headers={"Retry-After": "30"})

        async def stream():
            guard = asyncio.create_task(monitor(identity, response.aclose))
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            except httpx.HTTPError:
                pass
            finally:
                guard.cancel()
                await asyncio.gather(guard, return_exceptions=True)
                await response.aclose()
        result = StreamingResponse(stream(), status_code=response.status_code)
        result.raw_headers = [(key, value) for key, value in response.headers.raw
                              if key.decode().lower() not in HOP_HEADERS]
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.websocket("/{path:path}")
    async def socket(websocket: WebSocket, path: str):
        if websocket.headers.get("origin") != public_url or not path.startswith("accounts/") or not path.endswith("/ws"):
            await websocket.close(code=1008)
            return
        try:
            identity = await identity_for(websocket)
            workspace = await pool.ensure(identity)
            watched_sessions[identity.session_id] = (identity, workspace, time.monotonic() + 1200)
            url = workspace.url.replace("http://", "ws://") + websocket.url.path
            headers = worker_headers(websocket.headers, identity, workspace)
            headers.pop("origin", None)
            async with websockets.connect(url, additional_headers=headers, origin=public_url,
                                          proxy=None, max_size=2 * 1024 * 1024) as upstream:
                await websocket.accept()

                async def to_worker():
                    while True:
                        event = await websocket.receive()
                        if event["type"] == "websocket.disconnect":
                            return
                        await upstream.send(event.get("bytes") if event.get("bytes") is not None else event["text"])

                async def to_browser():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                tasks = [asyncio.create_task(to_worker()), asyncio.create_task(to_browser()),
                         asyncio.create_task(monitor(identity, upstream.close))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (AuthenticationError, httpx.HTTPError, websockets.WebSocketException, OSError, RuntimeError):
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=1000)

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_gateway(), host="0.0.0.0", port=int(os.environ.get("WEB_PORT", "8080")),
                timeout_graceful_shutdown=10, access_log=False)
