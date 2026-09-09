"""Launch the self-contained Windows distribution with per-user data."""

import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def open_when_ready(url, identity_url, token):
    for _ in range(120):
        try:
            request = urllib.request.Request(identity_url, headers={"Cookie": "job_bless_token=" + token})
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(.25)


def main():
    data_root = Path(os.environ.get("JOB_BLESS_USER_HOME", Path(os.environ["LOCALAPPDATA"]) / "job-bless"))
    data_root.mkdir(parents=True, exist_ok=True)
    instance_file = data_root / "instance.json"
    # One user data directory has one server; a second click opens its browser.
    import hashlib
    name = "Local\\job-bless-" + hashlib.sha256(str(data_root.resolve()).lower().encode()).hexdigest()[:24]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, name)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        for _ in range(40):
            try:
                identity = json.loads(instance_file.read_text(encoding="utf-8"))
                request = urllib.request.Request(identity["identity_url"], headers={"Cookie": "job_bless_token=" + identity["token"]})
                with urllib.request.urlopen(request, timeout=1) as response:
                    if response.status == 200:
                        webbrowser.open(identity["url"])
                        return
            except Exception:
                time.sleep(.25)
        ctypes.windll.user32.MessageBoxW(None, "job-bless уже запускается. Попробуйте открыть его через несколько секунд.", "job-bless", 0)
        return
    try:
        os.chdir(data_root)
        (data_root / "data").mkdir(exist_ok=True)
        os.environ["JOB_BLESS_DATA_DIR"] = str(data_root / "data")
        os.environ["JOB_BLESS_AISTUDIO_BUNDLE"] = str(APP / "vendor/aistudio")
        logging.basicConfig(filename=str(data_root / "job-bless.log"), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s", encoding="utf-8")
        from src.config import Config
        from src.web.app import create_app
        import uvicorn

        config = Config()
        config.db.driver = "sqlite"
        config.db.sqlite_path = str(data_root / "data/career_agent.db")
        config.web.host = "0.0.0.0"
        config.web.port = free_port()
        config.web.token = secrets.token_urlsafe(32)
        cdp_port = free_port()
        config.browser.cdp.endpoint = f"http://127.0.0.1:{cdp_port}"
        config.browser.local_process.args = [
            f"--remote-debugging-port={cdp_port}", f"--user-data-dir={data_root / 'data/browser-profile'}",
            "--new-window", "--start-maximized", "--no-first-run", "--no-default-browser-check", "https://hh.ru",
        ]
        base_url = f"http://127.0.0.1:{config.web.port}"
        url = f"{base_url}/actions?token={config.web.token}"
        identity_url = f"{base_url}/desktop/identity"
        instance_file.write_text(json.dumps({"url": url, "identity_url": identity_url, "token": config.web.token}), encoding="utf-8")
        app = create_app(config)
        server = uvicorn.Server(uvicorn.Config(app, host=config.web.host, port=config.web.port, log_config=None,
                                             timeout_graceful_shutdown=5))

        @app.get("/desktop/identity")
        async def identity():
            return {"application": "job-bless"}

        @app.post("/desktop/exit")
        async def exit_app():
            app.state.shutting_down = True
            server.should_exit = True
            return {"stopping": True}

        app.state.templates.env.globals["desktop_mode"] = True
        threading.Thread(target=open_when_ready, args=(url, identity_url, config.web.token), daemon=True).start()
        asyncio.run(server.serve())
    finally:
        instance_file.unlink(missing_ok=True)
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Could not start job-bless")
        ctypes.windll.user32.MessageBoxW(None, "Не удалось запустить job-bless. Подробности в файле job-bless.log в папке данных приложения.", "job-bless", 16)
