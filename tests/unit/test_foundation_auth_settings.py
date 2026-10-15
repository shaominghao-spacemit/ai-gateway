"""U2 配置优先级与统一鉴权回归，不依赖真实模型。"""

import base64
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from spacemit_ai_gateway.app.settings import Settings, get_settings, load_yaml_config
from spacemit_ai_gateway.common.sessions import SessionStore
from spacemit_ai_gateway.gateway.errors import setup_exception_handlers


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    import os

    for name in os.environ:
        if name.startswith("SPACEMIT_AI_GATEWAY_"):
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_nested_env_overrides_yaml_without_losing_siblings(tmp_path, monkeypatch):
    config = tmp_path / "base.yaml"
    config.write_text(
        'app:\n  host: 0.0.0.0\n  port: 12345\nauth:\n  enabled: false\n'
        '  api_keys: [yaml-key]\nasr:\n  stream:\n    session_ttl_s: 123\n'
        '    event_queue_size: 17\n', encoding="utf-8",
    )
    monkeypatch.setenv("SPACEMIT_AI_GATEWAY_CONFIG", str(config))
    monkeypatch.setenv("SPACEMIT_AI_GATEWAY_APP__HOST", "127.0.0.2")
    monkeypatch.setenv("SPACEMIT_AI_GATEWAY_AUTH__ENABLED", "true")
    monkeypatch.setenv("SPACEMIT_AI_GATEWAY_AUTH__API_KEYS", '["env-key"]')
    monkeypatch.setenv("SPACEMIT_AI_GATEWAY_ASR__STREAM__SESSION_TTL_S", "456")
    settings = get_settings()
    assert settings.app.host == "127.0.0.2"
    assert settings.app.port == 12345
    assert settings.auth.enabled and settings.auth.api_keys == ["env-key"]
    assert settings.asr.stream.session_ttl_s == 456
    assert settings.asr.stream.event_queue_size == 17


def test_defaults_remain_local_and_source_auth_compatible():
    assert Settings().app.host == "127.0.0.1"
    assert get_settings().app.host == "127.0.0.1"
    assert not get_settings().auth.enabled


def test_explicit_missing_config_fails(tmp_path, monkeypatch):
    path = str(tmp_path / "missing.yaml")
    with pytest.raises(FileNotFoundError, match="missing.yaml"):
        load_yaml_config(path)
    monkeypatch.setenv("SPACEMIT_AI_GATEWAY_CONFIG", path)
    with pytest.raises(FileNotFoundError, match="missing.yaml"):
        get_settings()


@pytest.mark.parametrize("text", ["[", "[]", "false", "null", "", "app:\n  port: invalid\n"])
def test_invalid_explicit_config_fails(tmp_path, monkeypatch, text):
    config = tmp_path / "bad.yaml"
    config.write_text(text, encoding="utf-8")
    monkeypatch.setenv("SPACEMIT_AI_GATEWAY_CONFIG", str(config))
    with pytest.raises(ValueError):
        get_settings()


def make_app(monkeypatch, **auth):
    from spacemit_ai_gateway.gateway.auth import GatewayAuthMiddleware

    settings = Settings(auth={"enabled": True, "api_keys": ["device-key"], **auth})
    monkeypatch.setattr("spacemit_ai_gateway.gateway.auth.get_settings", lambda: settings)
    app = FastAPI()
    setup_exception_handlers(app)
    app.add_middleware(GatewayAuthMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])
    app.state.calls = 0

    @app.api_route("/write", methods=["POST", "PATCH", "PUT", "DELETE"])
    async def write():
        app.state.calls += 1
        return {"ok": True}

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.websocket("/{path:path}")
    async def stream(ws: WebSocket, path: str):
        await ws.accept()
        app.state.calls += 1
        await ws.send_json({"ok": True})
        await ws.close()

    return app


@pytest.mark.parametrize("method", ["post", "patch", "put", "delete"])
def test_write_denied_before_side_effect_and_cors_works(monkeypatch, method):
    app = make_app(monkeypatch)
    with TestClient(app) as client:
        missing = getattr(client, method)("/write", headers={"Origin": "http://console"})
        assert missing.status_code == 401
        assert missing.headers["access-control-allow-origin"] == "*"
        assert getattr(client, method)("/write", headers={"X-API-Key": "wrong"}).status_code == 403
        assert app.state.calls == 0
        assert getattr(client, method)("/write", headers={"X-API-Key": "device-key"}).status_code == 200
        assert getattr(client, method)("/write", headers={"Authorization": "Bearer device-key"}).status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.options("/write", headers={"Origin": "http://console", "Access-Control-Request-Method": "POST"}).status_code == 200
    assert app.state.calls == 2


def test_whitelist_restricts_even_valid_key_and_bypasses_key_inside(monkeypatch, tmp_path):
    whitelist = tmp_path / "whitelist.txt"
    whitelist.write_text("# allowed\n192.0.2.0/24\n", encoding="utf-8")
    app = make_app(monkeypatch, ip_whitelist_file=str(whitelist))
    with TestClient(app, client=("192.0.2.3", 123)) as client:
        assert client.post("/write").status_code == 200
        whitelist.write_text("198.51.100.0/24\n", encoding="utf-8")
        assert client.post("/write", headers={"X-API-Key": "device-key"}).status_code == 403
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/v1/vad/stream", headers={"X-API-Key": "device-key"}):
                pass
        assert exc.value.code == 1008
    app = make_app(monkeypatch, ip_whitelist=["192.0.2.0/24"])
    with TestClient(app, client=("192.0.2.4", 123)) as client:
        assert client.post("/write").status_code == 200


def test_auth_disabled_preserves_http_and_ws(monkeypatch):
    app = make_app(monkeypatch, enabled=False, ip_whitelist=["192.0.2.0/24"])
    with TestClient(app) as client:
        assert client.post("/write").status_code == 200
        with client.websocket_connect("/v1/vad/stream") as ws:
            assert ws.receive_json() == {"ok": True}


@pytest.mark.parametrize("path", ["/v1/asr/stream", "/v1/tts/stream", "/v1/vad/stream", "/v1/vision/stream"])
def test_ws_requires_credentials_and_never_accepts_url_key(monkeypatch, path):
    app = make_app(monkeypatch)
    with TestClient(app) as client:
        for suffix in ("", "?token=device-key&api_key=device-key", "?session_id=forged"):
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(path + suffix):
                    pass
            assert exc.value.code == 1008
        for credential in ("spacemit-api-key.!!!", "spacemit-api-key.d3Jvbmc", "spacemit-api-key._w"):
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(path, subprotocols=["spacemit-gateway", credential]):
                    pass
            assert exc.value.code == 1008
        assert app.state.calls == 0
        protocol = "spacemit-api-key." + base64.urlsafe_b64encode(b"device-key").decode().rstrip("=")
        with client.websocket_connect(path, subprotocols=["spacemit-gateway", protocol]) as ws:
            assert ws.accepted_subprotocol == "spacemit-gateway"
            assert ws.receive_json() == {"ok": True}
        with client.websocket_connect(path, headers={"Authorization": "Bearer device-key"}) as ws:
            assert ws.receive_json() == {"ok": True}


@pytest.mark.parametrize("domain", ["asr", "tts"])
def test_ws_existing_session_is_domain_scoped_and_not_consumed(monkeypatch, domain):
    app = make_app(monkeypatch)
    store = SessionStore(ttl_seconds=60, namespace=domain)
    setattr(app.state, f"{domain}_service", SimpleNamespace(_sessions=store))
    with TestClient(app) as client:
        record = client.portal.call(store.create, {})
        path = f"/v1/{domain}/stream?session_id={record.session_id}"
        with client.websocket_connect(path) as ws:
            assert ws.receive_json() == {"ok": True}
        assert client.portal.call(store.get, record.session_id) is not None
        other = "tts" if domain == "asr" else "asr"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/v1/{other}/stream?session_id={record.session_id}"):
                pass
        record.expires_at = 0
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path):
                pass


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/asr/models/load"), ("PATCH", "/v1/asr/params"),
    ("PATCH", "/v1/tts/engine"), ("POST", "/v1/vad/models/switch"),
    ("POST", "/v1/llm/models/anything/download"), ("POST", "/v1/chat/completions"),
    ("POST", "/v1/embeddings"), ("POST", "/v1/rerank"),
    ("POST", "/v1/vision/models/load"), ("DELETE", "/v1/vision/jobs/anything"),
])
def test_real_entrypoint_blocks_missing_key_before_services(monkeypatch, method, path):
    import spacemit_ai_gateway.app.main as main

    settings = Settings(auth={"enabled": True, "api_keys": ["device-key"]})
    monkeypatch.setattr("spacemit_ai_gateway.gateway.auth.get_settings", lambda: settings)
    # 不启动模型和数据库；未授权请求不得触及任何服务。
    client = TestClient(main.app)
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("domain", ["asr", "tts"])
def test_authenticated_session_works_through_real_stream_handlers(monkeypatch, domain, asr_service, tts_service):
    from spacemit_ai_gateway.domains.asr import api as asr_api, stream as asr_stream
    from spacemit_ai_gateway.domains.tts import api as tts_api, stream as tts_stream
    from spacemit_ai_gateway.gateway.auth import GatewayAuthMiddleware

    settings = Settings(auth={"enabled": True, "api_keys": ["device-key"]})
    monkeypatch.setattr("spacemit_ai_gateway.gateway.auth.get_settings", lambda: settings)
    app = FastAPI()
    setup_exception_handlers(app)
    app.add_middleware(GatewayAuthMiddleware)
    service, api, stream, handler_cls = (
        (asr_service, asr_api, asr_stream, asr_stream.AsrStreamHandler) if domain == "asr"
        else (tts_service, tts_api, tts_stream, tts_stream.TtsStreamHandler)
    )
    setattr(app.state, f"{domain}_service", service)
    setattr(app.state, f"{domain}_stream_handler", handler_cls(service))
    app.include_router(api.router, prefix=f"/v1/{domain}")
    app.include_router(stream.router, prefix=f"/v1/{domain}")
    with TestClient(app) as client:
        response = client.post(f"/v1/{domain}/stream/session", json={}, headers={"Authorization": "Bearer device-key"})
        assert response.status_code == 200
        path = f"/v1/{domain}/stream?session_id={response.json()['session_id']}"
        with client.websocket_connect(path) as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "end"})
        # 域处理器仍原子消费一次性 session。
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path):
                pass


def test_console_all_request_types_carry_key_without_url_or_external_leak():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable; console runtime check requires Node.js")
    api_path = Path(__file__).resolve().parents[2] / "frontend/console/src/api.js"
    script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const base = 'http://127.0.0.1:18790';
const calls = [];
global.location = { href: 'http://127.0.0.1:18792/' };
global.window = { API_BASES: Object.fromEntries(['asr', 'tts', 'vad', 'vision', 'llm', 'vlm'].map(d => [d, base])) };
global.fetch = async (url, opts) => {
  calls.push({url, opts});
  if (opts.body && typeof opts.body === 'string' && JSON.parse(opts.body).stream) {
    return new Response('data: [DONE]\n\n', {headers: {'content-type': 'text/event-stream'}});
  }
  return new Response('{}', {headers: {'content-type': 'application/json'}});
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  window.gatewayAuth.setKey(' device-key ');
  const {asrApi, ttsApi, vadApi, visionApi, llmApi, vlmApi, systemApi} = window;
  const blob = new Blob(['data']);
  await asrApi.listModels();
  await asrApi.updateParams({language: 'en'});
  await asrApi.recognize(blob);
  await asrApi.createSession({});
  await ttsApi.synthesize({text: 'hello'});
  await ttsApi.getTaskAudio('task');
  await ttsApi.createStreamSession({});
  await vadApi.analyze(blob);
  await vadApi.segments(blob);
  await visionApi.inference(blob, []);
  await visionApi.feature(blob, 'face');
  await visionApi.loadModel('yolo11n');
  await llmApi.chat({messages: []});
  for await (const chunk of llmApi.chatStream({messages: []})) {}
  for await (const chunk of vlmApi.chatStream({messages: []})) {}
  await systemApi.stats();
  assert.equal(calls.length, 16);
  for (const {url, opts} of calls) {
    assert.equal(opts.headers.get('X-API-Key'), 'device-key');
    assert.ok(!url.includes('device-key'));
    if (opts.body instanceof FormData) assert.ok(!opts.headers.has('Content-Type'));
  }
  const protocols = window.gatewayAuth.wsProtocols();
  assert.equal(Buffer.from(protocols[1].split('.')[1], 'base64url').toString(), 'device-key');
  for (const api of [asrApi, ttsApi, vadApi, visionApi]) {
    assert.ok(!api.streamUrl().includes('device-key'));
  }
  await window.gatewayFetch('https://unrelated.invalid/audio');
  assert.equal(calls.at(-1).opts.headers.get('X-API-Key'), null);
  window.gatewayAuth.setKey('');
  await asrApi.listModels();
  assert.equal(calls.at(-1).opts.headers.get('X-API-Key'), null);
  assert.deepEqual(window.gatewayAuth.wsProtocols(), []);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, str(api_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("enabled", [True, False])
def test_browser_ws_negotiates_non_secret_protocol_over_real_socket(monkeypatch, enabled):
    import uvicorn

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable; real browser-compatible WS check requires Node.js")
    app = make_app(monkeypatch, enabled=enabled)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "test server did not start"
            script = r"""
const assert = require('node:assert/strict');
const ws = new WebSocket(process.argv[1], ['spacemit-gateway', 'spacemit-api-key.ZGV2aWNlLWtleQ']);
const timer = setTimeout(() => { console.error('WS timed out'); process.exit(1); }, 3000);
ws.onmessage = event => {
  assert.equal(ws.protocol, 'spacemit-gateway');
  assert.deepEqual(JSON.parse(event.data), {ok: true});
  clearTimeout(timer);
  ws.close();
};
ws.onerror = event => { console.error(event.message); clearTimeout(timer); process.exitCode = 1; };
"""
            result = subprocess.run([node, "-e", script, f"ws://127.0.0.1:{port}/v1/vad/stream"], capture_output=True, text=True, timeout=5)
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "test server failed to stop"
