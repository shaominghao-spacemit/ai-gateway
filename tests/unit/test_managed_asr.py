"""托管会话与资源契约；使用假推理器，真实 flock/子进程另有集成覆盖。"""
import asyncio
from uuid import uuid4

import pytest

from spacemit_ai_gateway.app.settings import AsrConfig
from spacemit_ai_gateway.common.errors import DomainError
from spacemit_ai_gateway.common.sessions import SessionStore
from spacemit_ai_gateway.domains.asr.service import AsrService


@pytest.fixture
async def managed(tmp_path, monkeypatch):
    from spacemit_ai_gateway.domains.asr import managed as module
    lock = tmp_path / 'engine.lock'
    lock.touch(mode=0o660)
    config = AsrConfig(managed={'enabled': True, 'lock_path': str(lock)})
    service = AsrService({}, 'sensevoice', SessionStore(ttl_seconds=300), config=config)
    monkeypatch.setattr(module, 'check_assets', lambda *args: (True, 'assets_verified'))
    monkeypatch.setattr(module, 'foreign_processes', lambda *args: '')
    monkeypatch.setattr(module, 'ManagedBackend', FakeBackend)
    yield service.managed
    await service.shutdown()


class FakeBackend:
    def __init__(self, config, model, selected, fd):
        self.model, self.selected = model, selected
        self.started, self.stopped = asyncio.Event(), asyncio.Event()
        self.finish = asyncio.Event()
        self.finish.set()

    async def start(self):
        pass

    async def healthy(self):
        return not self.stopped.is_set()

    async def recognize(self, audio, selected):
        self.started.set()
        await self.finish.wait()
        return f'{self.model}:{",".join(selected)}'

    async def shutdown(self):
        self.stopped.set()

    @property
    def pid(self):
        return None


async def test_session_exact_target_and_legacy_default(managed):
    owner = str(uuid4())
    result = await managed.acquire(owner, 'qwen3_asr_0.6b', 'zh,en')
    assert result['ready'] and result['resource_state'] == 'owned'
    assert managed.service._default == 'sensevoice'
    other = await managed.status('sensevoice', 'zh')
    assert not other['ready'] and other['owner'] == owner
    wrong = await managed.status('qwen3_asr_0.6b', 'en')
    assert wrong['ready'] and wrong['languages'] == ['en']
    assert await managed.recognize(owner, result['token'], 'zh,en', b'\0\0') == 'qwen3_asr_0.6b:zh,en'
    for bad_owner, token, language in [(str(uuid4()), result['token'], 'zh,en'),
                                       (owner, 'bad', 'zh,en'), (owner, '无效令牌', 'zh,en'),
                                       (owner, result['token'], 'unknown')]:
        with pytest.raises(DomainError):
            await managed.recognize(bad_owner, token, language, b'\0\0')
    with pytest.raises(DomainError):
        await managed.service.load_model('sensevoice')
    with pytest.raises(DomainError):
        await managed.release(str(uuid4()), result['token'])
    assert await managed.release(owner, result['token']) == {'released': True}
    assert (await managed.status('sensevoice', 'zh'))['resource_state'] == 'free'
    with pytest.raises(DomainError):
        await managed.release(owner, result['token'])


async def test_disconnect_release_waits_for_real_inference(managed):
    owner = str(uuid4())
    result = await managed.acquire(owner, 'sensevoice', 'zh')
    backend = managed.backend
    backend.finish.clear()
    request = asyncio.create_task(managed.recognize(owner, result['token'], 'zh', b'\0\0'))
    await backend.started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release = asyncio.create_task(managed.release(owner, result['token']))
    await asyncio.sleep(0)
    assert not release.done() and not backend.stopped.is_set()
    backend.finish.set()
    assert await release == {'released': True}
    assert backend.stopped.is_set()


async def test_old_model_never_unloaded(managed):
    from unittest.mock import AsyncMock
    old = AsyncMock()
    managed.service._backends['sensevoice'] = old
    try:
        with pytest.raises(DomainError):
            await managed.acquire(str(uuid4()), 'qwen3_asr_0.6b', 'zh,en')
        old.shutdown.assert_not_called()
        assert (await managed.status('sensevoice', 'zh'))['reason'] == 'legacy_asr_loaded'
    finally:
        managed.service._backends.clear()


async def test_status_never_loads_and_lists_never_fall_back(managed):
    status = await managed.status('sensevoice', 'zh,en')
    assert status['assets_ready'] and not status['ready']
    assert status['reason'] == 'sensevoice_language_list_unsupported'
    assert managed.backend is None
    with pytest.raises(DomainError):
        await managed.acquire(str(uuid4()), 'sensevoice', 'zh,en')


async def test_start_failure_releases_lock_and_allows_retry(managed, monkeypatch):
    from spacemit_ai_gateway.domains.asr import managed as module
    class Broken(FakeBackend):
        async def start(self):
            raise OSError('start failed')
    monkeypatch.setattr(module, 'ManagedBackend', Broken)
    with pytest.raises(DomainError, match='start_failed'):
        await managed.acquire(str(uuid4()), 'sensevoice', 'zh')
    assert not managed.active
    fd = module.take_lock(managed.config.managed.lock_path)
    import os
    os.close(fd)
    monkeypatch.setattr(module, 'ManagedBackend', FakeBackend)
    result = await managed.acquire(str(uuid4()), 'sensevoice', 'zh')
    assert result['ready']


async def test_shutdown_failure_retains_lock_and_can_retry(managed, monkeypatch):
    from spacemit_ai_gateway.domains.asr.managed import take_lock
    owner = str(uuid4())
    result = await managed.acquire(owner, 'sensevoice', 'zh')
    from unittest.mock import AsyncMock
    original = managed.backend.shutdown
    monkeypatch.setattr(managed.backend, 'shutdown', AsyncMock(side_effect=OSError('busy')))
    with pytest.raises(OSError):
        await managed.release(owner, result['token'])
    assert managed.active
    with pytest.raises(BlockingIOError):
        take_lock(managed.config.managed.lock_path)
    monkeypatch.setattr(managed.backend, 'shutdown', original)
    assert await managed.release(owner, result['token']) == {'released': True}


async def test_lock_conflict_and_unmanaged_process_refused(managed, monkeypatch):
    import os
    from spacemit_ai_gateway.domains.asr import managed as module
    fd = module.take_lock(managed.config.managed.lock_path)
    try:
        assert (await managed.status('sensevoice', 'zh'))['resource_state'] == 'blocked'
        with pytest.raises(DomainError, match='engine_lock_busy'):
            await managed.acquire(str(uuid4()), 'sensevoice', 'zh')
    finally:
        os.close(fd)
    monkeypatch.setattr(module, 'foreign_processes', lambda *args: 'unmanaged_asr_process:123')
    with pytest.raises(DomainError, match='unmanaged_asr_process'):
        await managed.acquire(str(uuid4()), 'sensevoice', 'zh')
    assert managed.backend is None


async def test_cancelled_acquire_waits_start_then_cleans_orphan(managed, monkeypatch):
    from spacemit_ai_gateway.domains.asr import managed as module
    import os
    started, finish = asyncio.Event(), asyncio.Event()
    class Slow(FakeBackend):
        async def start(self):
            started.set()
            await finish.wait()
    monkeypatch.setattr(module, 'ManagedBackend', Slow)
    acquire = asyncio.create_task(managed.acquire(str(uuid4()), 'sensevoice', 'zh'))
    await started.wait()
    backend = managed.backend
    acquire.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquire
    assert managed.active and not backend.stopped.is_set()
    with pytest.raises(BlockingIOError):
        module.take_lock(managed.config.managed.lock_path)
    finish.set()
    while managed.operations:
        await asyncio.gather(*tuple(managed.operations))
        await asyncio.sleep(0)
    assert backend.stopped.is_set() and not managed.active
    os.close(module.take_lock(managed.config.managed.lock_path))


async def test_cancelled_release_keeps_waiting_for_inference(managed):
    owner = str(uuid4())
    acquired = await managed.acquire(owner, 'sensevoice', 'zh')
    token = acquired['token']
    managed.backend.finish.clear()
    request = asyncio.create_task(managed.recognize(owner, token, 'en', b'\0\0'))
    await managed.backend.started.wait()
    release = asyncio.create_task(managed.release(owner, token))
    await asyncio.sleep(0)
    release.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release
    assert managed.active
    managed.backend.finish.set()
    await request
    while managed.operations:
        await asyncio.gather(*tuple(managed.operations))
        await asyncio.sleep(0)
    assert not managed.active


async def test_language_changes_do_not_reload_and_owner_retry_recovers_token(managed):
    owner = str(uuid4())
    acquired = await managed.acquire(owner, 'qwen3_asr_0.6b', 'zh,en')
    backend = managed.backend
    for language in ('en', 'zh', 'auto', 'zh,en'):
        status = await managed.status('qwen3_asr_0.6b', language)
        assert status['ready'] and status['languages'] == language.split(',')
        retry = await managed.acquire(owner, 'qwen3_asr_0.6b', language)
        assert retry['token'] == acquired['token']
        assert await managed.recognize(owner, acquired['token'], language, b'\0\0') == f'qwen3_asr_0.6b:{language}'
        assert managed.backend is backend
    with pytest.raises(DomainError):
        await managed.acquire(str(uuid4()), 'qwen3_asr_0.6b', 'zh')


async def test_two_owners_race_one_wins(managed):
    results = await asyncio.gather(*(managed.acquire(str(uuid4()), 'sensevoice', 'zh')
                                     for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, DomainError) for r in results) == 1


async def test_assets_missing_disabled_and_bad_languages(managed, monkeypatch):
    from spacemit_ai_gateway.domains.asr import managed as module
    managed.config.managed.enabled = False
    assert (await managed.status('sensevoice', 'zh'))['reason'] == 'managed_disabled'
    with pytest.raises(DomainError, match='managed_disabled'):
        await managed.acquire(str(uuid4()), 'sensevoice', 'zh')
    managed.config.managed.enabled = True
    monkeypatch.setattr(module, 'check_assets', lambda *args: (False, 'missing_encoder'))
    assert not (await managed.status('qwen3_asr_0.6b', 'zh,en'))['assets_ready']
    with pytest.raises(DomainError, match='missing_encoder'):
        await managed.acquire(str(uuid4()), 'qwen3_asr_0.6b', 'zh,en')
    for language in ('unknown', 'zh,', 'auto,zh', ''):
        with pytest.raises(DomainError):
            await managed.status('sensevoice', language)


async def test_legacy_unload_cannot_race_acquire(managed):
    from unittest.mock import AsyncMock
    entered, finish = asyncio.Event(), asyncio.Event()
    async def slow_shutdown():
        entered.set()
        await finish.wait()
    old = AsyncMock()
    old.shutdown.side_effect = slow_shutdown
    managed.service._backends['sensevoice'] = old
    managed.service._legacy_asr_used = True
    unload = asyncio.create_task(managed.service.unload_model('sensevoice'))
    await entered.wait()
    acquire = asyncio.create_task(managed.acquire(str(uuid4()), 'sensevoice', 'zh'))
    await asyncio.sleep(0)
    assert not acquire.done()
    finish.set()
    await unload
    with pytest.raises(DomainError, match='legacy_asr_loaded'):
        await acquire


async def test_http_auth_validation_and_session_routes(managed, monkeypatch):
    from fastapi import FastAPI
    import httpx
    from spacemit_ai_gateway.app.settings import Settings
    from spacemit_ai_gateway.domains.asr.api import router
    from spacemit_ai_gateway.gateway import auth
    from spacemit_ai_gateway.gateway.errors import setup_exception_handlers
    from spacemit_ai_gateway.gateway.auth import GatewayAuthMiddleware
    settings = Settings(auth={'enabled': True, 'api_keys': ['test-device-key']})
    monkeypatch.setattr(auth, 'get_settings', lambda: settings)
    app = FastAPI()
    app.state.asr_service = managed.service
    app.include_router(router, prefix='/v1/asr')
    app.add_middleware(GatewayAuthMiddleware)
    setup_exception_handlers(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        query = '/v1/asr/managed/status?model=qwen3_asr_0.6b&language=zh,en'
        assert (await client.get(query)).status_code == 401
        headers = {'Authorization': 'Bearer test-device-key'}
        assert (await client.get(query, headers={'X-API-Key': 'wrong'})).status_code == 403
        status = (await client.get(query, headers=headers)).json()
        assert status['protocol_version'] == 1 and status['languages'] == ['zh', 'en']
        owner = str(uuid4())
        assert (await client.post('/v1/asr/managed/acquire', json={
            'owner': owner, 'model': 'qwen3_asr_0.6b', 'language': 'zh,en'})).status_code == 401
        invalid = await client.post('/v1/asr/managed/acquire', headers=headers, json={
            'owner': 'not-a-uuid', 'model': 'qwen3_asr_0.6b', 'language': 'zh,en'})
        assert invalid.status_code == 422
        acquired = await client.post('/v1/asr/managed/acquire', headers=headers, json={
            'owner': owner, 'model': 'qwen3_asr_0.6b', 'language': 'zh,en'})
        assert acquired.status_code == 200, acquired.text
        token = acquired.json()['token']
        result = await client.post('/v1/asr/managed/recognize', headers=headers,
                                   data={'owner': owner, 'token': token, 'language': 'zh,en'},
                                   files={'file': ('test.pcm', b'\0\0' * 160, 'application/octet-stream')})
        assert result.json() == {'text': 'qwen3_asr_0.6b:zh,en'}
        released = await client.post('/v1/asr/managed/release', headers=headers,
                                    json={'owner': owner, 'token': token})
        assert released.json() == {'released': True}
        assert (await client.get('/v1/asr/managed/status?model=bad&language=zh', headers=headers)).status_code == 422


async def test_acquire_http_disconnect_cleans_unreceived_session(managed, monkeypatch):
    from spacemit_ai_gateway.domains.asr import managed as module
    from spacemit_ai_gateway.domains.asr.api import managed_acquire
    from spacemit_ai_gateway.domains.asr.schemas import ManagedAcquireRequest
    started, finish = asyncio.Event(), asyncio.Event()
    class Slow(FakeBackend):
        async def start(self):
            started.set()
            await finish.wait()
    class Disconnect:
        async def is_disconnected(self):
            return started.is_set()
    monkeypatch.setattr(module, 'ManagedBackend', Slow)
    body = ManagedAcquireRequest(owner=uuid4(), model='qwen3_asr_0.6b', language='zh')
    request = asyncio.create_task(managed_acquire(body, Disconnect(), managed.service))
    await started.wait()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert managed.active
    finish.set()
    while managed.operations:
        await asyncio.gather(*tuple(managed.operations))
        await asyncio.sleep(0)
    assert not managed.active


def free_engine_port():
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_voice_and_gateway_require_participant_marker(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from spacemit_ai_gateway.domains.asr import managed as module
    proc = SimpleNamespace(pid=123, info={'name': 'bibit-audio', 'cmdline': ['/usr/bin/bibit-audio']})
    monkeypatch.setattr(module.psutil, 'process_iter', lambda fields: [proc])
    lock = str(tmp_path / 'engine.lock')
    port = free_engine_port()
    assert module.foreign_processes(None, lock, port) == 'unmanaged_asr_process:123'
    monkeypatch.setattr(module, 'cooperative_participant', lambda *args: True)
    assert module.foreign_processes(None, lock, port) == ''
    proc.info = {'name': 'spacemit-ai-gat', 'cmdline': ['spacemit-ai-gateway']}
    assert module.foreign_processes(None, lock, port) == ''
    # LLM/Embedding 也用 llama-server，按进程名拦会挡住既有服务；只有占用引擎端口才算 ASR 占用。
    proc.info = {'name': 'llama-server', 'cmdline': ['llama-server']}
    assert module.foreign_processes(None, lock, port) == ''


def test_engine_port_occupancy_is_the_ownership_evidence(monkeypatch, tmp_path):
    import os
    import socket
    from spacemit_ai_gateway.domains.asr import managed as module
    lock = str(tmp_path / 'engine.lock')
    port = free_engine_port()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(1)
        assert module.foreign_processes(None, lock, port) == f'unmanaged_asr_engine_port:{port}'
    monkeypatch.setattr(module.psutil, 'process_iter', lambda fields: [])
    # 自己正持有引擎时不能把自家后端判成外来进程。
    assert module.foreign_processes(os.getpid(), lock, port) == ''


async def test_legacy_loading_obeys_lock_even_when_managed_disabled(tmp_path, monkeypatch):
    import os
    from spacemit_ai_gateway.domains.asr import managed as module
    from spacemit_ai_gateway.domains.asr import service as service_module
    from tests.unit.test_asr_service import ReloadableSenseVoiceBackend
    lock = tmp_path / 'engine.lock'
    lock.touch(mode=0o660)
    config = AsrConfig(backends=['sensevoice'], managed={'enabled': False, 'lock_path': str(lock)})
    service = AsrService({}, 'sensevoice', SessionStore(300), config=config)
    monkeypatch.setattr(module, 'foreign_processes', lambda *args: '')
    monkeypatch.setitem(service_module.ASR_REGISTRY, 'sensevoice', ReloadableSenseVoiceBackend)
    external = module.take_lock(str(lock))
    try:
        with pytest.raises(DomainError, match='engine_lock_busy'):
            await service.load_model('sensevoice')
        assert not service._backends
    finally:
        os.close(external)
    await service.load_model('sensevoice')
    await service.unload_model('sensevoice')
    await service.shutdown()
    with pytest.raises(BlockingIOError):
        module.take_lock(str(lock))
    # 测试专用清理；生产不会在 service.shutdown 关闭这个 FD。
    os.close(service._legacy_fd)


async def test_missing_shared_directory_never_loads_legacy(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from spacemit_ai_gateway.domains.asr import service as service_module
    factory = Mock()
    monkeypatch.setitem(service_module.ASR_REGISTRY, 'sensevoice', factory)
    service = AsrService({}, 'sensevoice', SessionStore(300), config=AsrConfig(
        managed={'lock_path': str(tmp_path / 'missing' / 'engine.lock')}))
    with pytest.raises(DomainError, match='engine_lock_unavailable'):
        await service.load_model('sensevoice')
    factory.assert_not_called()
