"""真实本地进程/HTTP/flock 集成，替身仅模拟厂商推理器。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from uuid import uuid4

import pytest

from spacemit_ai_gateway.app.settings import AsrConfig
from spacemit_ai_gateway.common.errors import DomainError
from spacemit_ai_gateway.common.sessions import SessionStore
from spacemit_ai_gateway.domains.asr.managed import take_lock, foreign_processes
from spacemit_ai_gateway.domains.asr.managed_assets import QWEN_TEXT, QWEN_ENCODERS
from spacemit_ai_gateway.domains.asr.service import AsrService


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    # 这里不替换进程管理/资产核验/HTTP/锁；执行本地 Python HTTP 服务。
    binary = tmp_path / 'test-inference-engine'
    source = Path(__file__).parent / 'fixtures/managed_llama_server.py'
    binary.write_text(f'#!{sys.executable}\n' + source.read_text().split('\n', 1)[1])
    binary.chmod(0o755)
    root = tmp_path / 'model'
    root.mkdir()
    for name in (*QWEN_ENCODERS, QWEN_TEXT):
        (root / name).write_bytes(b'test-model')
    (root / 'config.json').write_text('{"audio_model": {"ep_config": {"old": true}}}')
    lib = tmp_path / 'lib-test.so'
    lib.write_bytes(b'test-runtime')
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    (root / 'managed-asr-manifest.json').write_text(json.dumps({
        'model': 'qwen3_asr_0.6b', 'version': 'test-1',
        'files': {name: digest(root / name) for name in (*QWEN_ENCODERS, QWEN_TEXT, 'config.json')},
        'runtime': {'packages': {}, 'libraries': {str(lib): digest(lib)}, 'server_sha256': digest(binary)}}))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    lock = tmp_path / 'engine.lock'
    lock.touch(mode=0o660)
    config = AsrConfig(managed={'enabled': True, 'lock_path': str(lock),
                              'startup_timeout': 3, 'shutdown_timeout': .2,
                              'qwen3': {'server_bin': str(binary), 'model_dir': str(root), 'port': port}})
    service = AsrService({}, 'sensevoice', SessionStore(300), config=config)
    monkeypatch.setenv('TEST_LLAMA_ARGS', str(tmp_path / 'args.json'))
    monkeypatch.setenv('TEST_LLAMA_PAYLOAD', str(tmp_path / 'payload.json'))
    yield service.managed, tmp_path
    await service.shutdown()


async def test_real_process_parameters_inference_and_cleanup(engine):
    managed, tmp_path = engine
    owner = str(uuid4())
    acquired = await managed.acquire(owner, 'qwen3_asr_0.6b', 'zh,en', 'test-recovery-secret-' + 'x' * 32)
    backend = managed.backend
    pid, runtime = backend.pid, Path(backend.runtime_dir.name)
    assert Path(f'/proc/{pid}').exists()
    with pytest.raises(BlockingIOError):
        take_lock(managed.config.managed.lock_path)
    args = json.loads((tmp_path / 'args.json').read_text())
    assert '--slots' in args
    for flag, value in [('--media-backend', 'smt'), ('-t', '2'), ('-tb', '2'), ('-c', '4096'), ('-np', '1')]:
        assert args[args.index(flag) + 1] == value
    cfg = json.loads((runtime / 'config.json').read_text())
    assert cfg['audio_model']['ep_config']['spacemit_ep_intra_thread_affinity'] == '8;9'
    assert await managed.recognize(owner, acquired['token'], 'zh,en', b'\0\0' * 160) == '测试识别'
    payload = json.loads((tmp_path / 'payload.json').read_text())
    assert payload['messages'][0]['content'][1]['text'] == 'language Chinese,English<asr_text>'
    assert payload['max_tokens'] == 100 and payload['id_slot'] == 0
    assert await managed.release(owner, acquired['token']) == {'released': True}
    assert not Path(f'/proc/{pid}').exists() and not runtime.exists()
    fd = take_lock(managed.config.managed.lock_path)
    os.close(fd)


@pytest.mark.parametrize('failure', ['exit', 'wrong_model', 'timeout'])
async def test_real_start_failure_never_leaks_resource(engine, monkeypatch, failure):
    managed, tmp_path = engine
    if failure == 'exit':
        monkeypatch.setenv('TEST_LLAMA_EXIT', '1')
    else:
        monkeypatch.setenv('TEST_LLAMA_MODEL', 'another-model')
        managed.config.managed.startup_timeout = .3
    with pytest.raises(DomainError):
        await managed.acquire(str(uuid4()), 'qwen3_asr_0.6b', 'zh,en', 'test-recovery-secret-' + 'x' * 32)
    assert not managed.active and not list(tmp_path.glob('gateway-asr-*'))
    fd = take_lock(managed.config.managed.lock_path)
    os.close(fd)


async def test_real_slow_inference_disconnect_and_term_escalation(engine, monkeypatch):
    managed, tmp_path = engine
    monkeypatch.setenv('TEST_LLAMA_DELAY', '.3')
    monkeypatch.setenv('TEST_LLAMA_IGNORE_TERM', '1')
    owner = str(uuid4())
    acquired = await managed.acquire(owner, 'qwen3_asr_0.6b', 'zh,en', 'test-recovery-secret-' + 'x' * 32)
    pid = managed.backend.pid
    request = asyncio.create_task(managed.recognize(owner, acquired['token'], 'zh,en', b'\0\0'))
    for _ in range(100):
        if (tmp_path / 'payload.json').exists():
            break
        await asyncio.sleep(.01)
    assert (tmp_path / 'payload.json').exists()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release = asyncio.create_task(managed.release(owner, acquired['token']))
    await asyncio.sleep(.03)
    assert not release.done()
    with pytest.raises(BlockingIOError):
        take_lock(managed.config.managed.lock_path)
    assert await release == {'released': True}
    assert not Path(f'/proc/{pid}').exists()


def test_inherited_flock_survives_parent_death(tmp_path):
    lock, child_file = tmp_path / 'engine.lock', tmp_path / 'child'
    lock.touch()
    # 父进程获取锁并传给真正的孙进程，然后立即退出；不依赖 token 或本进程存活。
    source = '''import os, sys, subprocess
from spacemit_ai_gateway.domains.asr.managed import take_lock
fd = take_lock(sys.argv[1])
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'], pass_fds=(fd,))
open(sys.argv[2], 'w').write(str(child.pid))
os._exit(0)
'''
    parent = subprocess.run([sys.executable, '-c', source, str(lock), str(child_file)], timeout=5)
    assert parent.returncode == 0
    pid = int(child_file.read_text())
    try:
        with pytest.raises(BlockingIOError):
            take_lock(str(lock))
    finally:
        os.kill(pid, signal.SIGKILL)
    import time
    for _ in range(100):
        try:
            fd = take_lock(str(lock))
            os.close(fd)
            break
        except BlockingIOError:
            time.sleep(.01)
    else:
        pytest.fail('lock remained after last holder exited')


def test_participant_marker_is_removed_and_stale_files_are_reaped(tmp_path):
    lock = tmp_path / 'engine.lock'
    lock.touch()
    stale = tmp_path / 'participant-99999999.lock'
    stale.touch()
    source = '''import os, sys
from spacemit_ai_gateway.domains.asr.managed import register_participant
register_participant(sys.argv[1])
print(os.path.join(os.path.dirname(sys.argv[1]), f"participant-{os.getpid()}.lock"))
'''
    result = subprocess.run([sys.executable, '-c', source, str(lock)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert not stale.exists()
    assert not Path(result.stdout.strip()).exists()


def test_foreign_engine_port_is_detected_never_killed():
    # 旧调用者占着引擎端口：检测按实际占用，不要求它配合 flock，也不能杀它。
    import socket
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    listener.bind(('127.0.0.1', port))
    listener.listen(1)
    try:
        assert foreign_processes(None, '/run/lock/bibit-asr/engine.lock', port) == f'unmanaged_asr_engine_port:{port}'
    finally:
        listener.close()


def test_llama_server_name_without_engine_port_is_not_managed_asr():
    # LLM/Embedding 同样以 llama-server 运行；按进程名拦会挡住既有服务（R1）。
    import socket
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)', 'llama-server'])
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    try:
        assert child.poll() is None
        assert foreign_processes(None, '/run/lock/bibit-asr/engine.lock', port) != f'unmanaged_asr_process:{child.pid}'
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_participant_marker_requires_live_exclusive_flock(tmp_path):
    from spacemit_ai_gateway.domains.asr.managed import cooperative_participant
    lock = tmp_path / 'engine.lock'
    marker = tmp_path / 'participant-123.lock'
    assert not cooperative_participant(123, str(lock))
    marker.touch(mode=0o644)
    assert not cooperative_participant(123, str(lock))
    # 子进程真实持锁；留存文件/释放锁均不能当协作证据。
    code = "import fcntl,sys; f=open(sys.argv[1], 'r+b'); fcntl.flock(f, fcntl.LOCK_EX); print('ready',flush=True); input()"
    child = subprocess.Popen([sys.executable, '-c', code, str(marker)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline() == b'ready\n'
        assert cooperative_participant(123, str(lock))
    finally:
        child.communicate(b'\n', timeout=3)
    assert not cooperative_participant(123, str(lock))
    marker.unlink()
    marker.symlink_to(lock)
    assert not cooperative_participant(123, str(lock))


async def test_language_switch_preserves_child_and_updates_prompt(engine):
    managed, tmp_path = engine
    owner = str(uuid4())
    acquired = await managed.acquire(owner, 'qwen3_asr_0.6b', 'zh,en', 'test-recovery-secret-' + 'x' * 32)
    pid = managed.backend.pid
    for language, prompt in [('en', 'language English<asr_text>'), ('zh', 'language Chinese<asr_text>')]:
        assert (await managed.status('qwen3_asr_0.6b', language))['ready']
        await managed.recognize(owner, acquired['token'], language, b'\0\0')
        payload = json.loads((tmp_path / 'payload.json').read_text())
        assert payload['messages'][0]['content'][1]['text'] == prompt
        assert managed.backend.pid == pid


def test_participant_is_shared_and_never_inherited_by_worker(tmp_path):
    import concurrent.futures
    import fcntl
    from spacemit_ai_gateway.domains.asr.managed import register_participant
    lock = tmp_path / 'engine.lock'
    lock.touch(mode=0o660)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        descriptors = list(pool.map(register_participant, [str(lock)] * 8))
    marker_fd = descriptors[0]
    assert len(set(descriptors)) == 1
    assert fcntl.fcntl(marker_fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    marker = tmp_path / f'participant-{os.getpid()}.lock'
    assert marker.stat().st_mode & 0o777 == 0o644
    service = AsrService({}, 'sensevoice', SessionStore(300), config=AsrConfig(
        managed={'lock_path': str(lock)}))
    assert service._participant_error is None
    engine_fd = take_lock(str(lock))
    code = '''import json,os,sys
present = []
for name in os.listdir('/proc/self/fd'):
    try:
        present.append(os.fstat(int(name)).st_ino)
    except OSError:
        pass
print(json.dumps([int(arg) in present for arg in sys.argv[1:]]))
'''
    try:
        child = subprocess.run([sys.executable, '-c', code, str(marker.stat().st_ino), str(lock.stat().st_ino)],
                               pass_fds=(engine_fd,), capture_output=True, check=True, timeout=5)
        assert json.loads(child.stdout) == [False, True]
    finally:
        os.close(engine_fd)


def test_affinity_guard_only_pins_a100_threads(monkeypatch):
    from types import SimpleNamespace
    from spacemit_ai_gateway.domains.asr.managed_backend import ManagedBackend
    backend = ManagedBackend(AsrConfig(), 'qwen3_asr_0.6b', ['zh'], -1)
    backend.process = SimpleNamespace(pid=123)
    masks = {1: {8, 9, 10}, 2: {0, 8, 9}, 3: {0, 1}, 4: {8, 9}, 5: {15}}
    monkeypatch.setattr(Path, 'iterdir', lambda path: [Path(str(tid)) for tid in masks])
    monkeypatch.setattr(os, 'sched_getaffinity', lambda tid: masks[tid])
    calls = []
    monkeypatch.setattr(os, 'sched_setaffinity', lambda tid, target: calls.append((tid, target)))
    backend._pin_a100_threads({8, 9})
    assert calls == [(1, {8, 9}), (5, {8, 9})]


async def test_release_terminates_stalled_inference_before_unlock(engine, monkeypatch):
    import contextlib
    managed, tmp_path = engine
    monkeypatch.setenv('TEST_LLAMA_DELAY', '60')
    owner = str(uuid4())
    acquired = await managed.acquire(owner, 'qwen3_asr_0.6b', 'zh', 'test-recovery-secret-' + 'x' * 32)
    backend = managed.backend
    pid = backend.pid
    request = asyncio.create_task(managed.recognize(owner, acquired['token'], 'zh', b'\0\0'))
    release = None
    shutdown_entered = asyncio.Event()
    allow_shutdown = asyncio.Event()
    shutdown = backend.shutdown

    async def shutdown_with_barrier():
        shutdown_entered.set()
        await allow_shutdown.wait()
        await shutdown()

    monkeypatch.setattr(backend, 'shutdown', shutdown_with_barrier)

    async def wait_for_payload():
        while not (tmp_path / 'payload.json').exists():
            await asyncio.sleep(.01)

    try:
        await asyncio.wait_for(wait_for_payload(), 5)
        os.kill(pid, signal.SIGSTOP)
        release = asyncio.create_task(managed.release(owner, acquired['token']))
        await asyncio.wait_for(shutdown_entered.wait(), 2)
        assert backend.process.returncode is None and Path(f'/proc/{pid}').exists()
        with pytest.raises(BlockingIOError):
            take_lock(managed.config.managed.lock_path)
        allow_shutdown.set()
        assert await asyncio.wait_for(asyncio.shield(release), 2) == {'released': True}
        assert not Path(f'/proc/{pid}').exists() and not managed.active
        fd = take_lock(managed.config.managed.lock_path)
        os.close(fd)
    finally:
        allow_shutdown.set()
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        await asyncio.gather(request, return_exceptions=True)
        if release:
            await asyncio.wait_for(release, 5)


@pytest.mark.parametrize('outcome', ['reply', 'exit', 'hang'])
@pytest.mark.parametrize('interruption', ['timeout', 'cancel'])
async def test_sensevoice_health_timeout_preserves_pipe_and_shutdown_collects_task(outcome, interruption):
    from spacemit_ai_gateway.domains.asr.managed_backend import ManagedBackend
    config = AsrConfig(managed={'shutdown_timeout': .1})
    backend = ManagedBackend(config, 'sensevoice', ['auto'], -1)
    source = ('import sys,json,time\n'
              'for line in sys.stdin:\n'
              ' request=json.loads(line)\n'
              ' if request["op"]=="health":\n'
              f'  time.sleep({60 if outcome == "hang" else 2.3})\n'
              + ('  sys.exit(0)\n' if outcome == 'exit' else '')
              + ' print(json.dumps({"model":"sensevoice","languages":["auto"]} '
              'if request["op"]=="health" else {"text":"ok"}),flush=True)\n')
    backend.process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', source, start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    process = backend.process
    backend.identity = ('sensevoice', ['auto'])
    errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        if interruption == 'timeout':
            assert not await backend.healthy()
        else:
            probe = asyncio.create_task(backend.healthy())
            async def sent():
                while backend.exchange_started is None:
                    await asyncio.sleep(.001)
            await asyncio.wait_for(sent(), 1)
            probe.cancel()
            with pytest.raises(asyncio.CancelledError):
                await probe
        assert backend.io_lock.locked()
        exchange = backend.health_task
        assert await backend.healthy() is (outcome == 'reply' and interruption == 'timeout')
        assert backend.health_task is exchange
        if outcome == 'reply':
            assert await asyncio.wait_for(backend.recognize(b'\0\0', ['auto']), 4) == 'ok'
        elif outcome == 'exit':
            await asyncio.wait_for(process.wait(), 4)
            await asyncio.sleep(0)
        await asyncio.wait_for(backend.shutdown(), 1)
        assert exchange.done()
        assert process.returncode is not None
        assert not backend.io_lock.locked()
        assert backend.health_task is None
        import gc
        del exchange
        gc.collect()
        await asyncio.sleep(0)
        assert not errors
    finally:
        try:
            await asyncio.wait_for(backend.shutdown(), 1)
        finally:
            loop.set_exception_handler(previous_handler)
