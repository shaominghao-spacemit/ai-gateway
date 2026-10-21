"""执行实际 worker 协议循环，核对仅有固定语言能力的 SDK。"""
import asyncio
import base64
import io
import json
import os
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from spacemit_ai_gateway.common.ready_state import BackendReadyState
from spacemit_ai_gateway.domains.asr import managed_worker
from spacemit_ai_gateway.app.settings import AsrConfig
from spacemit_ai_gateway.common.errors import DomainError
from spacemit_ai_gateway.common.sessions import SessionStore
from spacemit_ai_gateway.domains.asr import managed as managed_module
from spacemit_ai_gateway.domains.asr.managed_backend import ManagedBackend
from spacemit_ai_gateway.domains.asr.service import AsrService


@pytest.mark.asyncio
@pytest.mark.parametrize('dynamic', [False, True])
async def test_fixed_language_engine_works_and_language_change_is_explicit(monkeypatch, dynamic):
    calls = []

    class Backend:
        state = BackendReadyState.READY
        _mock = False

        def __init__(self, config):
            self._engine = SimpleNamespace()
            if dynamic:
                self._engine.set_language = lambda language: calls.append(('language', language))

        async def warmup(self):
            pass

        async def recognize(self, audio, sample_rate, language, punctuation):
            calls.append(('recognize', language))
            assert audio == b'\x00\x00' and sample_rate == 16000
            return SimpleNamespace(text='recognized')

        async def shutdown(self):
            calls.append(('shutdown',))

    class Output(io.StringIO):
        def close(self):
            pass

    output = Output()
    audio = base64.b64encode(b'\x00\x00').decode()
    requests = [
        {'op': 'init', 'config': {'language': 'auto'}},
        {'op': 'recognize', 'languages': ['auto'], 'audio': audio},
        {'op': 'recognize', 'languages': ['zh'], 'audio': audio},
        {'op': 'health'},
    ]
    monkeypatch.setattr(managed_worker, 'sys', SimpleNamespace(
        stdin=io.StringIO(''.join(json.dumps(item) + '\n' for item in requests)),
        stdout=SimpleNamespace(fileno=lambda: 1), stderr=SimpleNamespace(fileno=lambda: 2)))
    monkeypatch.setattr(managed_worker.os, 'dup', lambda _: 123)
    monkeypatch.setattr(managed_worker.os, 'dup2', lambda *_: None)
    monkeypatch.setattr(managed_worker.os, 'fdopen', lambda *_args, **_kw: output)
    monkeypatch.setattr(managed_worker, 'SenseVoiceBackend', Backend)
    monkeypatch.setattr(managed_worker, '_lang_from_str', lambda language: language)
    await managed_worker.run()
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0] == replies[3] == {'model': 'sensevoice', 'languages': ['auto']}
    assert replies[1] == {'text': 'recognized'}
    if dynamic:
        assert replies[2] == {'text': 'recognized'}
        assert calls == [('recognize', 'auto'), ('language', 'zh'), ('recognize', 'zh'), ('shutdown',)]
    else:
        assert replies[2] == {'error': 'sensevoice_dynamic_language_unsupported'}
        assert calls == [('recognize', 'auto'), ('shutdown',)]


@pytest.fixture
async def protocol_service(tmp_path, monkeypatch):
    """真实管道交换；私有文件控制回复时机，不替换 healthy 或 _exchange。"""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    participants = {}
    monkeypatch.setattr(managed_module, '_PARTICIPANTS', participants)
    lock = tmp_path / 'engine.lock'
    lock.touch(mode=0o660)
    gate, seen, reply = (tmp_path / name for name in ('gate', 'seen', 'reply'))
    gate.touch()
    reply.write_text(json.dumps({'model': 'sensevoice', 'languages': ['auto']}))
    script = '''import json,sys,time
from pathlib import Path
gate,seen,reply = map(Path,sys.argv[1:])
for line in sys.stdin:
    request=json.loads(line)
    if request['op']=='health':
        with seen.open('a') as log: log.write('health\\n')
        while not gate.exists(): time.sleep(.005)
        print(reply.read_text(),flush=True)
    elif request['op']=='recognize':
        print(json.dumps({'text':'recognized'}),flush=True)
    else:
        print(json.dumps({'model':'sensevoice','languages':['auto']}),flush=True)
'''
    config = AsrConfig(managed={'enabled': True, 'lock_path': str(lock), 'shutdown_timeout': .2})
    service = AsrService({}, 'sensevoice', SessionStore(300), config=config)
    started, finish, second_health = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finish.set()
    created = []

    class Backend(ManagedBackend):
        async def start(self):
            self.health_calls = 0
            created.append(self)
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, '-c', script, str(gate), str(seen), str(reply),
                pass_fds=(self.lock_fd,), start_new_session=True, cwd=tmp_path,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            await self._wait_ready(tmp_path)
            started.set()
            await finish.wait()

        async def healthy(self):
            self.health_calls += 1
            if self.health_calls >= 2:
                second_health.set()
            return await super().healthy()

    monkeypatch.setattr(managed_module, 'ManagedBackend', Backend)
    monkeypatch.setattr(managed_module, 'check_assets', lambda *_: (True, 'assets_verified'))
    monkeypatch.setattr(managed_module, 'foreign_processes', lambda *_: '')
    try:
        yield SimpleNamespace(service=service, backend_class=Backend, gate=gate, seen=seen,
                              reply=reply, started=started, finish=finish, second_health=second_health)
    finally:
        gate.touch()
        finish.set()
        await service.shutdown()
        for backend in created:
            await backend.shutdown()
        for fd, path in participants.values():
            os.close(fd)
            os.unlink(path)


async def wait_for_health_request(case):
    async def seen():
        while not case.seen.exists():
            await asyncio.sleep(.005)
    await asyncio.wait_for(seen(), 2)


@pytest.mark.parametrize('valid', [True, False])
async def test_concurrent_health_checks_share_actual_probe(protocol_service, valid):
    case = protocol_service
    if not valid:
        case.reply.write_text(json.dumps({'model': 'wrong', 'languages': ['auto']}))
    backend = case.backend_class(case.service._config, 'sensevoice', ['auto'], -1)
    # 此处没有资源归属，使用本测试独立取得的私有 FD。
    backend.lock_fd = managed_module.take_lock(case.service._config.managed.lock_path)
    probes = []
    try:
        await backend.start()
        case.gate.unlink()
        probes.append(asyncio.create_task(backend.healthy()))
        await wait_for_health_request(case)
        task = backend.health_task
        probes.append(asyncio.create_task(backend.healthy()))
        await asyncio.wait_for(case.second_health.wait(), 1)
        await asyncio.sleep(.05)
        waiting = [not probe.done() for probe in probes]
        assert backend.health_task is task and not task.done()
        case.gate.touch()
        results = await asyncio.gather(*probes)
        assert waiting == [True, True]
        assert results == [valid, valid]
        assert case.seen.read_text().splitlines() == ['health']
        assert await backend.recognize(b'\x00\x00', ['auto']) == 'recognized'
    finally:
        case.gate.touch()
        await backend.shutdown()
        await asyncio.gather(*probes, return_exceptions=True)
        os.close(backend.lock_fd)


@pytest.mark.parametrize('valid', [True, False])
async def test_health_during_initial_acquire_uses_same_probe(protocol_service, valid):
    case = protocol_service
    case.finish.clear()
    case.gate.unlink()
    if not valid:
        case.reply.write_text(json.dumps({'model': 'wrong', 'languages': ['auto']}))
    acquire = asyncio.create_task(case.service.managed.acquire(
        str(uuid4()), 'sensevoice', 'auto', 'secret-' + 'x' * 32))
    probe = None
    try:
        await asyncio.wait_for(case.started.wait(), 2)
        backend = case.service.managed.backend
        process = backend.process
        probe = asyncio.create_task(case.service.healthz())
        await wait_for_health_request(case)
        task = backend.health_task
        case.finish.set()
        await asyncio.wait_for(case.second_health.wait(), 1)
        await asyncio.sleep(.05)
        waiting = not acquire.done() and not probe.done()
        same_probe = backend.health_task is task and not task.done()
        case.gate.touch()
        health, acquired = await asyncio.gather(probe, acquire, return_exceptions=True)
        assert waiting and same_probe
        assert health['ready'] is valid
        if valid:
            assert acquired['ready'] and case.service.managed.backend is backend
            assert process.returncode is None
            assert await case.service.managed.recognize(
                case.service.managed.owner, acquired['token'], 'auto', b'\x00\x00') == 'recognized'
        else:
            assert isinstance(acquired, DomainError)
            assert not case.service.managed.active and process.returncode is not None
    finally:
        case.gate.touch()
        case.finish.set()
        await asyncio.gather(*(task for task in (probe, acquire) if task is not None), return_exceptions=True)


@pytest.mark.parametrize('cleanup', ['late_reply', 'shutdown'])
async def test_health_timeout_keeps_probe_until_reply_or_shutdown(protocol_service, cleanup):
    case = protocol_service
    backend = case.backend_class(case.service._config, 'sensevoice', ['auto'], -1)
    backend.lock_fd = managed_module.take_lock(case.service._config.managed.lock_path)
    probes = []
    inference = None
    try:
        await backend.start()
        case.gate.unlink()
        durations = []
        async def timed_health():
            started = time.monotonic()
            result = await backend.healthy()
            durations.append(time.monotonic() - started)
            return result
        probes.append(asyncio.create_task(timed_health()))
        await wait_for_health_request(case)
        task = backend.health_task
        probes.append(asyncio.create_task(timed_health()))
        await asyncio.wait_for(case.second_health.wait(), 1)
        assert await asyncio.gather(*probes) == [False, False]
        assert all(1.9 <= elapsed < 3 for elapsed in durations)
        assert backend.health_task is task and not task.done() and backend.io_lock.locked()
        assert case.seen.read_text().splitlines() == ['health']
        if cleanup == 'late_reply':
            inference = asyncio.create_task(backend.recognize(b'\x00\x00', ['auto']))
            await asyncio.sleep(.05)
            assert not inference.done()
            case.gate.touch()
            assert await asyncio.wait_for(inference, 1) == 'recognized'
            assert task.done() and not task.cancelled()
            assert await backend.healthy()
        else:
            process = backend.process
            await backend.shutdown()
            assert process.returncode is not None and task.done()
            assert backend.health_task is None and not backend.io_lock.locked()
    finally:
        case.gate.touch()
        await backend.shutdown()
        await asyncio.gather(*(task for task in (*probes, inference) if task is not None), return_exceptions=True)
        os.close(backend.lock_fd)
