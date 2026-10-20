"""联合故障验收只借用现有快照/恢复，不替代 Voice 识别验收。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('failed', [False, True])
def test_gateway_failure_propagates_to_outer_voice_restoration(failed, monkeypatch):
    path = Path(__file__).parents[2] / 'docs/verification/evidence/task-0a454fe6a6aa4051/managed-device-check.py'
    spec = importlib.util.spec_from_file_location('managed_coordinated_check', path)
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    class FixturePath:
        def __init__(self, path):
            self.name = Path(path).name

        def read_text(self):
            return 'fixture-key\n'
    monkeypatch.setattr(check, 'Path', FixturePath)
    args = SimpleNamespace(gateway_deb='gateway.deb', gateway_sha256='hash',
                           managed_cases='cases.json', managed_cases_sha256='cases-hash',
                           evidence_dir='evidence', request_id='request', attempt_id='attempt', artifact_id='artifact')
    voice = SimpleNamespace(args=args, stop_services=Mock(), configure_gateway=Mock(), checked=Mock(),
                            managed={'result': 'joint-result'}, checks=[],
                            KEY=SimpleNamespace(read_text=lambda: 'fixture-key\n'))
    original = Mock()
    job = SimpleNamespace(managed_contract=Mock(side_effect=RuntimeError('fault check failed') if failed else None),
                          managed_evidence={'result': 'failed' if failed else 'ok'}, checks=['fault-pass'])
    gateway = SimpleNamespace(Lifecycle=Mock(return_value=job),
                              api=Mock(return_value={'status': 'ok'}),
                              wait_for=Mock(side_effect=lambda callback: callback()))
    if failed:
        with pytest.raises(RuntimeError, match='fault check failed'):
            check.exercise(voice, gateway, original)
    else:
        check.exercise(voice, gateway, original)
    voice.stop_services.assert_called_once()
    voice.configure_gateway.assert_called_once()
    gateway.wait_for.assert_called_once()
    gateway.api.assert_called_once_with('/healthz')
    original.assert_called_once_with(voice)
    assert voice.managed['result'] == 'joint-result'
    assert job.key == 'fixture-key'
    if failed:
        assert voice.managed['audit_fault']['error_type'] == 'RuntimeError'
        assert voice.managed['audit_fault']['failure_frames']
        voice.checked.assert_not_called()
    else:
        assert voice.managed['audit_fault'] == job.managed_evidence
        assert voice.checks == ['fault-pass']
    assert gateway.Lifecycle.call_args.args[0].managed_cases_sha256 == 'cases-hash'


@pytest.mark.parametrize('outcome', ['stopped', 'missing_symbol', 'probe_exit'])
def test_compute_probe_is_owned_and_collected(tmp_path, monkeypatch, outcome):
    path = Path(__file__).parents[2] / 'docs/verification/evidence/task-0a454fe6a6aa4051/managed-device-check.py'
    spec = importlib.util.spec_from_file_location('managed_compute_check', path)
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    library = tmp_path / 'libspacemit_ep.so.2'
    library.write_bytes(b'fixture-library')
    maps = tmp_path / 'maps'
    maps.write_text(str(library))
    status = tmp_path / 'status'
    status.write_text('Name:\tengine\nState:\tT (stopped)\n')
    monkeypatch.setattr(check, 'Path', lambda value: library if value == '/usr/lib/libspacemit_ep.so.2'
                        else maps if str(value).endswith('/maps') else status)
    symbol = '_ZN11onnxruntime8spacemit22SpineGemmDispatchMMT4D16PackOnlineWeightEfixture'
    monkeypatch.setattr(check.subprocess, 'check_output', lambda *a, **kw:
                        '' if outcome == 'missing_symbol' else f'1 0010 20 FUNC GLOBAL DEFAULT 10 {symbol}')
    monkeypatch.setenv('GATEWAY_AUDIT_GDB', str(tmp_path / 'root/usr/bin/gdb'))
    # debugger Path 的根目录不参与 /proc 夹具映射。
    real_path = check.Path
    monkeypatch.setattr(check, 'Path', lambda value: Path(value) if str(value).endswith('/gdb') else real_path(value))
    output = ('Temporary breakpoint 1, SpineGemmDispatchMMT4D::PackOnlineWeight from ' + str(library)
              + '\nOWNED_COMPUTE_PC=0x1234\n')
    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock(return_value=False)
    probe.poll.return_value = 1 if outcome == 'probe_exit' else None
    probe.returncode = 0
    probe.communicate.return_value = (output, 'fixture-diagnostic')
    launch = Mock(return_value=probe)
    monkeypatch.setattr(check.subprocess, 'Popen', launch)
    selector = Mock()
    selector.__enter__ = Mock(return_value=selector)
    selector.__exit__ = Mock(return_value=False)
    selector.select.return_value = [('ready', 1)]
    monkeypatch.setattr(check.selectors, 'DefaultSelector', lambda: selector)
    monkeypatch.setattr(check.os, 'read', lambda *args: b'Temporary breakpoint 1 at 0x1234\nOWNED_COMPUTE_ARMED\n')
    status.write_text('Name:\tengine\nState:\tT (stopped)\nTracerPid:\t0\n')
    job = SimpleNamespace(managed_evidence={}, processing_ready=Mock())
    future = Mock()
    future.done.return_value = False
    if outcome == 'stopped':
        check.stop_in_compute(job, 123, 8063, future)
        assert job.managed_evidence['inference_phase'] == 'verified_inference_weight_pack_breakpoint'
        assert job.managed_evidence['compute_probe']['pc'] == '0x1234'
        command = launch.call_args.args[0]
        assert command[command.index('-p') + 1] == '123'
        assert 'tbreak ' + symbol in command
        assert 'set print frame-arguments none' in command
        assert 'printf "OWNED_COMPUTE_ARMED\\n"' in command
        assert command[-3:] == ['shell kill -STOP 123', '-ex', 'detach']
    else:
        with pytest.raises(RuntimeError, match='owned_compute_'):
            check.stop_in_compute(job, 123, 8063, future)
        if outcome == 'missing_symbol':
            assert not job.managed_evidence
            launch.assert_not_called()
        else:
            assert 'inference_phase' not in job.managed_evidence
            job.processing_ready.set.assert_called_once()
