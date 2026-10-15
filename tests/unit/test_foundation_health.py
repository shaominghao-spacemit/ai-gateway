"""全局健康探针的八域汇总与故障隔离回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from spacemit_ai_gateway.gateway import health


DOMAINS = ("asr", "tts", "vad", "llm", "embed", "rerank", "vlm", "vision")


@pytest.fixture
def probe_app(monkeypatch):
    app = FastAPI()
    app.include_router(health.router)
    for name in DOMAINS[:-1]:
        setattr(
            app.state,
            f"{name}_service",
            SimpleNamespace(healthz=AsyncMock(return_value={
                "ready": True, "state": "ready", "backend": name,
            })),
        )
    monkeypatch.setattr(health, "vision_api", SimpleNamespace(
        domain_health_summary=Mock(return_value={
            "ready": True, "state": "ready", "backend": "vision",
        }),
    ))
    return app


def set_probe(app, name, **kwargs):
    if name == "vision":
        probe = Mock(**kwargs)
        health.vision_api.domain_health_summary = probe
    else:
        probe = AsyncMock(**kwargs)
        getattr(app.state, f"{name}_service").healthz = probe
    return probe


async def get_health(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    return response.json()


async def test_all_eight_domains_preserve_health_details(probe_app):
    data = await get_health(probe_app)
    assert data["status"] == "ok"
    assert set(data["domains"]) == set(DOMAINS)
    for name in DOMAINS:
        assert data["domains"][name] == {
            "ready": True, "state": "ready", "backend": name,
        }


@pytest.mark.parametrize("name", DOMAINS)
@pytest.mark.parametrize("state,ready", [
    ("degraded", True),
    ("degraded", False),
    ("failed", True),
    ("failed", False),
    ("error", False),
    ("initializing", False),
    ("warming_up", False),
    ("uninitialized", False),
    ("ready", False),
    ("unknown", True),
])
async def test_unhealthy_domain_degrades_gateway(probe_app, name, state, ready):
    info = {"ready": ready, "state": state, "backend": name}
    set_probe(probe_app, name, return_value=info)
    data = await get_health(probe_app)
    assert data["status"] == "degraded"
    assert data["domains"][name] == info
    assert set(data["domains"]) == set(DOMAINS)


@pytest.mark.parametrize("state", ["idle", "unloaded"])
async def test_lazy_domains_are_healthy_without_claiming_model_readiness(probe_app, state):
    for name in DOMAINS:
        set_probe(probe_app, name, return_value={"ready": False, "state": state})
    data = await get_health(probe_app)
    assert data["status"] == "ok"
    assert set(data["domains"]) == set(DOMAINS)
    assert all(info == {"ready": False, "state": state} for info in data["domains"].values())


@pytest.mark.parametrize("name", DOMAINS)
async def test_missing_domain_degrades_gateway(probe_app, monkeypatch, name):
    if name == "vision":
        monkeypatch.setattr(health, "vision_api", None)
    else:
        delattr(probe_app.state, f"{name}_service")
    data = await get_health(probe_app)
    assert data["status"] == "degraded"
    assert data["domains"][name] == {"ready": False, "state": "uninitialized"}
    assert set(data["domains"]) == set(DOMAINS)


@pytest.mark.parametrize("name", DOMAINS)
async def test_probe_failure_is_reported_and_can_recover(probe_app, name):
    probe = set_probe(probe_app, name, side_effect=RuntimeError("private backend details"))
    data = await get_health(probe_app)
    assert data["status"] == "degraded"
    assert data["domains"][name]["state"] == "failed"
    assert data["domains"][name]["ready"] is False
    assert data["domains"][name]["error"] == "health_check_failed"
    assert "private backend details" not in str(data)
    assert set(data["domains"]) == set(DOMAINS)
    probe.side_effect = None
    probe.return_value = {"ready": True, "state": "ready"}
    assert (await get_health(probe_app))["status"] == "ok"


@pytest.mark.parametrize("name", DOMAINS)
@pytest.mark.parametrize("info", [None, [], {}, {"ready": "true", "state": "ready"}])
async def test_invalid_probe_result_degrades_gateway(probe_app, name, info):
    set_probe(probe_app, name, return_value=info)
    data = await get_health(probe_app)
    assert data["status"] == "degraded"
    assert data["domains"][name]["state"] == "failed"
    assert data["domains"][name]["ready"] is False
    assert set(data["domains"]) == set(DOMAINS)


@pytest.mark.parametrize("states,expected_state,expected_ready", [
    (("ready", "error"), "degraded", False),
    (("error", "ready"), "degraded", False),
    (("unloaded", "error"), "degraded", False),
    (("loading", "error"), "degraded", False),
    (("unloaded", "loading"), "loading", False),
    (("loading", "unloaded"), "loading", False),
    (("ready", "unloaded"), "ready", True),
    (("unloaded", "ready"), "ready", True),
    (("ready", "loading"), "ready", True),
    (("unloaded",), "unloaded", False),
])
async def test_real_vision_summary_preserves_model_failures(
    probe_app, monkeypatch, states, expected_state, expected_ready,
):
    from spacemit_ai_gateway.domains.vision import api as vision_api
    from spacemit_ai_gateway.domains.vision.models import ManagedModel, ModelRegistry
    from spacemit_ai_gateway.domains.vision.schemas import ModelInfo

    registry = ModelRegistry(adapter=Mock())
    for index, state in enumerate(states):
        model_id = f"health-model-{index}"
        registry._models[model_id] = ManagedModel(info=ModelInfo(
            model_id=model_id, status=state, backend=f"backend-{state}",
        ))
    monkeypatch.setattr(vision_api, "_registry", registry)
    monkeypatch.setattr(health, "vision_api", vision_api)

    data = await get_health(probe_app)

    expected_backend_state = "error" if expected_state == "degraded" else expected_state
    assert data["domains"]["vision"] == {
        "ready": expected_ready,
        "state": expected_state,
        "backend": f"backend-{expected_backend_state}",
    }
    assert data["status"] == (
        "ok" if expected_state in {"ready", "unloaded"} else "degraded"
    )
    assert set(data["domains"]) == set(DOMAINS)
