"""真实 VAD 懒加载失败、健康探针与重试恢复回归。"""

import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from spacemit_ai_gateway.app.settings import VadConfig
from spacemit_ai_gateway.common.errors import ModelLoadFailed, ModelUnknown
from spacemit_ai_gateway.common.ready_state import BackendReadyState
from spacemit_ai_gateway.domains.vad import api as vad_api
from spacemit_ai_gateway.domains.vad import service as vad_module
from spacemit_ai_gateway.domains.vad.adapters.silero import SileroBackend
from spacemit_ai_gateway.domains.vision import api as vision_api
from spacemit_ai_gateway.domains.vision.models import ManagedModel, ModelRegistry
from spacemit_ai_gateway.domains.vision.schemas import ModelInfo
from spacemit_ai_gateway.gateway import health
from spacemit_ai_gateway.gateway.errors import setup_exception_handlers


MODEL = "health-test-vad"


class IdleDomain:
    async def healthz(self):
        return {"ready": False, "state": "idle"}


@pytest.fixture
def load_health_case(monkeypatch, fake_vad_backend):
    class RetryBackend(type(fake_vad_backend)):
        failure = None
        attempts = 0

        def __init__(self, config):
            type(self).attempts += 1
            self._state = BackendReadyState.INITIALIZING
            if self.failure == "constructor":
                raise RuntimeError("test VAD load failed")

        @property
        def state(self):
            return self._state

        async def warmup(self):
            self._state = BackendReadyState.WARMING_UP
            if self.failure == "warmup":
                raise RuntimeError("test VAD load failed")
            self._state = BackendReadyState.READY

    monkeypatch.setitem(vad_module.VAD_REGISTRY, MODEL, RetryBackend)
    service = vad_module.VadService(
        {}, MODEL, config=VadConfig(backend=MODEL, backends=[MODEL]),
    )
    app = FastAPI()
    app.include_router(health.router)
    app.include_router(vad_api.router, prefix="/v1/vad")
    app.state.vad_service = service
    for name in ("asr", "tts", "llm", "embed", "rerank", "vlm"):
        setattr(app.state, f"{name}_service", IdleDomain())
    registry = ModelRegistry(adapter=object())
    registry._models["health-test-vision"] = ManagedModel(info=ModelInfo(
        model_id="health-test-vision", status="unloaded",
    ))
    monkeypatch.setattr(vision_api, "_registry", registry)
    monkeypatch.setattr(health, "vision_api", vision_api)
    return service, RetryBackend, app


async def assert_health(service, app, state, overall):
    info = await service.healthz()
    assert info["state"] == state
    assert info["ready"] is (state in {"ready", "degraded"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        domain = await client.get("/v1/vad/healthz")
        aggregate = await client.get("/healthz")
    assert domain.status_code == aggregate.status_code == 200
    assert domain.json() == info
    assert aggregate.json()["domains"]["vad"] == info
    assert aggregate.json()["status"] == overall


@pytest.mark.parametrize("failure", ["constructor", "warmup"])
@pytest.mark.parametrize("entrypoint", [
    "load_model", "switch_default", "analyze", "segment", "open_stream",
])
async def test_actual_load_failure_degrades_health_until_retry_succeeds(
    load_health_case, failure, entrypoint,
):
    service, backend_type, app = load_health_case
    await assert_health(service, app, "idle", "ok")
    backend_type.failure = failure
    method = getattr(service, entrypoint)
    if entrypoint in {"load_model", "switch_default"}:
        args = (MODEL,)
    elif entrypoint == "open_stream":
        args = (16000,)
    else:
        args = (b"\x00" * 3200, 16000)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="test VAD load failed"):
            await method(*args)
        assert service.get_models()[0].loaded is False
        await assert_health(service, app, "failed", "degraded")

    with pytest.raises(ModelUnknown):
        await service.load_model("unknown-vad")
    await assert_health(service, app, "failed", "degraded")

    backend_type.failure = None
    await method(*args)
    assert backend_type.attempts == 3
    assert service.get_models()[0].loaded is True
    await assert_health(service, app, "ready", "ok")

    await service.unload_model(MODEL)
    await assert_health(service, app, "idle", "ok")


async def test_invalid_model_request_keeps_initial_idle_healthy(load_health_case):
    service, backend_type, app = load_health_case
    with pytest.raises(ModelUnknown):
        await service.load_model("unknown-vad")
    assert backend_type.attempts == 0
    await assert_health(service, app, "idle", "ok")


async def test_missing_vad_sdk_stays_degraded_after_warmup(
    load_health_case, monkeypatch,
):
    _, _, app = load_health_case
    monkeypatch.setitem(sys.modules, "spacemit_vad", None)
    service = vad_module.VadService(
        {}, "silero", config=VadConfig(backend="silero", backends=["silero"]),
    )
    app.state.vad_service = service
    await assert_health(service, app, "idle", "ok")

    result = await service.load_model("silero")

    assert isinstance(service.backend, SileroBackend)
    assert service.backend.backend_name == "silero (mock)"
    assert result["state"] == "degraded"
    await assert_health(service, app, "degraded", "degraded")
    await service.backend.warmup()
    await assert_health(service, app, "degraded", "degraded")


async def test_silero_with_sdk_becomes_ready_after_warmup(monkeypatch):
    engine = SimpleNamespace(is_initialized=True)
    monkeypatch.setitem(sys.modules, "spacemit_vad", SimpleNamespace(
        VadConfig=SimpleNamespace(preset=lambda name: SimpleNamespace()),
        VadEngine=lambda config: engine,
    ))
    backend = SileroBackend(VadConfig())
    assert backend.state == BackendReadyState.WARMING_UP
    assert backend._engine is engine
    assert backend.backend_name == "silero"

    await backend.warmup()

    assert backend.state == BackendReadyState.READY


async def test_sdk_silent_initialization_failure_degrades_health(load_health_case, monkeypatch):
    _, _, app = load_health_case
    monkeypatch.setitem(sys.modules, "spacemit_vad", SimpleNamespace(
        VadConfig=SimpleNamespace(preset=lambda name: SimpleNamespace()),
        VadEngine=lambda config: SimpleNamespace(is_initialized=False),
    ))
    service = vad_module.VadService(
        {}, "silero", config=VadConfig(backend="silero", backends=["silero"]),
    )
    monkeypatch.setattr(service.downloads, "supports", lambda name: False)
    app.state.vad_service = service
    setup_exception_handlers(app)

    with pytest.raises(ModelLoadFailed, match="not initialized"):
        await service.load_model("silero")

    await assert_health(service, app, "failed", "degraded")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post("/v1/vad/models/load", json={"model_id": "silero"})
    assert response.status_code == 503
    assert response.json()["error"] == "load_failed"
    await assert_health(service, app, "failed", "degraded")
