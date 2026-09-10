"""Native job-bless owns one Docker / Camoufox container per data directory."""

import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time

logger = logging.getLogger(__name__)
OWNER_LABEL = "io.job-bless.hh-owner"
BUILD_FILES = (
    "docker/hh/Dockerfile", "docker/hh/server.py", "docker/hh/server.cjs",
    "docker/display.sh", "docker/download_camoufox.py", "pyproject.toml", "uv.lock",
)


def free_browser_ports():
    # Keep both sockets reserved until both ports are chosen. Docker detects a
    # race with another process when binding; a restart retains these fixed ports.
    with socket.socket() as playwright, socket.socket() as vnc:
        playwright.bind(("127.0.0.1", 0))
        vnc.bind(("127.0.0.1", 0))
        return playwright.getsockname()[1], vnc.getsockname()[1]


class DockerBrowserRuntime:
    def __init__(self, config):
        self.config = config
        self.data = Path(config.db.sqlite_path).resolve().parent
        self.root = Path(__file__).resolve().parents[2]
        if (self.root / "vendor/hh/docker/hh/Dockerfile").is_file():
            self.root = self.root / "vendor/hh"  # Windows distribution build context.
        self.identity = hashlib.sha256(os.path.normcase(str(self.data)).encode()).hexdigest()[:20]
        self.name = f"job-bless-hh-{self.identity}"
        self.docker = shutil.which("docker")
        self._lock = None
        self._container = None

    def command(self, *args, check=True, timeout=30):
        result = subprocess.run(
            [self.docker, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if check and result.returncode:
            raise RuntimeError(f"Docker: {result.stderr.strip()[-1500:]}")
        return result

    def inspect(self):
        result = self.command("container", "inspect", self.name, check=False)
        if result.returncode:
            if "No such" in result.stderr:
                return None
            raise RuntimeError(f"Не удалось проверить контейнер HH: {result.stderr.strip()}")
        container = json.loads(result.stdout)[0]
        if container["Config"].get("Labels", {}).get(OWNER_LABEL) != self.identity:
            raise RuntimeError(f"Имя {self.name} занято чужим контейнером; он не изменён.")
        return container

    def start(self):
        if not self.docker:
            raise RuntimeError("Для HH нужен Docker. Установите и запустите Docker Desktop с Linux-контейнерами.")
        self.data.mkdir(parents=True, exist_ok=True)
        try:
            self._lock = (self.data / "hh-runtime.lock").open("a+b")
            self._lock.seek(0)
            if not self._lock.read(1):
                self._lock.write(b"0")
                self._lock.flush()
            self._lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if self._lock:
                self._lock.close()
            self._lock = None
            raise RuntimeError("Эта папка данных уже открыта другим экземпляром job-bless.") from error
        try:
            info = self.command("info", "--format", "{{.OSType}}", check=False)
            if info.returncode or info.stdout.strip() != "linux":
                raise RuntimeError("Запустите Docker Desktop и включите Linux-контейнеры, затем откройте job-bless снова.")
            revision = hashlib.sha256()
            for name in BUILD_FILES:
                revision.update(name.encode())
                revision.update((self.root / name).read_bytes())
            image = "job-bless-hh:" + revision.hexdigest()[:20]
            if self.command("image", "inspect", image, check=False).returncode:
                log = self.data / "hh-build.log"
                logger.info("Готовлю Camoufox в Docker. Первая сборка займёт несколько минут. Журнал: %s", log)
                with log.open("w", encoding="utf-8") as output:
                    result = subprocess.run(
                        [self.docker, "build", "-t", image, "-f", str(self.root / "docker/hh/Dockerfile"), str(self.root)],
                        stdout=output, stderr=subprocess.STDOUT, timeout=1800,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                if result.returncode:
                    raise RuntimeError(f"Не удалось собрать Camoufox. Подробности: {log}")
            container = self.inspect()
            if container and container["Config"]["Image"] != image:
                self.command("rm", "-f", container["Id"])
                container = None
            if container is None:
                playwright_port, vnc_port = free_browser_ports()
                result = self.command(
                    "run", "-d", "--init", "--name", self.name,
                    "--label", f"{OWNER_LABEL}={self.identity}",
                    "--memory", "1024m", "--shm-size", "256m", "--cpus", "1",
                    "--stop-timeout", "20", "--tmpfs", "/tmp:size=256m,mode=1777",
                    "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                    "--publish", f"127.0.0.1:{playwright_port}:3000",
                    "--publish", f"127.0.0.1:{vnc_port}:5900",
                    "--mount", f"type=volume,source={self.name}-profile,target=/data/profile", image,
                )
                self._container = result.stdout.strip()
            else:
                self._container = container["Id"]
                if not container["State"]["Running"]:
                    self.command("start", self._container)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                container = self.inspect()
                state = container["State"]
                if not state["Running"] or state.get("Health", {}).get("Status") == "unhealthy":
                    raise RuntimeError(f"Camoufox не запустился. Проверьте docker logs {self.name}")
                if state.get("Health", {}).get("Status") == "healthy":
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(f"Camoufox не готов за 90 секунд. Проверьте docker logs {self.name}")
            ports = container["NetworkSettings"]["Ports"]
            self.config.browser.endpoint = f"ws://127.0.0.1:{ports['3000/tcp'][0]['HostPort']}/hh"
            self.config.browser.storage_state_path = str(self.data / "hh-session.json")
            self.config.accounts.hh_vnc_host = "127.0.0.1"
            self.config.accounts.hh_vnc_port = int(ports["5900/tcp"][0]["HostPort"])
            assets = self.data / "runtime" / ("novnc-" + revision.hexdigest()[:20])
            if not (assets / "core/rfb.js").is_file():
                assets.mkdir(parents=True, exist_ok=True)
                self.command("cp", f"{self._container}:/opt/novnc/.", str(assets))
            self.config.accounts.novnc_path = str(assets)
            logger.info("HH Camoufox готов в Docker (%s).", self.name)
            return self
        except BaseException:
            self.stop()
            raise

    def stop(self):
        try:
            if self._container:
                container = self.inspect()
                if container and container["Id"] == self._container and container["State"]["Running"]:
                    self.command("stop", "--time", "20", self._container, timeout=25)
        finally:
            self._container = None
            if self._lock:
                self._lock.close()
                self._lock = None


def prepare_browser(config):
    """Explicit endpoint means Compose owns startup; otherwise own a local container."""
    if config.browser.endpoint:
        return None
    return DockerBrowserRuntime(config).start()


def local_panel_url(config):
    """Private bootstrap link becomes an expiring owner session on first navigation."""
    host = "127.0.0.1" if config.web.host in ("0.0.0.0", "::") else config.web.host
    url = f"http://{host}:{config.web.port}/actions"
    if not config.web.password_hash:
        config.web.token = config.web.token or secrets.token_urlsafe(32)
        url += f"?token={config.web.token}"
    return url
