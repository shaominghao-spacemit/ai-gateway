"""生命周期验收脚本的宿主边界测试；不执行真实包或服务操作。"""

import argparse
from contextlib import closing, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[2] / 'debian/tests/test-device-lifecycle.py'
SPEC = importlib.util.spec_from_file_location('device_lifecycle', SCRIPT)
lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lifecycle)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'gateway.deb'
        self.binary.write_bytes(b'test artifact')
        self.args = argparse.Namespace(
            binary=self.binary, expect_sha256=lifecycle.sha256(self.binary),
            evidence_dir=str(self.root / 'evidence'), request_id='request',
            attempt_id='attempt', artifact_id='artifact')
        self.job = lifecycle.Lifecycle(self.args)

    def execute(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = self.job.execute()
        return code, json.loads(output.getvalue().splitlines()[-1])

    def backup(self):
        evidence = Path(self.args.evidence_dir)
        evidence.mkdir(mode=0o755)
        self.job.backup = evidence / 'gateway-backup-test'
        self.job.backup.mkdir(mode=0o700)

    def test_hash_mismatch_changes_nothing(self):
        self.args.expect_sha256 = '0' * 64
        with patch.object(lifecycle, 'run') as run, \
                patch.object(self.job, 'exercise') as exercise, \
                patch.object(self.job, 'restore') as restore:
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertTrue(result['restored'])
        self.assertEqual(result['error'], 'SHA256 mismatch')
        self.assertFalse(Path(self.args.evidence_dir).exists())
        run.assert_not_called()
        exercise.assert_not_called()
        restore.assert_not_called()

    def test_preflight_failure_does_not_restore_or_install(self):
        with patch.object(self.job, 'prepare', side_effect=lifecycle.CheckFailed('no rollback deb')), \
                patch.object(self.job, 'exercise') as exercise, \
                patch.object(self.job, 'restore') as restore:
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertTrue(result['restored'])
        exercise.assert_not_called()
        restore.assert_not_called()

    def test_failure_after_mutation_always_restores(self):
        def fail():
            self.job.mutated = True
            raise lifecycle.CheckFailed('inference failed')

        def restored():
            self.job.restored = True

        with patch.object(self.job, 'prepare'), \
                patch.object(self.job, 'exercise', side_effect=fail), \
                patch.object(self.job, 'restore', side_effect=restored) as restore:
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertTrue(result['restored'])
        restore.assert_called_once()

    def test_restore_failure_is_explicit_and_preserves_backup(self):
        self.backup()
        self.job.mutated = True
        with patch.object(self.job, 'prepare'), patch.object(self.job, 'exercise'), \
                patch.object(self.job, 'restore', side_effect=OSError('private log')):
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertFalse(result['restored'])
        self.assertIn('RESTORE FAILED', result['error'])
        self.assertNotIn('private log', json.dumps(result))
        self.assertTrue(self.job.backup.is_dir())

    def test_public_summary_is_readable_and_contains_no_key_or_backup(self):
        self.backup()
        with patch.object(self.job, 'prepare'), patch.object(self.job, 'exercise'):
            code, result = self.execute()
        self.assertEqual(code, 0)
        log = Path(result['log'])
        text = log.read_text()
        self.assertEqual(log.stat().st_mode & 0o777, 0o644)
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.job.backup.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(self.job.key, text)
        self.assertNotIn('backup', text)
        self.assertEqual(json.loads(text)['result'], 'ok')

    def test_summary_failure_still_prints_failure_json(self):
        self.backup()
        (Path(self.args.evidence_dir) / 'gateway-backup-test-summary.json').touch()
        with patch.object(self.job, 'prepare'), patch.object(self.job, 'exercise'):
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(result['result'], 'failed')

    def test_simulated_changes_to_other_packages_are_rejected(self):
        for package in ('bibit-voice', 'spacemit-ort', 'libllama', 'python3'):
            for operation in ('Inst', 'Remv', 'Purg'):
                with self.subTest(package=package, operation=operation):
                    with self.assertRaises(lifecycle.CheckFailed):
                        lifecycle.safe_plan(f'{operation} {package} [1] (2 repo)')
        lifecycle.safe_plan('Inst spacemit-ai-gateway:riscv64 [1] (2 local)')

    def test_subprocess_forces_offline_and_redacts_errors(self):
        result = subprocess.CompletedProcess(['dpkg'], 1, 'secret key',
                                              'dpkg: error: API_KEY=super-secret\n'
                                              'maintainer: package configuration failed')
        with patch.object(lifecycle.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(lifecycle.CheckFailed,
                                        r'^dpkg failed \(exit 1\): .*API_KEY=\[redacted\]') as raised:
                lifecycle.run('dpkg', '-i', 'new.deb')
        self.assertEqual(run.call_args.kwargs['env']['PIP_NO_INDEX'], '1')
        self.assertIn('super-secret', raised.exception.detail)
        self.assertNotIn('super-secret', str(raised.exception))

    def test_failed_command_detail_is_private_backup_log(self):
        self.backup()
        failure = lifecycle.CheckFailed('dpkg failed (exit 1): package configuration failed',
                                         'stderr: API_KEY=super-secret')
        with patch.object(self.job, 'prepare', side_effect=failure):
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertNotIn('super-secret', json.dumps(result))
        detail = self.job.backup / 'command-errors.log'
        self.assertEqual(detail.read_text(), 'stderr: API_KEY=super-secret')
        self.assertEqual(detail.stat().st_mode & 0o777, 0o600)

    def test_credential_parser_preserves_json_quotes(self):
        path = self.root / 'credentials.env'
        path.write_text("SPACEMIT_AI_GATEWAY_AUTH__API_KEYS='[\"secret\"]'\n")
        self.assertEqual(lifecycle.credential_key(path), 'secret')
        for value in ('["secret"]', "'not json'", "'[]'", "'{}'"):
            with self.subTest(value=value):
                path.write_text('SPACEMIT_AI_GATEWAY_AUTH__API_KEYS=' + value + '\n')
                with self.assertRaises(lifecycle.CheckFailed):
                    lifecycle.credential_key(path)

    def test_packaged_auth_checked_before_admin_override(self):
        etc = self.root / 'etc'
        etc.mkdir()
        (etc / 'credentials.env').write_text(
            "SPACEMIT_AI_GATEWAY_AUTH__API_KEYS='[\"packaged-secret\"]'\n")
        pid_text = self.root / 'proc-status'
        pid_text.write_text('Uid:\t1000\t1000\t1000\t1000\n')
        responses = [subprocess.CompletedProcess([], 0, '1', ''),
                     subprocess.CompletedProcess([], 0, '2', ''),
                     subprocess.CompletedProcess([], 0,
                         'LISTEN 0 100 127.0.0.1:18790 0.0.0.0:*\n'
                         'LISTEN 0 100 127.0.0.1:8326 0.0.0.0:*', '')]
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if str(path).startswith('/proc/'):
                return original_read(pid_text)
            return original_read(path, *args, **kwargs)

        with patch.object(lifecycle, 'ETC', etc), patch.object(Path, 'read_text', read), \
                patch.object(lifecycle, 'run', side_effect=responses), \
                patch.object(self.job, 'health'), \
                patch.object(lifecycle, 'request', side_effect=[(401, b''), (404, b'')]) as request:
            self.job.check_services()
        self.assertEqual(request.call_args_list[0].args, ('/v1/vad/models/load',))
        self.assertEqual(request.call_args_list[1].args[1], 'packaged-secret')
        self.assertIn('packaged-credentials-authentication', self.job.checks)

    def test_mock_vad_is_rejected_before_inference(self):
        with patch.object(lifecycle, 'api', return_value={'loaded': True}) as api, \
                patch.object(self.job, 'health', return_value={'domains': {'vad': {
                    'ready': True, 'state': 'ready', 'backend': 'silero (mock)'}}}):
            with self.assertRaisesRegex(lifecycle.CheckFailed, 'real ready Silero'):
                self.job.vad()
        api.assert_called_once()

    def test_protected_inventory_is_filtered_and_compared(self):
        lines = ('bibit-voice\t1\tinstall ok installed\nspacemit-ort\t2\tinstall ok installed\n'
                 'libllama\t3\tinstall ok installed\npython3\t4\tinstall ok installed\n')
        with patch.object(lifecycle, 'run', return_value=subprocess.CompletedProcess([], 0, lines)):
            inventory = lifecycle.protected_packages()
        self.assertEqual(len(inventory), 3)
        self.job.protected_before = inventory
        with patch.object(self.job, 'prepare'), patch.object(self.job, 'exercise'), \
                patch.object(lifecycle, 'protected_packages', return_value=inventory[:-1]):
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertIn('inventory changed', result['error'])

    def test_admin_environment_quotes_entire_json_value(self):
        etc, data, model = (self.root / name for name in ('etc', 'data', 'silero_vad.onnx'))
        etc.mkdir()
        data.mkdir()
        model.write_bytes(b'model')
        (etc / 'base.yaml').write_text('vad: {}\n')
        with patch.object(lifecycle, 'ETC', etc), patch.object(lifecycle, 'DATA', data), \
                patch.object(lifecycle, 'MODEL', model), patch.object(lifecycle, 'UNITS', ()), \
                patch.object(lifecycle.pwd, 'getpwnam', return_value=Mock(pw_uid=1000, pw_gid=1000)), \
                patch.object(lifecycle.os, 'chown'), patch.object(self.job, 'restart'), \
                patch.object(self.job, 'vad'), \
                patch.object(lifecycle, 'request', return_value=(401, b'')):
            self.job.configure_admin()
        self.assertEqual(lifecycle.credential_key(etc / 'environment'), self.job.key)
        self.assertEqual((etc / 'environment').stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(lifecycle.os.geteuid() == 0, '宿主权限探针需要普通用户')
    def test_r2_probe_writes_sqlite_and_rejects_writable_program_directory(self):
        app, data = self.root / 'app', self.root / 'data'
        app.mkdir(mode=0o555)
        data.mkdir()

        def run(*command, **kwargs):
            self.assertEqual(command[:4], ('runuser', '-u', lifecycle.PACKAGE, '--'))
            result = subprocess.run(command[4:], capture_output=True, text=True)
            lifecycle.require(result.returncode == 0, 'permission probe failed')
            return result

        with patch.object(lifecycle, 'APP', app), patch.object(lifecycle, 'DATA', data), \
                patch.object(lifecycle, 'run', side_effect=run):
            try:
                self.job.check_storage_permissions()
                self.assertEqual(list(data.iterdir()), [])
                self.assertIn('r2-program-readonly-data-sqlite-writable', self.job.checks)
                app.chmod(0o755)
                with self.assertRaisesRegex(lifecycle.CheckFailed, 'permission probe failed'):
                    self.job.check_storage_permissions()
            finally:
                app.chmod(0o755)

    def test_r6_model_failure_must_degrade_health_and_always_restore_model(self):
        for status in ('degraded', 'ok'):
            with self.subTest(status=status):
                data = self.root / status
                directory = data / 'lifecycle-model'
                directory.mkdir(parents=True)
                model = directory / 'silero_vad.onnx'
                model.write_bytes(b'original real model')
                states = [{'status': status, 'domains': {'vad': {'ready': False}}},
                          {'status': 'ok'}]

                def request(*args, **kwargs):
                    self.assertIn(b'invalid ONNX', model.read_bytes())
                    return 503, b'{"error":"load_failed"}'

                with patch.object(lifecycle, 'DATA', data), patch.object(lifecycle, 'api'), \
                        patch.object(lifecycle, 'request', side_effect=request), \
                        patch.object(self.job, 'health', side_effect=states), \
                        patch.object(self.job, 'restart') as restart, \
                        patch.object(self.job, 'vad') as vad:
                    if status == 'ok':
                        with self.assertRaisesRegex(lifecycle.CheckFailed, 'R6 failed VAD'):
                            self.job.check_failed_model_health()
                    else:
                        self.job.check_failed_model_health()
                self.assertEqual(model.read_bytes(), b'original real model')
                self.assertFalse(model.with_name('silero_vad.onnx.lifecycle-good').exists())
                restart.assert_called_once()
                vad.assert_called_once()

    def test_r6_request_failure_still_restores_model_and_reloads(self):
        directory = self.root / 'lifecycle-model'
        directory.mkdir()
        model = directory / 'silero_vad.onnx'
        model.write_bytes(b'original')
        with patch.object(lifecycle, 'DATA', self.root), patch.object(lifecycle, 'api'), \
                patch.object(lifecycle, 'request', side_effect=TimeoutError), \
                patch.object(self.job, 'restart') as restart, patch.object(self.job, 'vad') as vad:
            with self.assertRaises(TimeoutError):
                self.job.check_failed_model_health()
        self.assertEqual(model.read_bytes(), b'original')
        restart.assert_called_once()
        vad.assert_called_once()

    def test_unavailable_exact_old_deb_stops_before_mutation(self):
        model = self.root / 'silero_vad.onnx'
        model.write_bytes(b'model')
        commands = []

        def run(*command, **kwargs):
            commands.append(command)
            if command[:2] == ('dpkg-deb', '-f'):
                return subprocess.CompletedProcess(command, 0, lifecycle.PACKAGE)
            if command[:2] == ('dpkg-query', '-W'):
                return subprocess.CompletedProcess(command, 0, 'install ok installed\n0.0.17')
            if command[:2] == ('apt-get', 'download'):
                self.assertEqual(command[2], lifecycle.PACKAGE + '=0.0.17')
                raise lifecycle.CheckFailed('exact old deb unavailable')
            self.fail('unexpected command')

        with patch.object(lifecycle.os, 'geteuid', return_value=0), \
                patch.object(lifecycle, 'MODEL', model), patch.object(lifecycle, 'run', side_effect=run), \
                patch.object(lifecycle, 'protected_packages', return_value=[]), \
                patch.object(self.job, 'exercise') as exercise, \
                patch.object(self.job, 'restore') as restore:
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertFalse(self.job.mutated)
        self.assertTrue(result['restored'])
        self.assertTrue(Path(result['log']).is_file())
        self.assertEqual(Path(self.args.evidence_dir).stat().st_mode & 0o777, 0o755)
        self.assertTrue(self.job.backup.is_dir())
        exercise.assert_not_called()
        restore.assert_not_called()
        self.assertFalse(any(cmd[0] in ('dpkg', 'systemctl') for cmd in commands))

    def test_snapshot_waits_for_sqlite_writer_and_backup_failure_only_restarts_services(self):
        for failed_archive in (None, 'files.tar', 'dpkg-info.tar', 'stop'):
            with self.subTest(failed_archive=failed_archive):
                self.job = lifecycle.Lifecycle(self.args)
                data = self.root / str(failed_archive)
                data.mkdir()
                database = data / 'live.sqlite'
                model = data / 'model.onnx'
                model.write_bytes(b'model')
                ready, stopped = threading.Event(), threading.Event()
                writer_errors = []

                def write():
                    try:
                        with closing(sqlite3.connect(database)) as db:
                            db.execute('PRAGMA journal_mode=WAL')
                            db.execute('CREATE TABLE commits (id INTEGER PRIMARY KEY)')
                            while not stopped.is_set():
                                db.execute('INSERT INTO commits DEFAULT VALUES')
                                db.commit()
                                ready.set()
                    except Exception as error:
                        writer_errors.append(error)
                        ready.set()

                writer = threading.Thread(target=write)
                writer.start()
                self.assertTrue(ready.wait(5), 'SQLite writer did not start')
                services = dict(zip(lifecycle.UNITS, ('active', 'inactive')))
                commands = []
                rows_at_stop = []
                real_run = lifecycle.run

                def run(*command, **kwargs):
                    commands.append(command)
                    if command[:2] == ('dpkg-deb', '-f'):
                        text, code = lifecycle.PACKAGE, 0
                    elif command[:2] == ('dpkg-query', '-W') or command[:2] == ('dpkg-query', '-s'):
                        text, code = 'not-installed', 1
                    elif command[:2] == ('apt-get', '-s'):
                        self.assertTrue(writer.is_alive())
                        self.assertFalse(any(c[:2] == ('systemctl', 'stop') for c in commands))
                        text, code = '', 0
                    elif command[0] == 'systemctl':
                        action, unit = command[1:3]
                        if action == 'is-active':
                            text = services[unit]
                        elif action == 'is-enabled':
                            text = 'enabled' if unit == lifecycle.UNITS[0] else 'disabled'
                        elif action == 'stop':
                            if failed_archive == 'stop' and unit == lifecycle.UNITS[1]:
                                raise TimeoutError('private stop output')
                            stopped.set()
                            writer.join(5)
                            self.assertFalse(writer.is_alive())
                            with closing(sqlite3.connect(database)) as db:
                                rows_at_stop[:] = [db.execute('SELECT count(*) FROM commits').fetchone()[0]]
                            for target in command[2:]:
                                services[target] = 'inactive'
                            text = ''
                        elif action == 'start':
                            services[unit] = 'active'
                            text = ''
                        else:
                            self.fail('unexpected service mutation: ' + action)
                        code = 0
                    elif command[0] == 'tar' and '-cpf' in command:
                        self.assertFalse(writer.is_alive(), 'snapshot raced with SQLite writer')
                        archive = Path(command[command.index('-cpf') + 1])
                        if archive.name == failed_archive:
                            archive.write_bytes(b'partial archive')
                            raise lifecycle.CheckFailed('injected archive failure')
                        return real_run(*command, **kwargs)
                    else:
                        self.fail('unexpected command: ' + str(command))
                    return subprocess.CompletedProcess(command, code, text, '')

                try:
                    with patch.object(lifecycle.os, 'geteuid', return_value=0), \
                            patch.object(lifecycle, 'MODEL', model), \
                            patch.object(lifecycle, 'PATHS', [data]), \
                            patch.object(lifecycle, 'DATA', data), \
                            patch.object(lifecycle.Path, 'glob', return_value=[model]), \
                            patch.object(lifecycle, 'protected_packages', return_value=[]), \
                            patch.object(lifecycle, 'run', side_effect=run), \
                            patch.object(self.job, 'exercise') as exercise, \
                            patch.object(self.job, 'restore') as restore, \
                            patch.object(lifecycle, 'remove_path') as remove:
                        if failed_archive:
                            code, result = self.execute()
                            self.assertEqual(code, 1)
                            self.assertTrue(result['restored'])
                            self.assertIn('TimeoutError' if failed_archive == 'stop'
                                          else 'injected archive failure', result['error'])
                            self.assertNotIn('private stop output', result['error'])
                            self.assertEqual(services, dict(zip(lifecycle.UNITS, ('active', 'inactive'))))
                            exercise.assert_not_called()
                            restore.assert_not_called()
                            remove.assert_not_called()
                            if failed_archive == 'stop':
                                self.assertFalse(any(c[0] == 'tar' for c in commands))
                            self.assertEqual([c for c in commands if c[:2] == ('systemctl', 'start')],
                                             [('systemctl', 'start', lifecycle.UNITS[0])])
                        else:
                            self.job.prepare()
                            self.assertGreater(rows_at_stop[0], 0)
                            extracted = data / 'extracted'
                            extracted.mkdir()
                            with tarfile.open(self.job.backup / 'files.tar') as archive:
                                archive.extractall(extracted, filter='data')
                            snapshot = extracted / str(database).lstrip('/')
                            with closing(sqlite3.connect(snapshot)) as db:
                                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                                self.assertEqual(db.execute('SELECT count(*) FROM commits').fetchone()[0],
                                                 rows_at_stop[0])
                        with closing(sqlite3.connect(database)) as db:
                            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                            self.assertEqual(db.execute('SELECT count(*) FROM commits').fetchone()[0],
                                             rows_at_stop[0])
                        self.assertEqual(model.read_bytes(), b'model')
                        self.assertFalse(writer_errors)
                finally:
                    stopped.set()
                    writer.join(5)

    def test_missing_units_do_not_block_first_install_snapshot(self):
        def run(*command, **kwargs):
            self.assertEqual(command[0], 'systemctl')
            text = {'stop': '', 'is-active': 'inactive', 'is-enabled': 'not-found'}[command[1]]
            return subprocess.CompletedProcess(command, 5, text, '')

        with patch.object(lifecycle, 'run', side_effect=run):
            self.job.stop_services()

    def test_service_only_recovery_reports_failure_and_attempts_other_services(self):
        self.job.services = {unit: {'active': 'active', 'enabled': 'enabled'}
                             for unit in lifecycle.UNITS}
        started = []

        def run(*command, **kwargs):
            if command[1] == 'start':
                started.append(command[2])
                if len(started) == 1:
                    raise TimeoutError('private restart output')
            else:
                self.assertEqual(command[1], 'is-active')
            return subprocess.CompletedProcess(command, 0, 'active', '')

        with patch.object(lifecycle, 'run', side_effect=run), \
                patch.object(lifecycle, 'remove_path') as remove:
            with self.assertRaisesRegex(lifecycle.CheckFailed, 'RESTORE FAILED: TimeoutError'):
                self.job.restore_active_services()
        self.assertEqual(started, list(lifecycle.UNITS))
        self.assertFalse(self.job.restored)
        remove.assert_not_called()

    def test_restore_preserves_safe_failure_reason_but_hides_unknown_exception_text(self):
        for failure, expected in (
            (lifecycle.CheckFailed('original service enablement changed'),
             'original service enablement changed'),
            (OSError('private credentials'), 'OSError'),
        ):
            with self.subTest(expected=expected):
                self.job = lifecycle.Lifecycle(self.args)
                self.job.backup = self.root
                self.job.services = {}
                self.job.original_data = None

                def run(*command, **kwargs):
                    if command == ('systemctl', 'daemon-reload'):
                        raise failure
                    return subprocess.CompletedProcess(command, 0, 'not-installed', '')

                with patch.object(self.job, 'stop_services'), \
                        patch.object(lifecycle, 'PATHS', []), \
                        patch.object(lifecycle, 'DATA', self.root / 'absent'), \
                        patch.object(lifecycle, 'run', side_effect=run):
                    with self.assertRaisesRegex(lifecycle.CheckFailed, 'RESTORE FAILED') as caught:
                        self.job.restore()
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('private credentials', str(caught.exception))
                self.assertFalse(self.job.restored)

    def test_restore_purges_only_gateway_even_with_reverse_dependencies(self):
        self.job.backup = self.root
        self.job.services = {}
        self.job.original_data = None

        def run(*command, **kwargs):
            if command[:2] == ('systemctl', 'is-active'):
                return subprocess.CompletedProcess(command, 0, 'inactive', '')
            if command[0] == 'dpkg-query':
                return subprocess.CompletedProcess(command, 0, 'install ok installed', '')
            if command[0] == 'dpkg':
                self.assertEqual(command, ('dpkg', '--force-depends', '--purge', lifecycle.PACKAGE))
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(lifecycle, 'PATHS', []), \
                patch.object(lifecycle, 'DATA', self.root / 'absent'), \
                patch.object(lifecycle, 'run', side_effect=run) as commands:
            self.job.restore()
        commands.assert_any_call('dpkg', '--force-depends', '--purge', lifecycle.PACKAGE, timeout=180)
        self.assertTrue(self.job.restored)

    def test_restore_stop_failure_never_removes_files_while_service_runs(self):
        for failed_stop in (None, 1, 2):
            for failure in ('timeout', 'exit', 'still-active'):
                with self.subTest(failed_stop=failed_stop, failure=failure):
                    self.job = lifecycle.Lifecycle(self.args)
                    self.job.backup = self.root
                    self.job.old_deb = self.binary
                    self.job.old_digest = lifecycle.sha256(self.binary)
                    self.job.services = {}
                    self.job.original_data = None
                    calls, stops = [], []
                    active = False

                    def run(*command, **kwargs):
                        nonlocal active
                        calls.append(command)
                        if command[:2] == ('systemctl', 'stop'):
                            # 每轮停服会逐一处理两个 unit。
                            if command[2] == lifecycle.UNITS[0]:
                                stops.append(command)
                            if len(stops) == failed_stop:
                                active = True
                                if failure == 'timeout':
                                    raise TimeoutError('sensitive output')
                                if failure == 'exit':
                                    if kwargs.get('check', True):
                                        raise lifecycle.CheckFailed('systemctl failed')
                                    return subprocess.CompletedProcess(command, 1, '', '')
                            active = len(stops) == failed_stop
                        if command[:2] == ('systemctl', 'is-active'):
                            return subprocess.CompletedProcess(command, 0, 'active' if active else 'inactive', '')
                        if command[0] == 'tar' or command[:3] == ('dpkg', '--force-depends', '--purge'):
                            self.assertFalse(active, 'destructive restore while service is active')
                        return subprocess.CompletedProcess(command, 0, 'not-installed', '')

                    with patch.object(lifecycle, 'PATHS', [self.binary]), \
                            patch.object(lifecycle, 'DATA', self.root / 'absent'), \
                            patch.object(lifecycle, 'remove_path') as remove, \
                            patch.object(lifecycle, 'run', side_effect=run):
                        if failed_stop:
                            with self.assertRaisesRegex(lifecycle.CheckFailed, 'RESTORE FAILED'):
                                self.job.restore()
                        else:
                            self.job.restore()
                    self.assertEqual(self.job.restored, failed_stop is None)
                    expected_copies = 2 if failed_stop is None else failed_stop - 1
                    self.assertEqual(remove.call_count, expected_copies)
                    tar_calls = [i for i, c in enumerate(calls) if c[0] == 'tar']
                    self.assertEqual(len(tar_calls), expected_copies)
                    if expected_copies:
                        install = next(i for i, c in enumerate(calls) if c[0] == 'dpkg' and '-i' in c)
                        self.assertLess(tar_calls[0], install)
                    self.assertEqual(self.binary.read_bytes(), b'test artifact')


if __name__ == '__main__':
    unittest.main()
