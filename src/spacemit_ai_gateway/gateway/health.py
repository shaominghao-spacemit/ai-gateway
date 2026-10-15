"""全局健康检查（聚合各域状态）。"""

import logging

from fastapi import APIRouter, Request

from ..common.ready_state import BackendReadyState

try:
    from ..domains.vision import api as vision_api
except ImportError:
    vision_api = None  # type: ignore[assignment]

router = APIRouter()
logger = logging.getLogger(__name__)

_LAZY_READY_STATES = {BackendReadyState.IDLE.value, "unloaded"}


@router.get("/healthz", tags=["Health"])
async def healthz(request: Request):
    domains: dict[str, dict] = {}
    overall_ready = True

    for name in ("asr", "tts", "vad", "llm", "embed", "rerank", "vlm", "vision"):
        info = {"ready": False, "state": "uninitialized"}
        try:
            if name == "vision":
                if vision_api is not None:
                    info = vision_api.domain_health_summary()
            else:
                svc = getattr(request.app.state, f"{name}_service", None)
                if svc is not None:
                    info = await svc.healthz()

            if (
                not isinstance(info, dict)
                or not isinstance(info.get("ready"), bool)
                or not isinstance(info.get("state"), str)
            ):
                raise ValueError("invalid health summary")
        except Exception:
            logger.exception("健康探针失败：%s", name)
            info = {"ready": False, "state": "failed", "error": "health_check_failed"}

        domains[name] = info
        # degraded 后端也可能 ready=True；服务在线不代表模型真实就绪。
        if info["state"] not in _LAZY_READY_STATES and not (
            info["state"] == BackendReadyState.READY.value and info["ready"]
        ):
            overall_ready = False

    return {
        "status": "ok" if overall_ready else "degraded",
        "domains": domains,
    }
