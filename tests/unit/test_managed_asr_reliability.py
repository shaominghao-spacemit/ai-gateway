"""旧加载拒绝不占用 SDK 寿命；所有锁、缓存和参与者均使用私有路径。"""
import asyncio
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from spacemit_ai_gateway.app.settings import AsrConfig
from spacemit_ai_gateway.common.errors import DomainError, ModelUnknown
from spacemit_ai_gateway.common.sessions import SessionStore
from spacemit_ai_gateway.domains.asr import managed as module, service as service_module
from spacemit_ai_gateway.domains.asr.service import AsrService
from tests.unit.test_asr_service import ReloadableSenseVoiceBackend
from tests.unit.test_managed_asr import FakeBackend


@pytest.fixture
async def legacy_service(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    participants = {}
    monkeypatch.setattr(module, '_PARTICIPANTS', participants)
    lock = tmp_path / 'engine.lock'
    lock.touch(mode=0o660)
    config = AsrConfig(backends=['sensevoice'], managed={'enabled': True, 'lock_path': str(lock)})
    service = AsrService({}, 'sensevoice', SessionStore(300), config=config)
    monkeypatch.setattr(module, 'check_assets', lambda *_: (True, 'assets_verified'))
    monkeypatch.setattr(module, 'foreign_processes', lambda *_: '')
    monkeypatch.setattr(module, 'ManagedBackend', FakeBackend)
    monkeypatch.setitem(service_module.ASR_REGISTRY, 'sensevoice', ReloadableSenseVoiceBackend)
    monkeypatch.setattr(service_module, 'sdk_installed', lambda _: False)
    try:
        yield service
    finally:
        await service.shutdown()
        if service._legacy_fd is not None:
            os.close(service._legacy_fd)
        for fd, path in participants.values():
            os.close(fd)
            os.unlink(path)


@pytest.mark.parametrize(
    'failure', ['unknown', 'unregistered', 'lock', 'foreign', 'foreign_error', 'download', 'cancel']
)
async def test_rejected_legacy_load_can_acquire_managed(legacy_service, monkeypatch, failure):
    service = legacy_service
    model = 'sensevoice'
    held_fd = None
    error = DomainError
    if failure == 'unknown':
        model, error = 'unknown-model', ModelUnknown
    elif failure == 'unregistered':
        monkeypatch.delitem(service_module.ASR_REGISTRY, model)
        error = ModelUnknown
    elif failure == 'lock':
        held_fd = module.take_lock(service._config.managed.lock_path)
    elif failure == 'foreign':
        monkeypatch.setattr(module, 'foreign_processes', lambda *_: 'unmanaged_asr_process:123')
    elif failure == 'foreign_error':
        def foreign_error(*_):
            raise OSError('foreign check failed')
        monkeypatch.setattr(module, 'foreign_processes', foreign_error)
        error = OSError
    else:
        monkeypatch.setattr(service_module, 'sdk_installed', lambda _: True)
        error = asyncio.CancelledError if failure == 'cancel' else RuntimeError
        monkeypatch.setattr(service.downloads, 'ensure', AsyncMock(side_effect=error('download failed')))
    observed = []
    class NeverConstruct(ReloadableSenseVoiceBackend):
        def __init__(self, config):
            observed.append('constructed')
            super().__init__(config)
    if failure != 'unregistered':
        monkeypatch.setitem(service_module.ASR_REGISTRY, 'sensevoice', NeverConstruct)
    try:
        with pytest.raises(error):
            await service.load_model(model)
    finally:
        if held_fd is not None:
            os.close(held_fd)
    assert observed == [] and not service._backends
    assert not service._legacy_asr_used
    assert service._legacy_fd is None
    monkeypatch.setattr(module, 'foreign_processes', lambda *_: '')
    result = await service.managed.acquire(str(uuid4()), 'sensevoice', 'auto', 'secret-' + 'x' * 32)
    assert result['ready'] and result['resource_state'] == 'owned'


@pytest.mark.parametrize('failure', ['foreign', 'download'])
async def test_preconstruction_checks_do_not_mark_or_retain_new_fd(legacy_service, monkeypatch, failure):
    service = legacy_service
    observed = []
    def foreign(*_):
        observed.append(service._legacy_asr_used)
        assert service._legacy_fd is not None
        return 'unmanaged_asr_process:123' if failure == 'foreign' else ''
    async def download(_):
        observed.append(service._legacy_asr_used)
        raise RuntimeError('download failed')
    monkeypatch.setattr(module, 'foreign_processes', foreign)
    monkeypatch.setattr(service_module, 'sdk_installed', lambda _: True)
    monkeypatch.setattr(service.downloads, 'ensure', download)
    with pytest.raises(DomainError if failure == 'foreign' else RuntimeError):
        await service.load_model('sensevoice')
    assert observed == ([False] if failure == 'foreign' else [False, False])
    assert service._legacy_fd is None and not service._legacy_asr_used
    os.close(module.take_lock(service._config.managed.lock_path))


@pytest.mark.parametrize('failure', ['construct', 'warmup', 'loaded'])
async def test_legacy_sdk_lifetime_retains_marker_and_lock(legacy_service, monkeypatch, failure):
    service = legacy_service
    observed = []
    class Backend(ReloadableSenseVoiceBackend):
        def __init__(self, config):
            observed.append(('construct', service._legacy_asr_used, service._legacy_fd))
            if failure == 'construct':
                raise RuntimeError('construct failed')
            super().__init__(config)

        async def warmup(self):
            if failure == 'warmup':
                raise RuntimeError('warmup failed')

        async def shutdown(self):
            observed.append(('shutdown', service._legacy_asr_used, service._legacy_fd))
    monkeypatch.setitem(service_module.ASR_REGISTRY, 'sensevoice', Backend)
    if failure == 'loaded':
        assert (await service.load_model('sensevoice'))['loaded']
        with pytest.raises(DomainError, match='legacy_asr_loaded'):
            await service.managed.acquire(str(uuid4()), 'sensevoice', 'auto', 'secret-' + 'x' * 32)
        await service.unload_model('sensevoice')
    else:
        with pytest.raises(RuntimeError, match=f'{failure} failed'):
            await service.load_model('sensevoice')
    fd = service._legacy_fd
    assert fd is not None and service._legacy_asr_used and not service._backends
    assert observed == [('construct', True, fd)] + ([] if failure == 'construct' else [('shutdown', True, fd)])
    await service.shutdown()
    assert service._legacy_fd == fd
    os.fstat(fd)
    with pytest.raises(BlockingIOError):
        module.take_lock(service._config.managed.lock_path)
    with pytest.raises(DomainError, match='legacy_asr_loaded'):
        await service.managed.acquire(str(uuid4()), 'sensevoice', 'auto', 'secret-' + 'x' * 32)


@pytest.mark.parametrize('failure', ['foreign', 'download'])
async def test_later_preconstruction_failure_preserves_prior_sdk_lock(legacy_service, monkeypatch, failure):
    service = legacy_service
    await service.load_model('sensevoice')
    fd = service._legacy_fd
    if failure == 'foreign':
        monkeypatch.setattr(module, 'foreign_processes', lambda *_: 'unmanaged_asr_process:123')
    else:
        monkeypatch.setattr(service_module, 'sdk_installed', lambda _: True)
        monkeypatch.setattr(service.downloads, 'ensure', AsyncMock(side_effect=RuntimeError('download failed')))
    with pytest.raises(DomainError if failure == 'foreign' else RuntimeError):
        await service._ensure_backend('sensevoice', force_reload=True)
    assert service._legacy_fd == fd and service._legacy_asr_used
    assert 'sensevoice' in service._backends
    os.fstat(fd)
    with pytest.raises(BlockingIOError):
        module.take_lock(service._config.managed.lock_path)
