"""Private Google container API; uses exactly the desktop login/bridge runtime.

Only job-bless can call this API. The Google password is entered in the browser
on the virtual display, and is never part of a control request.
"""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager

import httpx
import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from src.aistudio.runtime import AIStudioRuntime
from src.config import Config


class RuntimeSettings:
    """Only runtime preferences; the main application's SQLite owns user settings."""

    def __init__(self):
        self.config = Config()
        self.values = {"llm.model": "gemini-flash-latest"}
        self.managed_llm = None

    def get(self, key, default=None):
        return self.values.get(key, default)

    async def save(self, values, *, keys):
        self.values.update({key: value for key, value in values.items() if key in keys})
        return []


class RuntimeHealth:
    def invalidate(self):
        pass  # The runtime itself probes a model; the parent owns the UI health cache.


class ConnectRequest(BaseModel):
    login: bool = False
    model: str = Field(default="", max_length=200)


def create_service(*, key=None, runtime=None):
    key = key if key is not None else os.environ.get("AISTUDIO_SERVICE_KEY", "")
    if len(key) < 24:
        raise ValueError("AISTUDIO_SERVICE_KEY must contain at least 24 characters")
    runtime = runtime or AIStudioRuntime(RuntimeSettings(), RuntimeHealth())
    generation = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await runtime.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/health" and not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), f"Bearer {key}".encode()
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"service": "job-bless-aistudio", "available": runtime.available}

    @app.get("/control/status")
    async def status():
        return {**runtime.snapshot(), "models": runtime.models}

    @app.post("/control/connect")
    async def connect(body: ConnectRequest):
        if generation.locked():
            return JSONResponse({"detail": "Generation is in progress"}, status_code=409)
        async with generation:
            if not runtime.busy and (body.login or runtime.snapshot()["state"] != "ready"):
                if body.model:
                    runtime.settings.values["llm.model"] = body.model
                runtime.begin(login=body.login)
        return runtime.snapshot()

    @app.post("/control/stop")
    async def stop():
        if generation.locked():
            return JSONResponse({"detail": "Generation is in progress"}, status_code=409)
        async with generation:
            await runtime.stop()
        return runtime.snapshot()

    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request):
        if (request.method, path) not in (("GET", "models"), ("POST", "chat/completions")):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if runtime.snapshot()["state"] != "ready" or runtime.busy:
            return JSONResponse({"error": {"message": "Google AI Studio is not connected"}}, status_code=503)
        # A single shared gate covers all application lanes, including streaming.
        await generation.acquire()
        client = httpx.AsyncClient(base_url=runtime._endpoint, timeout=360, trust_env=False,
                                   headers={"Authorization": f"Bearer {runtime._key}"})
        upstream = None
        released = False

        async def release():
            nonlocal released
            if released:
                return
            released = True
            with anyio.CancelScope(shield=True):
                try:
                    if upstream:
                        await upstream.aclose()
                    await client.aclose()
                finally:
                    generation.release()

        try:
            # The runtime may have been stopped while this request waited its turn.
            if runtime.snapshot()["state"] != "ready" or runtime.busy:
                await release()
                return JSONResponse({"detail": "Google is reconnecting"}, status_code=503)
            upstream = await client.send(client.build_request(
                request.method, f"/v1/{path}", content=await request.body(),
                headers={"Content-Type": "application/json"},
            ), stream=True)
        except BaseException:
            await release()
            raise

        async def body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await release()

        return StreamingResponse(body(), status_code=upstream.status_code,
                                 media_type=upstream.headers.get("content-type", "application/json"),
                                 background=BackgroundTask(release))

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_service(), host="0.0.0.0", port=7860, access_log=False)
