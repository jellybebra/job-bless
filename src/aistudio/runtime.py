"""Run preinstalled AIStudioToAPI components; never download on first use."""

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import time

import httpx

from src.aistudio.process import OwnedProcess
from src.llm.catalog import text_model_ids
from src.llm.models import ModelInfo

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


class RuntimeErrorWithHint(Exception):
    """A message safe to show in the connection card."""


class AIStudioRuntime:
    def __init__(self, settings, health, *, bundle: Path = None, data: Path = None):
        self.settings = settings
        self.health = health
        self.bundle = Path(bundle or os.environ.get("JOB_BLESS_AISTUDIO_BUNDLE", ROOT / "vendor" / "aistudio")).resolve()
        default_data = (Path(settings.config.db.sqlite_path).resolve().parent
                        if settings.config.db.driver == "sqlite" else ROOT / "data")
        self.data = Path(data or os.environ.get("JOB_BLESS_DATA_DIR", default_data)).resolve() / "aistudio"
        self.state = "idle"
        self.message = ""
        self.revision = 0
        self._task = None
        self._server = None
        self._login = None
        self._lock_file = None
        self._run_id = ""
        self._endpoint = ""
        self._key = ""
        self.models = []

    @property
    def available(self):
        required = ("manifest.json", self.node_path, self.browser_path, "app/main.js",
                    "app/node_modules/playwright/package.json", "app/configs/models.json")
        return all((self.bundle / item).is_file() for item in required)

    @property
    def node_path(self):
        return "node/node.exe" if os.name == "nt" else "node/node"

    @property
    def browser_path(self):
        return "camoufox/camoufox.exe" if os.name == "nt" else "camoufox/camoufox"

    @property
    def busy(self):
        return self._task is not None and not self._task.done()

    @property
    def selected(self):
        return self.settings.get("llm.connection", "custom") == "aistudio"

    def snapshot(self):
        if self.state == "ready" and self._server and self._server.poll() is not None:
            self._set_state("error", "Сервис остановился. Нажмите «Запустить снова».")
            self.health.invalidate()
        elif self.state == "ready":
            status = self._read_status()
            if not status.get("connected"):
                # Do not say the model is ready after its browser disconnected.
                self._set_state("reconnecting", "Восстанавливаем подключение к Google AI Studio…")
        elif self.state == "reconnecting":
            if not self._server or self._server.poll() is not None:
                self._set_state("error", "Сервис остановился. Нажмите «Запустить снова».")
            elif self._read_status().get("connected"):
                self._set_state("ready", "Подключено через Google AI Studio")
        return {
            "available": self.available, "selected": self.selected, "state": self.state,
            "external_enabled": not self.selected and bool(self.settings.get("llm.enabled", False)),
            "message": self.message, "busy": self.busy, "revision": self.revision,
            "model": self.settings.get("llm.model", ""),
        }

    def _set_state(self, state, message=""):
        if (state, message) != (self.state, self.message):
            self.state, self.message = state, message
            self.revision += 1

    def begin(self, *, login=False):
        if self.busy:
            return
        self._set_state("starting", "Запускаем встроенное подключение…")
        self._task = asyncio.create_task(self._connect(login=login))

    def autostart(self):
        if self.selected and self.settings.get("llm.enabled", False):
            self.begin()

    async def _connect(self, *, login):
        try:
            if not self.available:
                raise RuntimeErrorWithHint("В этой сборке нет встроенных компонентов. Установите полный комплект job-bless или подключите свой API-сервис.")
            await asyncio.to_thread(self._prepare)
            await self._stop_processes()
            if login or not self._has_auth():
                self._set_state("login", "Войдите в Google в окне браузера. После входа проверим подключение автоматически.")
                before = self._auth_stamp()
                self._login = self._spawn("login")
                await self._wait_for_login(before)
                await asyncio.to_thread(self._login.stop)
                self._login = None
            self._set_state("starting", "Запускаем Google AI Studio…")
            self._run_id = secrets.token_hex(16)
            port, ws_port = _free_ports()
            self._endpoint = f"http://127.0.0.1:{port}"
            self._server = self._spawn("bridge", port=port, ws_port=ws_port)
            await self._wait_for_server()
            self._set_state("checking", "Проверяем ответ модели…")
            model = await self._probe()
            self.settings.managed_llm = {"base_url": self._endpoint, "api_key": self._key}
            errors = await self.settings.save({
                "llm.connection": "aistudio", "llm.enabled": "1", "llm.model": model,
            }, keys={"llm.connection", "llm.enabled", "llm.model"})
            if errors:
                raise RuntimeErrorWithHint("Не удалось сохранить подключение.")
            self.health.invalidate()
            self._set_state("ready", "Подключено через Google AI Studio")
        except asyncio.CancelledError:
            raise
        except RuntimeErrorWithHint as error:
            await self._stop_processes()
            self._set_state("error", str(error))
        except Exception:
            logger.exception("bundled AI Studio connection failed")
            await self._stop_processes()
            self._set_state("error", "Не удалось запустить встроенное подключение. Попробуйте ещё раз или войдите в Google заново.")

    def _prepare(self):
        self.data.mkdir(parents=True, exist_ok=True)
        if not self._lock_file:
            lock = (self.data / "runtime.lock").open("a+b")
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.close()
                raise RuntimeErrorWithHint("Это подключение уже используется другим экземпляром job-bless.") from None
            self._lock_file = lock
        config_dir = self.data / "configs"
        (config_dir / "auth").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.bundle / "app/configs/models.json", config_dir / "models.json")
        key_file = self.data / "api-key"
        if not key_file.exists():
            key_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        self._key = key_file.read_text(encoding="utf-8").strip()
        if not self._key:
            raise RuntimeErrorWithHint("Не удалось прочитать ключ встроенного подключения.")

    def _spawn(self, kind, *, port=0, ws_port=0):
        # No npm/setup command is allowed here. All executable components and
        # dependencies must already be supplied by the distribution builder.
        env = dict(os.environ)
        env.pop("WS_PORT", None)
        env.update({
            "NODE_ENV": "production", "HOST": "127.0.0.1", "PORT": str(port),
            "API_KEYS": self._key, "CHECK_UPDATE": "false", "LOG_LEVEL": "WARN",
            "MAX_RETRIES": "1", "SWITCH_ON_USES": "0", "MAX_CONTEXTS": "1",
            "CAMOUFOX_EXECUTABLE_PATH": str(self.bundle / self.browser_path),
            "JOB_BLESS_AISTUDIO_APP": str(self.bundle / "app"),
            "JOB_BLESS_AISTUDIO_WS_PORT": str(ws_port), "JOB_BLESS_AISTUDIO_RUN_ID": self._run_id,
        })
        return OwnedProcess(
            [str(self.bundle / self.node_path), str(ROOT / "src/aistudio" / f"{kind}.cjs")],
            cwd=self.data, env=env, log=self.data / f"{kind}.log",
        )

    def _auth_stamp(self):
        path = self.data / "configs/auth/auth-0.json"
        return path.stat().st_mtime_ns if path.is_file() else 0

    def _has_auth(self):
        try:
            state = json.loads((self.data / "configs/auth/auth-0.json").read_text(encoding="utf-8"))
            return bool(state.get("cookies")) and not state.get("expired", False)
        except (OSError, ValueError, AttributeError):
            return False

    async def _wait_for_login(self, before):
        deadline = time.monotonic() + 610
        while time.monotonic() < deadline:
            code = self._login.poll()
            if code is not None:
                if code == 0 and self._has_auth() and self._auth_stamp() != before:
                    return
                raise RuntimeErrorWithHint("Вход не завершён. Нажмите «Войти в Google» и завершите вход в открывшемся браузере.")
            await asyncio.sleep(.5)
        raise RuntimeErrorWithHint("Время ожидания входа истекло. Нажмите «Войти в Google» ещё раз.")

    def _read_status(self):
        try:
            path = self.data / "bridge-status.json"
            status = json.loads(path.read_text(encoding="utf-8"))
            if (status.get("run_id") == self._run_id and self._server
                    and status.get("pid") == self._server.pid and time.time() - path.stat().st_mtime < 10):
                return status
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    async def _wait_for_server(self):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self._server.poll() is not None:
                raise RuntimeErrorWithHint("Сервис не запустился. Попробуйте снова или войдите в Google заново.")
            status = self._read_status()
            if status.get("connected"):
                return
            if status.get("error"):
                if status.get("error_code") == "region_unsupported":
                    raise RuntimeErrorWithHint(
                        "Google AI Studio недоступен из текущей сети: Google вернул «Region not supported». "
                        "Настройте доступ из поддерживаемого региона и нажмите «Запустить снова». Повторный вход не нужен."
                    )
                raise RuntimeErrorWithHint("Не удалось открыть Google AI Studio. Проверьте доступ к Google и повторите вход.")
            await asyncio.sleep(.5)
        raise RuntimeErrorWithHint("Google AI Studio не ответил вовремя. Проверьте доступ к Google и повторите вход.")

    async def _probe(self):
        async with httpx.AsyncClient(base_url=self._endpoint, headers={"Authorization": f"Bearer {self._key}"},
                                     timeout=300, trust_env=False) as client:
            response = await client.get("/v1/models", timeout=10)
            response.raise_for_status()
            self.models = text_model_ids(
                ModelInfo(id=item["id"], raw=item)
                for item in response.json().get("data", []) if isinstance(item.get("id"), str)
            )
            current = self.settings.get("llm.model", "")
            model = current if current in self.models else next((name for name in self.models if "flash" in name), "")
            if not model:
                raise RuntimeErrorWithHint("В сервисе не найдена модель для работы с текстом.")
            for token_limit in (64, 1024):
                response = await client.post("/v1/chat/completions", json={
                    "model": model, "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                    "max_tokens": token_limit, "temperature": 1.0, "stream": False,
                })
                if response.status_code >= 400:
                    raise RuntimeErrorWithHint("Сервис запущен, но модель не ответила. Проверьте доступность Google AI Studio для аккаунта или повторите вход.")
                choices = response.json().get("choices") or []
                if choices and choices[0].get("message", {}).get("content"):
                    return model
                # Thinking can consume the tiny probe budget before any answer.
                # Retry only that case once; never mark an empty answer as ready.
                if token_limit == 64 and choices and choices[0].get("finish_reason") == "length":
                    logger.info("Google probe exhausted its short token budget; retrying with room for an answer")
                    continue
                raise RuntimeErrorWithHint("Модель вернула пустой ответ. Попробуйте запустить подключение ещё раз.")

    async def _stop_processes(self):
        for name in ("_login", "_server"):
            process = getattr(self, name)
            if process:
                await asyncio.to_thread(process.stop)
                setattr(self, name, None)
        self.settings.managed_llm = None

    async def stop(self, *, disable=False):
        if self.busy:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._stop_processes()
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None
        if disable and self.selected:
            await self.settings.save({"llm.enabled": ""}, keys={"llm.enabled"})
        self.health.invalidate()
        self._set_state("stopped", "Встроенное подключение остановлено")


def _free_ports():
    sockets = []
    try:
        for _ in range(2):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return tuple(sock.getsockname()[1] for sock in sockets)
    finally:
        for sock in sockets:
            sock.close()
