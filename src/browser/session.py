"""One Camoufox connection and context shared by login and all HH task lanes.

The web app keeps it alive between jobs, including a pending manual challenge.
Cookies, localStorage and IndexedDB are saved after work and at shutdown; a new
connection restores the snapshot after either the app or container restarts.
"""

import asyncio
import logging
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from src.config import BrowserConfig
from src.browser.connection import connect_browser

logger = logging.getLogger(__name__)

async def save_storage_state(context, storage_path: Path, *, checkpoint=None) -> None:
    """Commit a complete snapshot, including IndexedDB, without truncating the old one."""
    snapshot = await context.storage_state(indexed_db=True)
    if checkpoint:
        checkpoint()
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    pending = storage_path.with_suffix(".pending.json")
    try:
        pending.write_text(json.dumps(snapshot), encoding="utf-8")
        pending.chmod(0o600)
        pending.replace(storage_path)
    finally:
        pending.unlink(missing_ok=True)


class SharedBrowserSession:
    """Reference-counted connection to the browser.

    CLI users close on the last release; the web app keeps the connection until
    its lifespan ends. Only the last user snapshots the shared context.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._users = 0
        self.keep_alive = False
        self._config = None

    @asynccontextmanager
    async def connection(self, config):
        acquired = await self.acquire(config)
        try:
            yield acquired
        finally:
            await self.release()

    @property
    def is_connected(self) -> bool:
        return bool(self._browser and self._browser.is_connected())

    async def acquire(self, config: BrowserConfig) -> Tuple[Browser, BrowserContext]:
        async with self._lock:
            if not self.is_connected:
                await self._disconnect()  # drop a stale connection, if any
                await self._connect(config)
            self._users += 1
            logger.debug("Browser session acquired (users=%d).", self._users)
            return self._browser, self._context

    async def release(self) -> None:
        async with self._lock:
            self._users = max(0, self._users - 1)
            logger.debug("Browser session released (users=%d).", self._users)
            if self._users == 0:
                await self._save()
                if not self.keep_alive:
                    await self._disconnect()

    async def close(self) -> None:
        async with self._lock:
            self._users = 0
            try:
                await self._save()
            finally:
                await self._disconnect()

    async def forget(self, config: BrowserConfig) -> None:
        """Discard the HH context and saved login after its jobs have stopped."""
        async with self._lock:
            if self._users:
                raise RuntimeError("Дождитесь остановки действий HH и повторите выход.")
            if self._context and self.is_connected:
                # Closing the whole context also clears localStorage and IndexedDB.
                await asyncio.wait_for(self._context.close(), timeout=10)
            await self._disconnect()
            storage = Path(config.storage_state_path)
            storage.unlink(missing_ok=True)
            storage.with_suffix(".pending.json").unlink(missing_ok=True)
            self._config = None

    # --- internals --------------------------------------------------------

    async def _connect(self, config: BrowserConfig) -> None:
        self._config = config
        try:
            from src.workspace import wake_browser
            await wake_browser("hh")
            self._playwright = await async_playwright().start()
            self._browser = await connect_browser(self._playwright, config)
            self._context = await self._pick_context()
        except BaseException:
            await self._disconnect()
            raise

    async def _pick_context(self) -> BrowserContext:
        contexts = self._browser.contexts
        if contexts:
            logger.info("Using the existing BrowserContext (profile session).")
            return contexts[0]

        # Camoufox controls window/screen dimensions; Playwright must not resize them.
        kwargs = {"no_viewport": True}
        storage = Path(self._config.storage_state_path)
        if storage.is_file():
            logger.info("Restoring the saved HH session.")
            kwargs["storage_state"] = str(storage)
        logger.info("Creating a new BrowserContext.")
        return await self._browser.new_context(**kwargs)

    async def _save(self):
        if self._context and self._config and self.is_connected:
            await save_storage_state(self._context, Path(self._config.storage_state_path))

    async def _disconnect(self) -> None:
        self._context = None
        if self._browser:
            try:
                if self._browser.is_connected():
                    await self._browser.close()
            except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001
                logger.warning(f"Error while closing the browser connection: {e}")
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001
                logger.warning(f"Error while stopping Playwright driver instance: {e}")
            self._playwright = None


# Process-wide: lanes must share it, that is the whole point.
SESSION = SharedBrowserSession()
