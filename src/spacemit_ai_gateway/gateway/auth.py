"""统一 HTTP / WebSocket 鉴权，保留 IP 白名单与 ASR/TTS 会话兼容。"""

import base64
import binascii
import grp
import hmac
import ipaddress
import os
import stat
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..app.settings import get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_WS_KEY_PREFIX = "spacemit-api-key."
VOICE_API_KEY_FILE = Path("/etc/spacemit-ai-gateway/voice-api-key")
VOICE_API_KEY_OWNER = 0
VOICE_API_KEY_GROUP = "bibit-asr"


def _voice_api_key() -> Optional[str]:
    """只接受安装器发布的不可跟随、固定身份和权限的单个 ASCII Key。"""
    try:
        expected_gid = grp.getgrnam(VOICE_API_KEY_GROUP).gr_gid
    except KeyError:
        return None
    try:
        fd = os.open(VOICE_API_KEY_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError(f"Voice API key file is not usable: {error.strerror}") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != VOICE_API_KEY_OWNER
                or info.st_gid != expected_gid or stat.S_IMODE(info.st_mode) != 0o640):
            raise RuntimeError("Voice API key file identity or mode is invalid")
        if info.st_size < 1 or info.st_size > 4097:
            raise RuntimeError("Voice API key file size is invalid")
        chunks = []
        remaining = 4097
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if data.endswith(b"\n"):
        data = data[:-1]
    if len(data) > 4096 or not data or any(byte < 0x21 or byte > 0x7e for byte in data):
        raise RuntimeError("Voice API key file content is invalid")
    return data.decode("ascii")


def _is_managed_asr_path(path: str) -> bool:
    # 精确路由白名单：前缀匹配会放行 managed-evil 等未声明路径。
    return path in {
        "/v1/asr/managed/status", "/v1/asr/managed/acquire",
        "/v1/asr/managed/release", "/v1/asr/managed/recognize",
    }


def _load_whitelist_file(filepath: str) -> list[str]:
    p = Path(filepath)
    if not p.is_file():
        return []
    return [
        line.strip() for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _is_ip_whitelisted(client_ip: str, settings) -> bool:
    entries = list(settings.auth.ip_whitelist)
    if settings.auth.ip_whitelist_file:
        entries.extend(_load_whitelist_file(settings.auth.ip_whitelist_file))
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _connection_key(connection: HTTPConnection) -> Optional[str]:
    key = connection.headers.get("X-API-Key")
    if key:
        return key
    scheme, _, value = connection.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    if connection.scope["type"] == "websocket":
        for protocol in connection.scope.get("subprotocols", []):
            if protocol.startswith(_WS_KEY_PREFIX):
                encoded = protocol[len(_WS_KEY_PREFIX):]
                try:
                    return base64.b64decode(
                        encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
                    ).decode("utf-8")
                except (ValueError, binascii.Error, UnicodeDecodeError):
                    return None
    return None


def _key_error(api_key: Optional[str], settings, path: str = "") -> Optional[tuple[int, dict]]:
    if not api_key:
        return 401, {"error": "unauthorized", "message": "Missing API key"}
    try:
        encoded = api_key.encode()
    except UnicodeEncodeError:
        return 403, {"error": "forbidden", "message": "Invalid API key"}
    if any(hmac.compare_digest(encoded, key.encode()) for key in settings.auth.api_keys):
        return None
    if not _is_managed_asr_path(path):
        return 403, {"error": "forbidden", "message": "Invalid API key"}
    try:
        voice_key = _voice_api_key()
    except RuntimeError:
        # 损坏的 Voice Key 不能让管理员认证或普通拒绝路径变成 500。
        return 403, {"error": "forbidden", "message": "Invalid API key"}
    if voice_key and hmac.compare_digest(encoded, voice_key.encode()):
        return None
    return 403, {"error": "forbidden", "message": "Invalid API key"}


async def verify_api_key(
    request: Request,
    api_key: Optional[str] = Depends(api_key_header),
) -> Optional[str]:
    settings = get_settings()
    if not settings.auth.enabled:
        return None
    if _is_ip_whitelisted(request.client.host if request.client else "", settings):
        return None
    api_key = api_key or _connection_key(request)
    error = _key_error(api_key, settings, request.url.path)
    if error:
        raise HTTPException(status_code=error[0], detail=error[1])
    return api_key


async def _has_stream_session(connection: HTTPConnection) -> bool:
    domain = {
        "/v1/asr/stream": "asr", "/v1/tts/stream": "tts",
    }.get(connection.scope["path"])
    session_id = connection.query_params.get("session_id")
    if not domain or not session_id:
        return False
    service = getattr(connection.app.state, f"{domain}_service", None)
    # 仅查询现有存储；消费 session 仍由域服务的 open_stream 原子完成。
    store = getattr(service, "_sessions", None)
    return store is not None and await store.get(session_id) is not None


class GatewayAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket" and "spacemit-gateway" in scope.get("subprotocols", []):
            original_send = send

            async def send_with_protocol(message: Message) -> None:
                if message["type"] == "websocket.accept" and not message.get("subprotocol"):
                    message = {**message, "subprotocol": "spacemit-gateway"}
                await original_send(message)

            send = send_with_protocol
        settings = get_settings()
        if not settings.auth.enabled or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        whitelisted = _is_ip_whitelisted(connection.client.host if connection.client else "", settings)
        error = None
        if (settings.auth.ip_whitelist or settings.auth.ip_whitelist_file) and not whitelisted:
            error = 403, {"error": "forbidden", "message": "Client IP not allowed"}
        elif not whitelisted and (
            scope["type"] == "websocket" or scope["method"] not in {"GET", "HEAD"}
        ):
            if scope["type"] != "websocket" or not await _has_stream_session(connection):
                error = _key_error(_connection_key(connection), settings, scope.get("path", ""))

        if error:
            # middleware 位于异常处理器外层，直接响应，不能抛 HTTPException。
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": error[1]["message"]})
            else:
                await JSONResponse(status_code=error[0], content=error[1])(scope, receive, send)
            return
        await self.app(scope, receive, send)
