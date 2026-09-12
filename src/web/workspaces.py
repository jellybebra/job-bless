"""One private network, SQLite database and set of processes per Clerk user.

Only the gateway has Docker access. A worker can wake its own two browsers;
neither container names nor mount paths can be supplied by an HTTP client.
"""

import asyncio
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass

import docker
import httpx


LABEL = "job-bless.workspace"
logger = logging.getLogger(__name__)


@dataclass
class Workspace:
    user_id: str
    key: str
    token: str
    google_key: str
    last_used: float = 0

    @property
    def url(self):
        return f"http://jb-{self.key}-app:8080"


class WorkspacePool:
    def __init__(self, *, root, host_root, image_prefix, image_tag, gateway_container,
                 public_url, publishable_key, owner_email="", docker_client=None,
                 idle_seconds=1800):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.host_root = str(host_root).rstrip("/")
        self.image_prefix, self.image_tag = image_prefix, image_tag
        self.gateway_container = gateway_container
        self.public_url, self.publishable_key = public_url, publishable_key
        self.owner_email = owner_email.lower().strip()
        self.idle_seconds = idle_seconds
        self.docker = docker_client or docker.from_env(timeout=120)
        self.image_ids = {}
        self.attached_networks = set()
        self.db = sqlite3.connect(self.root / "workspaces.db")
        self.db.execute("CREATE TABLE IF NOT EXISTS workspaces (user_id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, token TEXT NOT NULL, google_key TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()
        self.entries = {row[0]: Workspace(*row, last_used=time.monotonic())
                        for row in self.db.execute("SELECT user_id,key,token,google_key FROM workspaces")}
        self.lock = asyncio.Lock()
        self.workspace_locks = {}

    def _lock(self, item):
        return self.workspace_locks.setdefault(item.key, asyncio.Lock())

    def _register(self, identity):
        existing = self.entries.get(identity.user_id)
        if existing:
            return existing
        key = hashlib.sha256(identity.user_id.encode()).hexdigest()[:32]
        item = Workspace(identity.user_id, key, secrets.token_urlsafe(48), secrets.token_urlsafe(48))
        target = self.root / key
        legacy = self.root / "legacy"
        owner = self.owner_email and self.owner_email in identity.verified_emails
        claimed = self.db.execute("SELECT value FROM metadata WHERE key='legacy_owner'").fetchone()
        # Refuse to bind existing private data to an unverified email claim.
        if self.owner_email and identity.email.lower() == self.owner_email and not owner and not claimed:
            raise ValueError("Подтвердите email в Clerk, чтобы получить прежние данные.")
        if owner and legacy.is_dir() and not claimed:
            # Rename makes retry after a crash safe. Existing legacy files are retained for rollback.
            pending = self.root / (key + ".pending")
            if not target.exists():
                if pending.exists():
                    shutil.rmtree(pending)
                shutil.copytree(legacy, pending, symlinks=True)
                pending.rename(target)
            self.db.execute("INSERT INTO metadata VALUES ('legacy_owner', ?)", (identity.user_id,))
        for child in ("app", "hh", "google"):
            directory = target / child
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if hasattr(os, "chown"):
            for path in [target, *target.rglob("*")]:
                if not path.is_symlink():
                    os.chown(path, 1000, 1000)
        self.db.execute("INSERT INTO workspaces VALUES (?,?,?,?)", (item.user_id, key, item.token, item.google_key))
        self.db.commit()
        self.entries[item.user_id] = item
        return item

    def _container(self, name):
        try:
            return self.docker.containers.get(name)
        except docker.errors.NotFound:
            return None

    def _network(self, item):
        name = f"jb-{item.key}"
        if name in self.attached_networks:
            return name
        try:
            network = self.docker.networks.get(name)
        except docker.errors.NotFound:
            network = self.docker.networks.create(name, driver="bridge", labels={LABEL: item.key},
                                                  options={"com.docker.network.bridge.enable_icc": "true"})
        network.reload()
        gateway = self.docker.containers.get(self.gateway_container)
        if gateway.id not in network.attrs.get("Containers", {}):
            network.connect(gateway, aliases=["workspace-gateway"])
        self.attached_networks.add(name)
        return name

    def _start(self, item, service):
        name = f"jb-{item.key}-{service}"
        # A recreated gateway has a new Docker identity even when the user's
        # worker stays on the same image. Restore its network attachment first.
        network = self._network(item)
        image_kind = {"app": "app", "hh": "hh", "google": "aistudio"}[service]
        image = f"{self.image_prefix}-{image_kind}:{self.image_tag}"
        if image not in self.image_ids:
            self.image_ids[image] = self.docker.images.get(image).id
        container = self._container(name)
        if container and (container.attrs["Config"]["Image"] != image or container.attrs["Image"] != self.image_ids[image]):
            container.stop(timeout=20)
            container.remove()
            container = None
        if not container:
            mount = {"app": "/app/data", "hh": "/data/profile", "google": "/app/data"}[service]
            environment = {}
            options = {}
            if service == "app":
                environment = {
                    "WEB_HOST": "0.0.0.0", "WEB_PORT": "8080", "WEB_REQUIRE_AUTH": "true",
                    "WEB_PUBLIC_URL": self.public_url, "WEB_SECURE_COOKIES": str(self.public_url.startswith("https")).lower(),
                    "WORKSPACE_TOKEN": item.token, "WORKSPACE_USER_ID": item.user_id,
                    "WORKSPACE_BROKER_URL": "http://workspace-gateway:8080",
                    "CLERK_PUBLISHABLE_KEY": self.publishable_key,
                    "HH_BROWSER_ENDPOINT": f"ws://jb-{item.key}-hh:3000/hh",
                    "HH_VNC_HOST": f"jb-{item.key}-hh", "GOOGLE_VNC_HOST": f"jb-{item.key}-google",
                    "AISTUDIO_SERVICE_URL": f"http://jb-{item.key}-google:7860",
                    "AISTUDIO_SERVICE_KEY": item.google_key,
                }
                options["command"] = ["python", "main.py", "web", "configs/config.docker.yaml"]
            elif service == "google":
                environment["AISTUDIO_SERVICE_KEY"] = item.google_key
                proxy = os.environ.get("GOOGLE_PROXY_URL", "")
                if proxy:
                    environment.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, NO_PROXY="localhost,127.0.0.1,::1")
            container = self.docker.containers.create(
                image, name=name, hostname=name, network=network,
                labels={LABEL: item.key, "job-bless.service": service, "traefik.enable": "false"},
                environment=environment, user="1000:1000", init=True,
                mem_limit={"app": "384m", "hh": "2048m", "google": "1536m"}[service],
                nano_cpus=1_000_000_000, shm_size="256m", cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                volumes={f"{self.host_root}/{item.key}/{service}": {"bind": mount, "mode": "rw"}},
                tmpfs={"/tmp": "size=256m,mode=1777"},
                restart_policy={"Name": "unless-stopped"},
                log_config=docker.types.LogConfig(type="json-file", config={"max-size": "10m", "max-file": "2"}),
                **options,
            )
        container.reload()
        if container.status != "running":
            container.start()
        return container

    async def _ready(self, item, service):
        port = {"app": 8080, "hh": 3000, "google": 7860}[service]
        host = f"jb-{item.key}-{service}"
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            try:
                if service == "hh":
                    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2)
                    writer.close()
                    await writer.wait_closed()
                else:
                    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                        response = await client.get(f"http://{host}:{port}/" + ("healthz" if service == "app" else "health"))
                        response.raise_for_status()
                return
            except (OSError, TimeoutError, httpx.HTTPError):
                await asyncio.sleep(.5)
        raise RuntimeError("Личная рабочая среда не успела запуститься. Повторите через минуту.")

    async def ensure(self, identity):
        async with self.lock:
            item = self._register(identity)
        async with self._lock(item):
            item.last_used = time.monotonic()
            await asyncio.to_thread(self._start, item, "app")
            await self._ready(item, "app")
            return item

    async def wake(self, token, provider):
        if provider not in ("hh", "google"):
            raise ValueError("Unknown browser")
        item = next((entry for entry in self.entries.values()
                     if hmac.compare_digest(entry.token.encode(), token.encode())), None)
        if item is None:
            raise PermissionError("Unknown workspace")
        async with self._lock(item):
            item.last_used = time.monotonic()
            await asyncio.to_thread(self._start, item, provider)
            await self._ready(item, provider)

    async def reap_idle(self):
        for item in list(self.entries.values()):
            async with self._lock(item):
                if time.monotonic() - item.last_used < self.idle_seconds:
                    continue
                container = await asyncio.to_thread(self._container, f"jb-{item.key}-app")
                if not container or container.status != "running":
                    continue
                try:
                    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                        response = await client.get(item.url + "/internal/idle-status",
                                                    headers={"X-Workspace-Token": item.token})
                        response.raise_for_status()
                        state = response.json()
                    if state["busy"] or state["scheduled"]:
                        continue
                    # App shutdown first snapshots the HH context and stops Google cleanly.
                    for service in ("app", "hh", "google"):
                        owned = await asyncio.to_thread(self._container, f"jb-{item.key}-{service}")
                        if owned:
                            await asyncio.to_thread(owned.stop, timeout=20)
                except (httpx.HTTPError, docker.errors.DockerException):
                    continue

    async def restore(self):
        # Recreate workers on image upgrades so their saved schedules run even
        # before the user's next visit. Containers stopped by idle cleanup stay asleep.
        for item in self.entries.values():
            container = await asyncio.to_thread(self._container, f"jb-{item.key}-app")
            if container and container.status == "running":
                try:
                    async with self._lock(item):
                        await asyncio.to_thread(self._start, item, "app")
                        await self._ready(item, "app")
                except Exception:
                    logger.exception("Could not restore workspace %s", item.key)

    async def running(self):
        items = []
        for item in list(self.entries.values()):
            container = await asyncio.to_thread(self._container, f"jb-{item.key}-app")
            if container and container.status == "running":
                items.append(item)
        return items

    async def deactivate(self, item):
        async with self._lock(item):
            for service in ("app", "hh", "google"):
                container = await asyncio.to_thread(self._container, f"jb-{item.key}-{service}")
                if container:
                    await asyncio.to_thread(container.stop, timeout=20)

    def close(self):
        self.db.close()
        self.docker.close()
