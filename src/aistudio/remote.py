"""The same connection card backed by our private Linux browser service."""

import asyncio
import logging

import httpx

from src.aistudio.runtime import AIStudioRuntime, RuntimeErrorWithHint

logger = logging.getLogger(__name__)


class RemoteAIStudioRuntime(AIStudioRuntime):
    def __init__(self, settings, health):
        super().__init__(settings, health)
        self._endpoint = settings.config.accounts.google_url
        self._key = settings.config.accounts.google_key
        self._watcher = None

    @property
    def available(self):
        return bool(self._endpoint and self._key)

    def snapshot(self):
        return {
            "available": self.available, "selected": self.selected, "state": self.state,
            "external_enabled": not self.selected and bool(self.settings.get("llm.enabled", False)),
            "message": self.message, "busy": self.busy, "revision": self.revision,
            "model": self.settings.get("llm.model", ""), "remote": True,
        }

    async def _request(self, method, path, **kwargs):
        try:
            async with httpx.AsyncClient(base_url=self._endpoint, trust_env=False, timeout=15,
                                         headers={"Authorization": f"Bearer {self._key}"}) as client:
                response = await client.request(method, path, **kwargs)
            if response.status_code == 401:
                raise RuntimeErrorWithHint("Не совпадает ключ подключения к сервису Google. Проверьте конфигурацию сервера.")
            if response.status_code == 409:
                raise RuntimeErrorWithHint("Google ещё выполняет запрос. Дождитесь завершения и повторите.")
            response.raise_for_status()
            status = response.json()
            if not isinstance(status, dict):
                raise ValueError("Expected an object")
            return status
        except ValueError as error:
            raise RuntimeErrorWithHint("Сервис Google вернул неверный ответ. Перезапустите подключение.") from error
        except httpx.HTTPError as error:
            raise RuntimeErrorWithHint("Сервис Google недоступен. Проверьте, что контейнер AI Studio запущен.") from error

    def _update(self, status):
        state = status.get("state", "error")
        self.models = status.get("models", [])
        self._set_state(state, status.get("message", ""))
        if state == "ready":
            self.settings.managed_llm = {"base_url": self._endpoint, "api_key": self._key}
        else:
            self.settings.managed_llm = None
        self.health.invalidate()

    async def _connect(self, *, login):
        try:
            if not self.available:
                raise RuntimeErrorWithHint("Не настроен сервис Google AI Studio.")
            self.settings.managed_llm = None
            await self._request("POST", "/control/connect", json={
                "login": login, "model": str(self.settings.get("llm.model", "")),
            })
            # The service owns the login timeout and verifies an actual model response.
            async with asyncio.timeout(1100):
                while True:
                    status = await self._request("GET", "/control/status")
                    self._update(status)
                    if self.state == "ready":
                        errors = await self.settings.save({
                            "llm.connection": "aistudio", "llm.enabled": "1",
                            "llm.model": status.get("model", ""),
                        }, keys={"llm.connection", "llm.enabled", "llm.model"})
                        if errors:
                            raise RuntimeErrorWithHint("Не удалось сохранить подключение Google.")
                        return
                    if self.state in ("error", "stopped"):
                        return
                    await asyncio.sleep(1)
        except (RuntimeErrorWithHint, TimeoutError) as error:
            self.settings.managed_llm = None
            self._set_state("error", str(error) or "Время ожидания подключения Google истекло. Попробуйте снова.")
        except Exception:
            logger.exception("remote Google connection failed")
            self.settings.managed_llm = None
            self._set_state("error", "Не удалось подключить Google. Попробуйте снова.")
        finally:
            if self.selected and (not self._watcher or self._watcher.done()):
                self._watcher = asyncio.create_task(self._watch())

    async def _watch(self):
        while True:
            await asyncio.sleep(5)
            if self.busy or not self.selected or not self.settings.get("llm.enabled", False):
                continue
            try:
                status = await self._request("GET", "/control/status")
                self._update(status)
                if self.state == "idle":
                    # Only a fresh service restart is automatically resumed.
                    # An expired login or failed generation requires user action.
                    self.begin()
            except RuntimeErrorWithHint as error:
                self.settings.managed_llm = None
                self._set_state("error", str(error))

    async def stop(self, *, disable=False):
        if self.busy:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        if self._watcher:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
        try:
            if self.available:
                await self._request("POST", "/control/stop")
        except RuntimeErrorWithHint:
            logger.warning("Google service was unavailable during disconnect")
        self.settings.managed_llm = None
        if disable and self.selected:
            await self.settings.save({"llm.enabled": ""}, keys={"llm.enabled"})
        self.health.invalidate()
        self._set_state("stopped", "Подключение Google остановлено")
