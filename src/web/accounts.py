"""Authenticated, short-lived access to the two private browser displays."""

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

import anyio
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from src.db.models import TaskKind
from src.web import jobs
from src.web.panel import hh_account_status
from src.web.tasks import LANE_ACTIVITY, LANE_MAIN, LANE_PROFILE, TaskBusyError

SCREEN_SECONDS = 15 * 60
router = APIRouter(prefix="/accounts")


@dataclass
class ScreenLease:
    owner: str
    expires: float
    socket: WebSocket | None = None
    revoked: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class RemoteScreens:
    def __init__(self, config):
        self.config = config
        self.leases = {}
        self.lock = asyncio.Lock()

    def enabled(self, provider):
        return bool({"hh": self.config.hh_vnc_host, "google": self.config.google_vnc_host}.get(provider))

    def active(self, provider):
        lease = self.leases.get(provider)
        return bool(lease and lease.expires > time.monotonic())

    def grant(self, provider, owner):
        if self.active(provider):
            lease = self.leases[provider]
            if lease.owner != owner:
                raise HTTPException(409, "Этот браузер уже открыт на другом устройстве. Закройте его и повторите.")
            return lease
        lease = ScreenLease(owner, time.monotonic() + SCREEN_SECONDS)
        self.leases[provider] = lease
        return lease

    async def revoke(self, provider, owner=None):
        lease = self.leases.get(provider)
        if not lease or (owner is not None and lease.owner != owner):
            return False
        self.leases.pop(provider, None)
        lease.revoked.set()
        if lease.socket:
            # Wait for our connection cleanup, not the server's request task.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(lease.closed.wait(), timeout=3)
        return True

    async def revoke_owner(self, owner):
        for provider in list(self.leases):
            await self.revoke(provider, owner)

    async def close(self):
        for provider in list(self.leases):
            await self.revoke(provider)


def check_provider(request, provider):
    if not request.app.state.screens.enabled(provider):
        raise HTTPException(404, "Удалённый вход для этого аккаунта не настроен")


@router.get("/{provider}")
async def screen_page(provider: str, request: Request):
    check_provider(request, provider)
    return request.app.state.templates.TemplateResponse(request, "account_screen.html", {
        "provider": provider, "title": "HH" if provider == "hh" else "Google AI Studio",
    })


@router.post("/{provider}/open")
async def open_screen(provider: str, request: Request):
    check_provider(request, provider)
    state = request.app.state
    session = state.auth.session(request)
    if not session:
        raise HTTPException(401, "Войдите в панель")
    from src.workspace import wake_browser
    try:
        await wake_browser(provider)
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    async with state.screens.lock:
        if provider == "google":
            if state.tasks.is_busy or state.tasks.lane(LANE_PROFILE).is_busy:
                raise HTTPException(409, "Дождитесь завершения текущего действия перед входом в Google")
            if not state.aistudio.busy or state.aistudio.state not in ("starting", "login"):
                raise HTTPException(409, "Нажмите «Подключить Google» или «Войти в Google» в карточке нейросети")
        async with state.tasks._start_lock:
            state.screens.grant(provider, session.id)
        if provider == "hh":
            try:
                # Claim the screen before stopping tasks: the scheduler sees the
                # lease and cannot start another browser task in the gap.
                stopping = []
                for name in (LANE_MAIN, LANE_ACTIVITY):
                    lane = state.tasks.lane(name)
                    if lane.is_busy and not (name == LANE_MAIN and lane.current.kind == TaskKind.LOGIN):
                        state.tasks.request_stop(name)
                        stopping.append(lane.task)
                if stopping:
                    _, pending = await asyncio.wait(stopping, timeout=6)
                    if pending:
                        raise HTTPException(409, "Дождитесь остановки действий и откройте браузер снова")
                if not state.tasks.is_busy:
                    await state.tasks.start(TaskKind.LOGIN, jobs.login_job)
            except BaseException:
                await state.screens.revoke(provider, session.id)
                raise
        return {"open": True, "expires_in": SCREEN_SECONDS}


@router.post("/{provider}/close")
async def close_screen(provider: str, request: Request):
    check_provider(request, provider)
    state = request.app.state
    session = state.auth.session(request)
    if session and await state.screens.revoke(provider, session.id):
        if provider == "hh":
            current = state.tasks.current
            if (state.tasks.is_busy and current.kind == TaskKind.LOGIN
                    and not current.result.get("account_verified")):
                state.tasks.request_stop(LANE_MAIN)
        elif state.aistudio.state == "login":
            await state.aistudio.stop()
    return {"open": False}


@router.get("/{provider}/status")
async def screen_status(provider: str, request: Request):
    check_provider(request, provider)
    state = request.app.state
    if provider == "google":
        snapshot = state.aistudio.snapshot()
        return {"state": snapshot["state"], "message": snapshot["message"]}
    current = state.tasks.current
    if current and current.kind == TaskKind.LOGIN:
        if current.result.get("account_verified"):
            return {"state": "ready", "message": "Вход в HH подтверждён. Можно закрыть окно."}
        if current.error_message:
            return {"state": "error", "message": current.error_message}
        if state.tasks.is_busy:
            return {"state": "login", "message": "Войдите в HH. После входа проверим аккаунт автоматически."}
    account = await hh_account_status(state.repository)
    return {"state": "ready" if account["logged_in"] else "login",
            "message": "Аккаунт HH подключён" if account["logged_in"] else "Войдите в аккаунт HH"}


@router.websocket("/{provider}/ws")
async def screen_socket(websocket: WebSocket, provider: str):
    state = websocket.app.state
    auth = state.auth
    session = auth.session(websocket)
    lease = state.screens.leases.get(provider)
    if (not auth.enabled or not session or not auth.same_origin(websocket)
            or not state.screens.enabled(provider) or not state.screens.active(provider)
            or not lease or lease.owner != session.id):
        await websocket.close(code=1008)
        return
    if lease.socket:
        lease.revoked.set()
        try:
            await asyncio.wait_for(lease.closed.wait(), timeout=3)
        except TimeoutError:
            await websocket.close(code=1008)
            return
    lease.revoked = asyncio.Event()
    lease.closed = asyncio.Event()
    lease.socket = websocket
    config = state.config.accounts
    host, port = ((config.hh_vnc_host, config.hh_vnc_port) if provider == "hh"
                  else (config.google_vnc_host, config.google_vnc_port))
    pending = set()
    writer = None
    await websocket.accept()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)

        async def from_browser():
            while chunk := await reader.read(65536):
                await websocket.send_bytes(chunk)

        async def to_browser():
            while True:
                chunk = await websocket.receive_bytes()
                writer.write(chunk)
                await writer.drain()

        async def until_revoked():
            while (auth.session(websocket) is session and state.screens.active(provider)
                   and state.screens.leases.get(provider) is lease and lease.socket is websocket):
                if provider == "google" and state.aistudio.state not in ("starting", "login"):
                    return
                try:
                    await asyncio.wait_for(lease.revoked.wait(), timeout=1)
                    return
                except TimeoutError:
                    pass

        pending = {asyncio.create_task(fn()) for fn in (from_browser, to_browser, until_revoked)}
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, TimeoutError, WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        with anyio.CancelScope(shield=True):
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if writer:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1)
            if lease.socket is websocket:
                lease.socket = None
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1000)
            lease.closed.set()
