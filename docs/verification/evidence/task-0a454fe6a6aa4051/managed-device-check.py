"""在已持有的板端验收租约内，复用 Voice 快照/恢复保护 Gateway 故障验收。"""

import importlib.util
import hashlib
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stop_in_compute(job, pid, port, future):
    library = Path('/usr/lib/libspacemit_ep.so.2').resolve()
    symbols = subprocess.check_output(['readelf', '--dyn-syms', '--wide', str(library)], text=True)
    candidates = [line.split()[-1] for line in symbols.splitlines()
                  if ' FUNC ' in line and ' UND ' not in line
                  and '_ZN11onnxruntime8spacemit22SpineGemmDispatchMMT4D16PackOnlineWeightE' in line]
    if len(candidates) != 1 or str(library) not in Path(f'/proc/{pid}/maps').read_text():
        raise RuntimeError('owned_compute_symbol_unavailable')
    debugger = Path(os.environ['GATEWAY_AUDIT_GDB'])
    root = debugger.parents[2]
    symbol = candidates[0]
    # 真人长音频线程栈已确认在线权重打包；基类派发入口只是 ret，不能作为推理证据。
    command = [str(debugger), '-nx', '-nh', '-q', '--batch',
               '--data-directory=' + str(root / 'usr/share/gdb'),
               '-ex', 'set auto-load off', '-ex', 'set debuginfod enabled off',
               '-ex', 'set pagination off', '-ex', 'set confirm off',
               '-ex', 'set print frame-arguments none', '-ex', 'set print thread-events off',
               '-ex', 'set breakpoint pending off', '-p', str(pid),
               '-ex', 'tbreak ' + symbol, '-ex', 'printf "OWNED_COMPUTE_ARMED\\n"', '-ex', 'continue',
               '-ex', 'printf "OWNED_COMPUTE_PC=%p\\n", $pc',
               '-ex', 'info symbol $pc', '-ex', 'thread', '-ex', 'bt 4',
               '-ex', f'shell kill -STOP {pid}', '-ex', 'detach']
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          env=dict(os.environ, LD_LIBRARY_PATH=str(root / 'usr/lib/riscv64-linux-gnu'))) as probe:
        try:
            prefix = []
            with selectors.DefaultSelector() as selector:
                selector.register(probe.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and probe.poll() is None:
                    if selector.select(.1):
                        chunk = os.read(probe.stdout.fileno(), 65536).decode()
                        prefix.append(chunk)
                        if 'OWNED_COMPUTE_ARMED\n' in ''.join(prefix):
                            if 'Temporary breakpoint 1 at ' not in ''.join(prefix):
                                raise RuntimeError('owned_compute_breakpoint_not_armed')
                            job.processing_ready.set()
                            break
                else:
                    raise RuntimeError('owned_compute_breakpoint_not_armed')
            output, error = probe.communicate(timeout=30)
            result = subprocess.CompletedProcess(command, probe.returncode, ''.join(prefix) + output, error)
        finally:
            if probe.poll() is None:
                probe.send_signal(signal.SIGINT)
                try:
                    probe.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    probe.kill()
                    probe.communicate()
            job.processing_ready.set()
    job.managed_evidence['compute_probe_exit'] = result.returncode
    job.managed_evidence['compute_probe_stderr_sha256'] = hashlib.sha256(result.stderr.encode()).hexdigest()
    import re
    pc = re.search(r'OWNED_COMPUTE_PC=(0x[0-9a-f]+)', result.stdout)
    state = Path(f'/proc/{pid}/status').read_text()
    if (result.returncode or pc is None or 'Temporary breakpoint 1,' not in result.stdout
            or 'SpineGemmDispatchMMT4D::PackOnlineWeight' not in result.stdout
            or 'libspacemit_ep.so' not in result.stdout or future.done()
            or '\nState:\tT' not in state or '\nTracerPid:\t0' not in state):
        raise RuntimeError('owned_compute_stop_not_verified')
    job.managed_evidence['inference_phase'] = 'verified_inference_weight_pack_breakpoint'
    job.managed_evidence['compute_probe'] = dict(
        pid=pid, pc=pc[1], library=str(library), symbol=symbol,
        sha256=hashlib.sha256(library.read_bytes()).hexdigest())


def exercise(voice, gateway, original_exercise):
    original_exercise(voice)
    voice.stop_services()
    args = voice.args
    job = gateway.Lifecycle(SimpleNamespace(
        binary=args.gateway_deb, expect_sha256=args.gateway_sha256,
        managed_cases=args.managed_cases, managed_cases_sha256=args.managed_cases_sha256,
        evidence_dir=args.evidence_dir, request_id=args.request_id,
        attempt_id=args.attempt_id, artifact_id=args.artifact_id))
    job.key = Path('/etc/spacemit-ai-gateway/voice-api-key').read_text().strip()
    voice.configure_gateway()
    gateway.wait_for(lambda: gateway.api('/healthz'))
    # 外层 Voice execute 的 finally 负责恢复模型 manifest、Gateway 和用户服务。
    try:
        job.managed_contract()
    except Exception as exc:
        job.managed_evidence['error_type'] = type(exc).__name__
        job.managed_evidence['failure_frames'] = [
            {'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)]
        raise
    finally:
        voice.managed['audit_fault'] = job.managed_evidence
    voice.checks.extend(job.checks)
    voice.checked('mr7-coordinated-gateway-stalled-release')


def main():
    voice = load('voice_device_acceptance', sys.argv.pop(1))
    gateway = load('gateway_device_acceptance', sys.argv.pop(1))
    # 使用已安装 ABI 入口，manifest 仍记录解析后的实际文件与摘要，不伪造旧版本软链。
    voice.RUNTIME_LIBS = tuple(Path('/usr/lib') / name for name in
                               ('libonnxruntime.so.1', 'libspacemit_ep.so.2', 'libspert.so.1'))
    original_exercise = voice.Lifecycle.exercise
    if os.environ.get('GATEWAY_AUDIT_FAULT_ONLY') == '1':
        def original_exercise(job):
            job.stop_services()
            job.install_debs(job.args.gateway_deb)
            job.checked('gateway-fault-only-no-joint-recognition-claim')
    gateway.Lifecycle.processing_ready = threading.Event()
    original_request = gateway.request
    def request(*args, **kwargs):
        if (args and args[0] == '/v1/asr/managed/recognize'
                and threading.current_thread() is not threading.main_thread()):
            if not gateway.Lifecycle.processing_ready.wait(60):
                raise RuntimeError('owned_compute_breakpoint_not_armed')
        return original_request(*args, **kwargs)
    gateway.request = request
    gateway.Lifecycle.stop_in_processing = stop_in_compute
    voice.Lifecycle.exercise = lambda self: exercise(self, gateway, original_exercise)
    return voice.main()


if __name__ == '__main__':
    raise SystemExit(main())
