"""Private, narrowly scoped wake-up request used by an isolated worker."""

import os

import httpx


async def wake_browser(provider):
    broker = os.environ.get("WORKSPACE_BROKER_URL", "")
    if not broker:
        return
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        response = await client.post(broker + "/internal/wake/" + provider,
                                     headers={"X-Workspace-Token": os.environ["WORKSPACE_TOKEN"]})
    if response.status_code != 200:
        raise RuntimeError("Не удалось запустить личный браузер. Сервер занят; повторите чуть позже.")
