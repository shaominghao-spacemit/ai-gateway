"""生命周期验收脚本的宿主边界测试；不执行真实包或服务操作。"""

import argparse
import base64
import errno
import os
import struct
import sys
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import threading
import tempfile
from types import ModuleType, SimpleNamespace
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

    def test_upgrade_checks_original_listener_and_auth_instead_of_fresh_defaults(self):
        original = {'listeners': {18790: ['0.0.0.0:18790'], 8326: ['127.0.0.1:8326']},
                    'unauthenticated_status': 404}
        self.job.original_contract = original
        for address, status, accepted in (('0.0.0.0', 404, True),
                                          ('127.0.0.1', 404, False),
                                          ('0.0.0.0', 401, False)):
            with self.subTest(address=address, status=status):
                responses = [subprocess.CompletedProcess([], 0, '1', ''),
                             subprocess.CompletedProcess([], 0, '2', ''),
                             subprocess.CompletedProcess([], 0,
                                 f'LISTEN 0 100 {address}:18790 0.0.0.0:*\n'
                                 'LISTEN 0 100 127.0.0.1:8326 0.0.0.0:*', '')]
                with patch.object(Path, 'read_text', return_value='Uid:\t1000\t1000\t1000\t1000\n'), \
                        patch.object(lifecycle, 'run', side_effect=responses), \
                        patch.object(self.job, 'health'), \
                        patch.object(lifecycle, 'request', return_value=(status, b'')):
                    if accepted:
                        self.job.check_services()
                    else:
                        with self.assertRaisesRegex(lifecycle.CheckFailed, 'original listener/auth'):
                            self.job.check_services()
        self.assertIn('original-listener-auth-contract-preserved', self.job.checks)

    def test_packaged_credential_parser_accepts_systemd_double_quoted_json(self):
        path = self.root / 'credentials.env'
        path.write_text('SPACEMIT_AI_GATEWAY_AUTH__API_KEYS="[\\"secret\\"]"\n')
        self.assertEqual(lifecycle.credential_key(path), 'secret')

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

    def test_legacy_database_discovery_uses_original_environment_and_cwd(self):
        app = self.root / 'old-app'
        (app / 'venv/bin').mkdir(parents=True)
        (app / 'venv/bin/python').symlink_to(sys.executable)
        package = self.root / 'spacemit_ai_gateway/app'
        package.mkdir(parents=True)
        (package / 'settings.py').write_text(
            'import json, os\nfrom types import SimpleNamespace\n'
            'def get_settings():\n'
            '    paths = json.loads(os.environ["ORIGINAL_DB_PATHS"])\n'
            '    return SimpleNamespace(**{domain: SimpleNamespace(storage=SimpleNamespace(db_path=path))\n'
            '        for domain, path in zip(("llm", "embed", "rerank", "vlm"), paths)})\n')
        cwd = self.root / 'original-working-directory'
        cwd.mkdir()
        home = self.root / 'original-home'
        paths = ['~/.cache/llm/db.sqlite', 'relative/embed.sqlite',
                 str(self.root / 'absolute-rerank.sqlite'), '~/.cache/vlm/db.sqlite']
        environment = dict(os.environ, HOME=str(home), PYTHONPATH=str(self.root),
                           ORIGINAL_DB_PATHS=json.dumps(paths))
        with subprocess.Popen([sys.executable, '-c',
                               'import sys; print("ready", flush=True); sys.stdin.read()'],
                              cwd=cwd, env=environment, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                with patch.object(lifecycle, 'APP', app), \
                        patch.object(lifecycle, 'run', return_value=subprocess.CompletedProcess(
                            [], 0, str(process.pid), '')):
                    discovered = self.job.legacy_databases()
            finally:
                process.terminate()
                process.communicate(timeout=5)
        self.assertEqual(discovered, [str(home / '.cache/llm/db.sqlite'),
                                      str(cwd / 'relative/embed.sqlite'), paths[2],
                                      str(home / '.cache/vlm/db.sqlite')])

    def test_legacy_database_discovery_rejects_invalid_or_private_output(self):
        for result in (
            subprocess.CompletedProcess([], 1, '', 'password=private-original-settings'),
            subprocess.CompletedProcess([], 0, 'token=private-original-settings', ''),
            subprocess.CompletedProcess([], 0, json.dumps(['/absolute'] * 3), ''),
            subprocess.CompletedProcess([], 0, json.dumps(['relative'] * 4), ''),
            subprocess.CompletedProcess([], 0, json.dumps([42] * 4), ''),
            subprocess.CompletedProcess([], 0, json.dumps({'db': '/absolute'}), ''),
        ):
            with self.subTest(returncode=result.returncode, shape=result.stdout[:1]):
                with patch.object(lifecycle, 'run', return_value=subprocess.CompletedProcess(
                        [], 0, str(os.getpid()), '')), \
                        patch.object(lifecycle.subprocess, 'run', return_value=result):
                    with self.assertRaises(lifecycle.CheckFailed) as caught:
                        self.job.legacy_databases()
                self.assertNotIn('private-original-settings', str(caught.exception))

    def test_external_database_and_sidecars_are_restored_without_touching_models(self):
        self.backup()
        directory = self.root / 'original-home/.cache/gateway/llm'
        directory.mkdir(parents=True)
        database = directory / 'registered.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE models (id TEXT)')
            connection.execute("INSERT INTO models VALUES ('original')")
            connection.commit()
        database.chmod(0o640)
        os.setxattr(database, 'user.gateway-test', b'original metadata')
        wal = Path(str(database) + '-wal')
        wal.write_bytes(b'original-wal')
        model = directory / 'weights.gguf'
        model.write_bytes(b'not part of database backup')
        original = database.read_bytes()
        with patch.object(lifecycle, 'run', wraps=lifecycle.run) as commands:
            self.job.snapshot_external_databases([str(database)])
        archive_command = commands.call_args.args
        self.assertEqual(archive_command[archive_command.index('-C') + 2], '--')
        evidence = self.job.backup / 'external-databases.json'
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
        database.write_bytes(b'changed after real inference')
        database.chmod(0o600)
        os.setxattr(database, 'user.gateway-test', b'changed metadata')
        wal.unlink()
        Path(str(database) + '-shm').write_bytes(b'new sidecar')
        self.job.restore_external_databases()
        self.assertEqual(database.read_bytes(), original)
        self.assertEqual(database.stat().st_mode & 0o777, 0o640)
        self.assertEqual(os.getxattr(database, 'user.gateway-test'), b'original metadata')
        self.assertEqual(wal.read_bytes(), b'original-wal')
        self.assertFalse(Path(str(database) + '-shm').exists())
        self.assertEqual(model.read_bytes(), b'not part of database backup')
        # 旧 postinst 再次改写后，还原必须可重入，不能复用首次恢复的成功结论。
        database.write_bytes(b'old postinst changed it again')
        self.job.restore_external_databases()
        self.assertEqual(database.read_bytes(), original)

    def test_external_database_absence_and_missing_parent_are_restored(self):
        self.backup()
        database = self.root / 'missing-home/cache/domain/db.sqlite'
        self.job.snapshot_external_databases([str(database)])
        database.parent.mkdir(parents=True)
        database.write_bytes(b'new database')
        Path(str(database) + '-journal').write_bytes(b'new journal')
        self.job.restore_external_databases()
        self.assertFalse((self.root / 'missing-home').exists())

    def test_external_database_symlinks_and_parent_replacement_are_rejected(self):
        for sidecar in (False, True):
            with self.subTest(sidecar=sidecar):
                self.job = lifecycle.Lifecycle(self.args)
                self.job.backup = self.root
                directory = self.root / ('sidecar' if sidecar else 'database')
                directory.mkdir()
                database = directory / 'db.sqlite'
                victim = directory / 'model.gguf'
                victim.write_bytes(b'never remove')
                database.write_bytes(b'db')
                symlink = Path(str(database) + '-wal') if sidecar else database
                symlink.unlink(missing_ok=True)
                symlink.symlink_to(victim)
                with self.assertRaisesRegex(lifecycle.CheckFailed, 'database.*regular'):
                    self.job.snapshot_external_databases([str(database)])
                self.assertEqual(victim.read_bytes(), b'never remove')
        directory = self.root / 'replaced'
        directory.mkdir()
        database = directory / 'db.sqlite'
        database.write_bytes(b'original')
        self.job.snapshot_external_databases([str(database)])
        directory.rename(self.root / 'saved-parent')
        directory.mkdir()
        database.write_bytes(b'other owner')
        with self.assertRaisesRegex(lifecycle.CheckFailed, 'parent.*changed'):
            self.job.restore_external_databases()
        self.assertEqual(database.read_bytes(), b'other owner')

    def test_external_database_hardlinks_and_symlink_parent_are_rejected(self):
        for suffix in ('', '-wal'):
            with self.subTest(suffix=suffix):
                directory = self.root / ('main' if not suffix else 'wal')
                directory.mkdir()
                database = directory / 'db.sqlite'
                target = Path(str(database) + suffix)
                target.write_bytes(b'original')
                (directory / 'other-owner').hardlink_to(target)
                with self.assertRaisesRegex(lifecycle.CheckFailed, 'single regular file'):
                    self.job.snapshot_external_databases([str(database)])
                self.assertEqual(target.read_bytes(), b'original')
        directory = self.root / 'directory'
        directory.mkdir()
        (self.root / 'alias').symlink_to(directory, target_is_directory=True)
        with self.assertRaisesRegex(lifecycle.CheckFailed, 'parent must be a directory'):
            self.job.snapshot_external_databases([str(self.root / 'alias/db.sqlite')])

    def test_external_restore_validates_all_targets_before_deleting_any(self):
        self.backup()
        database = self.root / 'db.sqlite'
        database.write_bytes(b'original')
        self.job.snapshot_external_databases([str(database)])
        database.write_bytes(b'current')
        model = self.root / 'model.gguf'
        model.write_bytes(b'other owner')
        Path(str(database) + '-wal').symlink_to(model)
        with self.assertRaisesRegex(lifecycle.CheckFailed, 'single regular file'):
            self.job.restore_external_databases()
        self.assertEqual(database.read_bytes(), b'current')
        self.assertEqual(model.read_bytes(), b'other owner')

    def test_external_database_restore_error_cannot_report_restored(self):
        self.backup()
        self.job.services = {}
        self.job.original_data = None
        self.job.mutated = True
        with patch.object(self.job, 'prepare'), patch.object(self.job, 'exercise'), \
                patch.object(lifecycle, 'PATHS', []), \
                patch.object(lifecycle, 'DATA', self.root / 'absent'), \
                patch.object(lifecycle, 'run', return_value=subprocess.CompletedProcess([], 0, 'inactive', '')), \
                patch.object(self.job, 'restore_external_databases',
                             side_effect=lifecycle.CheckFailed('external database restore failed')):
            code, result = self.execute()
        self.assertEqual(code, 1)
        self.assertFalse(result['restored'])
        self.assertIn('RESTORE FAILED', result['error'])

    def test_prepare_snapshots_original_home_databases_after_stopping_writer(self):
        database = self.root / 'original-home/db.sqlite'
        database.parent.mkdir()
        database.write_bytes(b'original database')
        model = self.root / 'silero.onnx'
        model.write_bytes(b'model')
        services = dict(zip(lifecycle.UNITS, ('active', 'inactive')))
        real_run = lifecycle.run
        observed = []

        def databases():
            self.assertEqual(services[lifecycle.UNITS[0]], 'active')
            observed.append('read-live-config')
            return [str(database)]

        def run(*command, **kwargs):
            code, text = 0, ''
            if command[:2] == ('dpkg-deb', '-f'):
                text = lifecycle.PACKAGE if command[-1] == 'Package' else '0.0.19'
            elif command[:2] == ('dpkg-query', '-W'):
                text = 'install ok installed\n0.0.19'
            elif command[:2] == ('apt-get', 'download'):
                (self.job.backup / 'old.deb').write_bytes(b'rollback')
            elif command[:2] == ('dpkg-deb', '-e'):
                Path(command[-1]).mkdir()
            elif command[0] == 'systemctl':
                action, unit = command[1:3]
                if action == 'is-active':
                    text = services[unit]
                elif action == 'is-enabled':
                    text = 'enabled' if unit == lifecycle.UNITS[0] else 'disabled'
                elif action == 'stop':
                    services[unit] = 'inactive'
                    observed.append('stop-' + unit)
            elif command[0] == 'tar':
                self.assertTrue(all(state == 'inactive' for state in services.values()))
                observed.append(Path(command[command.index('-cpf') + 1]).name)
                return real_run(*command, **kwargs)
            return subprocess.CompletedProcess(command, code, text, '')

        with patch.object(lifecycle.os, 'geteuid', return_value=0), \
                patch.object(lifecycle, 'MODEL', model), patch.object(lifecycle, 'PATHS', []), \
                patch.object(lifecycle, 'DATA', self.root / 'absent'), \
                patch.object(lifecycle, 'run', side_effect=run), \
                patch.object(lifecycle, 'protected_packages', return_value=[]), \
                patch.object(self.job, 'legacy_databases', side_effect=databases), \
                patch.object(self.job, 'service_contract', return_value={'original': True}):
            self.job.prepare()
        self.assertLess(observed.index('read-live-config'), observed.index('stop-' + lifecycle.UNITS[0]))
        self.assertLess(observed.index('stop-' + lifecycle.UNITS[1]), observed.index('external-databases.tar'))
        self.assertTrue((self.job.backup / 'external-databases.json').is_file())

    def test_file_restore_failures_block_reinstall_and_service_start(self):
        self.backup()
        for failed_stage in ('files-first', 'database-first', 'files-second',
                             'database-second', 'daemon-reload'):
            with self.subTest(stage=failed_stage):
                self.job = lifecycle.Lifecycle(self.args)
                self.job.backup = Path(self.args.evidence_dir) / 'gateway-backup-test'
                self.job.old_deb = self.binary
                self.job.old_digest = lifecycle.sha256(self.binary)
                self.job.services = {unit: {'active': 'active', 'enabled': 'enabled'}
                                     for unit in lifecycle.UNITS}
                self.job.original_data = None
                stages = []

                def run(*command, **kwargs):
                    if command[0] == 'tar':
                        stage = 'files-first' if 'files-first' not in stages else 'files-second'
                    elif command == ('systemctl', 'daemon-reload'):
                        stage = 'daemon-reload'
                    elif command[0] == 'dpkg' and '-i' in command:
                        stage = 'reinstall'
                    else:
                        stage = 'other'
                    stages.append(stage)
                    if stage == failed_stage:
                        raise lifecycle.CheckFailed('file recovery failed')
                    return subprocess.CompletedProcess(command, 0, 'inactive', '')

                def databases():
                    stage = 'database-first' if 'database-first' not in stages else 'database-second'
                    stages.append(stage)
                    if stage == failed_stage:
                        raise lifecycle.CheckFailed('database recovery failed')

                with patch.object(lifecycle, 'PATHS', []), \
                        patch.object(lifecycle, 'DATA', self.root / 'absent'), \
                        patch.object(lifecycle, 'run', side_effect=run) as commands, \
                        patch.object(self.job, 'restore_external_databases', side_effect=databases):
                    with self.assertRaisesRegex(lifecycle.CheckFailed, 'RESTORE FAILED'):
                        self.job.restore()
                self.assertFalse(self.job.restored)
                self.assertFalse(any(call.args[:2] == ('systemctl', 'start')
                                     for call in commands.call_args_list))
                self.assertEqual('reinstall' in stages, not failed_stage.endswith('-first'))

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


class StorageAclRollbackTests(unittest.TestCase):
    """真实临时文件 ACL + 模拟包操作，不访问宿主系统目录。"""

    def setUp(self):
        LifecycleTests.setUp(self)
        self.etc = self.root / 'etc'
        self.etc.mkdir()
        self.journal = self.etc / 'legacy-storage-acl.jsonl'
        self.external = self.root / 'private-home'
        self.external.mkdir(mode=0o700)
        self.existing = self.root / 'previously-authorized'
        self.existing.mkdir(mode=0o750)
        LifecycleTests.backup(self)
        self.job.services = {}
        self.job.original_data = None
        self.commands = []
        self.patchers = [patch.object(lifecycle, 'ETC', self.etc),
                         patch.object(lifecycle, 'PATHS', [self.etc]),
                         patch.object(lifecycle, 'DATA', self.root / 'absent'),
                         patch.object(lifecycle, 'MODEL', self.binary),
                         patch.object(lifecycle, 'UNITS', ()),
                         patch.object(lifecycle, 'protected_packages', return_value=[]),
                         patch.object(lifecycle, 'run', side_effect=self.run_command)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.access = 'system.posix_acl_access'
        self.default = 'system.posix_acl_default'
        try:
            self.grant(self.existing, 12345)
        except OSError as error:
            if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
                self.skipTest('temporary filesystem does not support POSIX ACL')
            raise
        self.existing_acl = os.getxattr(self.existing, self.access)
        # 该历史记录中的原值为 None，但验收前授权必须保留。
        self.journal.write_text(json.dumps(self.record(self.existing, self.access, None, 0o750)) + '\n')
        self.journal.chmod(0o600)
        self.before = self.journal.read_bytes()

    def grant(self, path, uid, attribute='system.posix_acl_access', permissions=5):
        entries = [(1, 7, 0xffffffff), (2, permissions, uid), (4, 0, 0xffffffff),
                   (16, permissions, 0xffffffff), (32, 0, 0xffffffff)]
        value = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *row) for row in entries)
        os.setxattr(path, attribute, value)
        return value

    def record(self, path, attribute, acl, mode):
        return {'path': str(path), 'attribute': attribute, 'mode': mode,
                'acl': base64.b64encode(acl).decode() if acl is not None else None}

    def snapshot(self):
        with patch.object(lifecycle.os, 'geteuid', return_value=0):
            self.job.prepare()
        self.commands.clear()

    def append_change(self, path=None, attribute=None, permissions=5):
        path, attribute = path or self.external, attribute or self.access
        try:
            original = os.getxattr(path, attribute)
        except OSError as error:
            if error.errno != errno.ENODATA:
                raise
            original = None
        mode = path.stat().st_mode & 0o7777
        with self.journal.open('a') as stream:
            stream.write(json.dumps(self.record(path, attribute, original, mode)) + '\n')
        self.grant(path, 23456, attribute, permissions)
        return original, mode

    def run_command(self, *command, **kwargs):
        self.commands.append(command)
        if command[:2] == ('dpkg-deb', '-f'):
            return subprocess.CompletedProcess(command, 0, lifecycle.PACKAGE, '')
        if command[0] == 'tar' and '-xpf' in command:
            self.etc.mkdir(exist_ok=True)
            self.journal.write_bytes(self.before)
            self.journal.chmod(0o600)
        if command[0] == 'dpkg' and '-i' in command:
            # 旧 deb 重装前必须完成首轮 ACL 恢复；其 postinst 可再次写 journal。
            self.assertEqual(self.external.stat().st_mode & 0o7777, 0o700)
            self.append_change()
        return subprocess.CompletedProcess(command, 1 if command[0] == 'dpkg-query' else 0,
                                           'not-installed', '')

    def test_new_external_acls_are_restored_but_prior_authorization_survives(self):
        self.snapshot()
        self.append_change()
        self.append_change(attribute=self.default)
        self.job.restore()
        self.assertTrue(self.job.restored)
        for attribute in (self.access, self.default):
            with self.assertRaises(OSError) as caught:
                os.getxattr(self.external, attribute)
            self.assertEqual(caught.exception.errno, errno.ENODATA)
        self.assertEqual(self.external.stat().st_mode & 0o7777, 0o700)
        self.assertEqual(os.getxattr(self.existing, self.access), self.existing_acl)
        self.assertEqual(self.journal.read_bytes(), self.before)

    def test_original_binary_acl_and_mode_are_restored_exactly(self):
        self.grant(self.external, 34567)
        self.external.chmod(0o2750)
        original = os.getxattr(self.external, self.access)
        self.snapshot()
        self.append_change()
        self.job.restore()
        self.assertEqual(os.getxattr(self.external, self.access), original)
        self.assertEqual(self.external.stat().st_mode & 0o7777, 0o2750)

    def test_old_deb_reinstall_acl_writes_are_rolled_back_after_reinstall(self):
        self.snapshot()
        self.append_change()
        self.job.old_deb = self.binary
        self.job.old_digest = lifecycle.sha256(self.binary)
        self.job.restore()
        self.assertTrue(self.job.restored)
        self.assertEqual(self.external.stat().st_mode & 0o7777, 0o700)
        with self.assertRaises(OSError):
            os.getxattr(self.external, self.access)
        self.assertEqual(self.journal.read_bytes(), self.before)

    def test_acl_restore_failure_is_explicit_and_preserves_journal_evidence(self):
        self.snapshot()
        self.append_change()
        with patch.object(lifecycle.os, 'removexattr', side_effect=PermissionError('private-home secret')):
            with self.assertRaisesRegex(lifecycle.CheckFailed, 'RESTORE FAILED') as caught:
                self.job.restore()
        self.assertNotIn('private-home', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))
        self.assertFalse(self.job.restored)
        self.assertIn(str(self.external), self.journal.read_text())
        self.assertFalse(any(command[0] in ('tar', 'dpkg') for command in self.commands))

    def test_prepare_snapshot_keeps_source_identity_and_content_private(self):
        original = self.journal.stat()
        self.snapshot()
        saved = json.loads((self.job.backup / 'storage-acl-before.json').read_text())
        self.assertEqual(saved['identity'], [original.st_dev, original.st_ino])
        self.assertEqual(base64.b64decode(saved['content']), self.before)
        self.assertEqual((self.job.backup / 'storage-acl-before.json').stat().st_mode & 0o777, 0o600)

    def test_modified_history_is_not_reinterpreted_as_new_grants(self):
        self.snapshot()
        self.journal.write_text('')
        self.append_change()
        with self.assertRaisesRegex(lifecycle.CheckFailed, 'RESTORE FAILED'):
            self.job.restore()
        self.assertFalse(self.job.restored)
        self.assertFalse(any(command[0] in ('tar', 'dpkg') for command in self.commands))


    def test_external_readonly_model_and_0750_home_traversal_are_restored(self):
        self.external.chmod(0o750)
        cache = self.external / '.cache'
        cache.mkdir(mode=0o700)
        model = cache / '模型.gguf'
        model.write_bytes(b'original model bytes')
        model.chmod(0o640)
        self.snapshot()
        for path, permission in ((self.external, 1), (cache, 5), (model, 4)):
            self.append_change(path, permissions=permission)
        self.job.restore()
        for path, mode in ((self.external, 0o750), (cache, 0o700), (model, 0o640)):
            self.assertEqual(path.stat().st_mode & 0o7777, mode)
            with self.assertRaises(OSError) as caught:
                os.getxattr(path, self.access)
            self.assertEqual(caught.exception.errno, errno.ENODATA)
        self.assertEqual(model.read_bytes(), b'original model bytes')
        self.assertEqual(os.getxattr(self.existing, self.access), self.existing_acl)

    def test_first_migration_without_previous_journal_is_rolled_back(self):
        self.journal.unlink()
        self.before = b''
        self.snapshot()
        self.journal.touch(mode=0o600)
        self.append_change()
        self.job.restore()
        self.assertEqual(self.external.stat().st_mode & 0o7777, 0o700)
        with self.assertRaises(OSError):
            os.getxattr(self.external, self.access)

    def test_atomic_journal_replacement_and_incomplete_tail_are_supported(self):
        self.snapshot()
        self.append_change()
        replacement = self.journal.with_suffix('.replacement')
        replacement.write_bytes(self.journal.read_bytes() + b'{"path":"incomplete')
        replacement.chmod(0o600)
        replacement.replace(self.journal)
        self.job.restore()
        self.assertEqual(self.external.stat().st_mode & 0o7777, 0o700)
        self.assertEqual(self.journal.read_bytes(), self.before)

    def test_journal_symlink_or_public_permissions_are_rejected_before_install(self):
        self.journal.chmod(0o644)
        with self.assertRaises(lifecycle.CheckFailed):
            self.snapshot()
        self.assertFalse(any(command[0] == 'dpkg' for command in self.commands))
        self.journal.chmod(0o600)
        original = self.journal.with_suffix('.original')
        self.journal.rename(original)
        self.journal.symlink_to(original)
        with self.assertRaises(OSError):
            self.snapshot()

    def test_acl_failure_public_summary_contains_no_source_paths_or_acl(self):
        self.snapshot()
        self.job.mutated = True
        with patch.object(self.job, 'prepare'), \
                patch.object(self.job, 'exercise', side_effect=self.append_change), \
                patch.object(lifecycle.os, 'removexattr', side_effect=PermissionError('private secret')):
            code, result = LifecycleTests.execute(self)
        self.assertEqual(code, 1)
        self.assertFalse(result['restored'])
        summary = Path(result['log']).read_text()
        self.assertIn('RESTORE FAILED', summary)
        self.assertNotIn(str(self.external), summary)
        self.assertNotIn('private secret', summary)
        self.assertNotIn(base64.b64encode(self.existing_acl).decode(), summary)
        for path in self.job.backup.glob('storage-acl-*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ManagedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = self.root / 'cases.json'
        self.args = argparse.Namespace(managed_cases=str(self.cases))
        self.job = lifecycle.Lifecycle(self.args)

    def test_foundation_run_cannot_be_managed_evidence(self):
        self.args.managed_cases = None
        self.job.managed_contract()
        self.assertEqual(self.job.managed_evidence['result'], 'not_run')
        self.assertEqual(self.job.managed_evidence['t18']['result'], 'not_passed')
        self.assertFalse(any('u4-' in item for item in self.job.checks))

    def test_managed_cases_require_speech_expectation_and_both_models_for_t18(self):
        audio = self.root / 'speech.wav'
        audio.write_bytes(b'fixture-speech')
        case = {'model': 'qwen3_asr_0.6b', 'language': 'zh,en', 'audio': str(audio), 'expect_text': '你好'}
        self.cases.write_text(json.dumps([case]))
        def passed(case, audio):
            self.job.managed_evidence['models'].append({'model': case['model'], 'result': 'ok'})
        with patch.object(self.job, 'managed_case', side_effect=passed):
            self.job.managed_contract()
        self.assertEqual(self.job.managed_evidence['result'], 'ok')
        self.assertEqual(self.job.managed_evidence['t18']['result'], 'not_passed')
        del case['expect_text']
        self.cases.write_text(json.dumps([case]))
        # 新一轮验收创建新生命周期；同一轮读取的用例保持不变。
        self.job = lifecycle.Lifecycle(self.args)
        with self.assertRaises(KeyError):
            self.job.managed_contract()
        self.assertEqual(self.job.managed_evidence['result'], 'failed')

    def test_case_digest_is_checked_before_any_service_or_package_change(self):
        self.cases.write_text('[]')
        self.args.managed_cases_sha256 = '0' * 64
        self.args.expect_sha256 = 'a' * 64
        self.args.request_id, self.args.attempt_id, self.args.artifact_id = 'req-test', 'att-test', 'gateway-test'
        with patch.object(self.job, 'prepare') as prepare, \
                patch.object(self.job, 'exercise') as exercise, \
                patch('builtins.print') as output:
            self.assertEqual(self.job.execute(), 1)
        prepare.assert_not_called()
        exercise.assert_not_called()
        summary = json.loads(output.call_args.args[0])
        self.assertTrue(summary['restored'])
        self.assertIn('managed cases digest mismatch', summary['error'])
        self.assertEqual(summary['managed_asr']['result'], 'failed')

    def test_case_bytes_are_frozen_and_digest_is_part_of_evidence(self):
        audio = self.root / 'speech.wav'
        audio.write_bytes(b'fixture-speech')
        original = json.dumps([dict(model='qwen3_asr_0.6b', language='zh',
                                   audio=str(audio), expect_text='你好')]).encode()
        self.cases.write_bytes(original)
        self.args.managed_cases_sha256 = lifecycle.hashlib.sha256(original).hexdigest()
        self.job.read_managed_cases()
        self.cases.write_text('[{"model": "unexpected"}]')
        with patch.object(self.job, 'managed_case') as recognize:
            self.job.managed_contract()
        self.assertEqual(recognize.call_args.args[0]['model'], 'qwen3_asr_0.6b')
        self.assertEqual(self.job.managed_evidence['cases_sha256'], self.args.managed_cases_sha256)

    def test_sensevoice_list_refusal_records_t18_reason(self):
        state = {'reason': 'sensevoice_language_list_unsupported'}
        with patch.object(lifecycle, 'request', return_value=(401, b'')), \
                patch.object(lifecycle, 'api', return_value=state):
            with self.assertRaisesRegex(lifecycle.CheckFailed, 'T18 not passed'):
                self.job.managed_case({'model': 'sensevoice', 'language': 'zh,en'}, b'speech')
        self.assertEqual(self.job.managed_evidence['t18'], {
            'result': 'blocked', 'reason': 'sensevoice_language_list_unsupported'})


    def test_managed_case_confirms_real_flock_and_releases_after_inference_failure(self):
        from types import SimpleNamespace
        import os
        lock = self.root / 'engine.lock'
        lock.touch(mode=0o660)
        engine = SimpleNamespace(
            parent=SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=0, st_gid=123, st_mode=0o2775)),
            stat=lambda: SimpleNamespace(st_uid=0, st_gid=123, st_mode=0o660), open=lock.open)
        self.job.key = 'fixture-key'
        for inference_failure in (False, True):
            with self.subTest(inference_failure=inference_failure):
                fd = None
                released = []
                def api(path, key, body=None, **kwargs):
                    nonlocal fd, owner
                    state = dict(protocol_version=1, model='qwen3_asr_0.6b', languages=['zh'],
                                 assets_ready=True, instance_id='fixture-process', reason='available',
                                 ready=fd is not None, owner=None if fd is None else owner,
                                 resource_state='free' if fd is None else 'owned')
                    if path.endswith('/acquire'):
                        owner = body['owner']
                        fd = os.open(lock, os.O_RDWR)
                        lifecycle.fcntl.flock(fd, lifecycle.fcntl.LOCK_EX | lifecycle.fcntl.LOCK_NB)
                        return dict(state, ready=True, owner=owner, token='fixture-token', resource_state='owned')
                    if path.endswith('/recognize'):
                        self.assertIn(b'fixture-token', body)
                        self.assertIn(b'real speech fixture', body)
                        if inference_failure:
                            raise lifecycle.CheckFailed('transcript failed')
                        return {'text': 'expected speech'}
                    if path.endswith('/release'):
                        self.assertEqual(body, {'owner': owner, 'token': 'fixture-token'})
                        os.close(fd)
                        fd = None
                        released.append(True)
                        return {'released': True}
                    return state
                owner = None
                with patch.object(lifecycle, 'ENGINE_LOCK', engine), \
                        patch.object(lifecycle.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=123)), \
                        patch.object(lifecycle, 'request', side_effect=lambda path, *a: (401 if 'status?' in path else 403, b'')), \
                        patch.object(lifecycle, 'api', side_effect=api):
                    case = dict(model='qwen3_asr_0.6b', language='zh', expect_text='expected')
                    if inference_failure:
                        with self.assertRaisesRegex(lifecycle.CheckFailed, 'transcript failed'):
                            self.job.managed_case(case, b'real speech fixture')
                    else:
                        self.job.managed_case(case, b'real speech fixture')
                self.assertEqual(released, [True])
                self.assertIsNone(fd)
                with lock.open('r+b') as stream:
                    lifecycle.fcntl.flock(stream, lifecycle.fcntl.LOCK_EX | lifecycle.fcntl.LOCK_NB)

    def test_acquire_without_received_token_never_claims_release(self):
        from types import SimpleNamespace
        self.job.key = 'fixture-key'
        engine = SimpleNamespace(
            parent=SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=0, st_gid=123, st_mode=0o2775)),
            stat=lambda: SimpleNamespace(st_uid=0, st_gid=123, st_mode=0o660))
        calls = []
        def api(path, key, body=None, **kwargs):
            calls.append(path)
            if path.endswith('/acquire'):
                raise TimeoutError('connection failed')
            return dict(protocol_version=1, model='qwen3_asr_0.6b', languages=['zh'],
                        assets_ready=True, instance_id='fixture-process', ready=False,
                        owner=None, resource_state='free')
        with patch.object(lifecycle, 'ENGINE_LOCK', engine), \
                patch.object(lifecycle.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=123)), \
                patch.object(lifecycle, 'request', return_value=(401, b'')), \
                patch.object(lifecycle, 'api', side_effect=api):
            with self.assertRaises(TimeoutError):
                self.job.managed_case(dict(model='qwen3_asr_0.6b', language='zh'), b'speech')
        self.assertEqual(sum(path.endswith('/acquire') for path in calls), 2)
        self.assertFalse(any(path.endswith('/release') for path in calls))
        self.assertEqual(self.job.managed_evidence['models'][-1]['resource_release'], 'unconfirmed')


class LegacyIntentTests(unittest.TestCase):
    setUp = LifecycleTests.setUp
    backup = LifecycleTests.backup
    def result(self, **changes):
        return dict(completion='CALL mute on=true', token_count=12,
                    probabilities_count=4, vectors={'browser': [0.6, 0.8],
                    'question': [1.0, 0.0], 'version': [0.0, 1.0]}, **changes)

    def test_compare_real_results_rejects_drift_and_invalid_vectors(self):
        before = self.result()
        self.assertEqual(lifecycle.compare_intent_results(before, before)['browser']['dimension'], 2)
        for field, value in [('token_count', 13), ('completion', 'different'),
                             ('vectors', {'browser': [float('nan')]}),
                             ('vectors', {'browser': [True, 0.8]}),
                             ('vectors', {'browser': [0.6, 0.8, 0.0]}),
                             ('vectors', {'browser': [0.8, 0.6], 'question': [1.0, 0.0],
                                          'version': [0.0, 1.0]})]:
            with self.subTest(field=field, value=value):
                after = {**before, field: value}
                with self.assertRaises(lifecycle.CheckFailed):
                    lifecycle.compare_intent_results(before, after)

    def test_malformed_completion_is_a_contract_failure(self):
        for completion in (None, 42, [], {}):
            with self.subTest(completion=completion):
                result = {**self.result(), 'completion': completion}
                with self.assertRaises(lifecycle.CheckFailed):
                    lifecycle.compare_intent_results(result, result)

    def test_no_original_gateway_skips_client_and_session_detection(self):
        with patch.object(self.job, 'intent_context') as context, \
                patch.object(self.job, 'run_intent_probe') as probe:
            self.job.legacy_intent_probe('after')
        context.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(self.job.legacy_intent_evidence,
                         {'result': 'not_run', 'reason': 'original_upgrade_not_checked'})

    def test_original_config_comparison_precedes_administrator_test_changes(self):
        self.job.old_version = '0.0.19'
        events = []
        def event(name):
            return lambda *args, **kwargs: events.append(name)
        with ExitStack() as stack:
            for name in ('stop_services', 'install', 'restart', 'check_services',
                         'check_storage_permissions'):
                stack.enter_context(patch.object(self.job, name, side_effect=event(name)))
            stack.enter_context(patch.object(self.job, 'legacy_intent_probe',
                side_effect=lambda stage: events.append(stage)))
            stack.enter_context(patch.object(self.job, 'configure_admin',
                side_effect=lifecycle.CheckFailed('end of original configuration checks')))
            stack.enter_context(patch.object(lifecycle, 'run',
                return_value=subprocess.CompletedProcess([], 0, '0.0.19', '')))
            stack.enter_context(patch.object(lifecycle, 'wait_for', side_effect=event('healthy')))
            with self.assertRaisesRegex(lifecycle.CheckFailed, 'end of original'):
                self.job.exercise()
        self.assertEqual(events, ['healthy', 'before', 'stop_services', 'install',
                                 'restart', 'check_services', 'check_storage_permissions', 'after'])
        self.assertTrue(self.job.mutated)

    def test_baseline_probe_failure_restores_without_installing_candidate(self):
        self.job.old_version = '0.0.19'
        def restored():
            self.job.restored = True
        with patch.object(self.job, 'prepare'), \
                patch.object(lifecycle, 'run'), patch.object(lifecycle, 'wait_for'), \
                patch.object(self.job, 'legacy_intent_probe',
                             side_effect=lifecycle.CheckFailed('original model unavailable')), \
                patch.object(self.job, 'install') as install, \
                patch.object(self.job, 'restore', side_effect=restored) as restore:
            code, result = LifecycleTests.execute(self)
        self.assertEqual(code, 1)
        self.assertTrue(result['restored'])
        restore.assert_called_once()
        install.assert_not_called()

    def test_absent_client_is_explicitly_not_run(self):
        with patch.object(self.job, 'intent_context', return_value=None), \
                patch.object(self.job, 'run_intent_probe') as probe:
            self.job.legacy_intent_probe('before')
            self.job.legacy_intent_probe('after')
        probe.assert_not_called()
        self.assertEqual(self.job.legacy_intent_evidence['result'], 'not_run')
        self.assertNotIn('legacy-intent-original-config-real-llm-embedding-upgrade', self.job.checks)

    def test_original_client_and_configuration_must_not_change(self):
        self.backup()
        context = {'version': '0.1.87', 'identity': {'source': 'before'}}
        with patch.object(self.job, 'intent_context', side_effect=[context,
                {**context, 'identity': {'source': 'changed'}}]), \
                patch.object(self.job, 'run_intent_probe', return_value=self.result()) as probe:
            self.job.legacy_intent_probe('before')
            with self.assertRaisesRegex(lifecycle.CheckFailed, 'original Intent'):
                self.job.legacy_intent_probe('after')
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(self.job.legacy_intent_evidence['result'], 'failed')

    def test_success_has_private_vectors_and_public_comparison(self):
        self.backup()
        with patch.object(self.job, 'intent_context', return_value={'version': '0.1.87'}), \
                patch.object(self.job, 'run_intent_probe', return_value=self.result()):
            self.job.legacy_intent_probe('before')
            self.assertNotEqual(self.job.legacy_intent_evidence['result'], 'ok')
            self.job.legacy_intent_probe('after')
        self.assertEqual(self.job.legacy_intent_evidence['result'], 'ok')
        self.assertNotIn('vectors', json.dumps(self.job.legacy_intent_evidence))
        for stage in ('before', 'after'):
            saved = self.job.backup / ('legacy-intent-' + stage + '.json')
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(saved.read_text()), self.result())
        self.assertIn('legacy-intent-original-config-real-llm-embedding-upgrade', self.job.checks)

    def test_fixed_probe_uses_systemd_environment_file_and_nonroot_identity(self):
        self.backup()
        context = dict(uid=1000, gid=1000, home='/home/test-user', version='0.1.87')
        calls = []
        def run(*command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0,
                json.dumps(self.result()) if command[0] == 'systemd-run' else 'inactive\n', '')
        with patch.object(lifecycle, 'run', side_effect=run):
            self.assertEqual(self.job.run_intent_probe(context), self.result())
        command, kwargs = calls[0]
        self.assertIn('--uid=1000', command)
        self.assertIn('--gid=1000', command)
        self.assertIn('EnvironmentFile=/etc/bibit-intent/inference.env', command)
        self.assertIn('/opt/bibit-intent/.venv/bin/python', command)
        self.assertIn('-B', command)
        self.assertLessEqual(kwargs['timeout'], 450)
        self.assertTrue(any(c[0][:2] == ('systemctl', 'stop') for c in calls))

    def test_context_requires_a_complete_original_installation(self):
        app = self.root / 'original-intent'
        config = self.root / 'inference.env'
        files = [config, app / 'main.py', app / '.venv/pyvenv.cfg', app / 'src/client.py']
        for file in files:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('original bytes\n')
        calls = []
        def run(*command, **kwargs):
            calls.append(command)
            if command[0] == 'dpkg-query':
                return subprocess.CompletedProcess(command, 0, 'install ok installed\n0.1.89')
            if command[1] == 'list-sessions':
                return subprocess.CompletedProcess(command, 0, '1 ignored\n2 ignored\n3 ignored\n')
            return subprocess.CompletedProcess(command, 0,
                'Type=wayland\nUser=1000\n' if command[2] != '3' else 'Type=tty\nUser=0\n')
        account = SimpleNamespace(pw_uid=1000, pw_gid=1001, pw_dir='/home/original')
        with patch.object(lifecycle, 'INTENT_APP', app), \
                patch.object(lifecycle, 'INTENT_CONFIG', config), \
                patch.object(lifecycle, 'run', side_effect=run), \
                patch.object(lifecycle.pwd, 'getpwuid', return_value=account) as lookup:
            context = self.job.intent_context()
            self.assertEqual(context, dict(version='0.1.89', uid=1000, gid=1001,
                home='/home/original', identity={str(file): lifecycle.sha256(file) for file in files}))
            lookup.assert_called_once_with(1000)
            (app / 'main.py').unlink()
            calls.clear()
            with self.assertRaisesRegex(lifecycle.CheckFailed, 'installation/configuration missing'):
                self.job.intent_context()
            self.assertEqual(len(calls), 1)

    def test_context_rejects_missing_or_ambiguous_graphical_users(self):
        app = self.root / 'original-intent'
        config = self.root / 'inference.env'
        for file in (config, app / 'main.py', app / '.venv/pyvenv.cfg', app / 'src/client.py'):
            file.parent.mkdir(parents=True, exist_ok=True)
            file.touch()
        for users in ([], [('tty', 1000)], [('wayland', 0)],
                      [('wayland', 1000), ('x11', 1001)]):
            with self.subTest(users=users):
                def run(*command, **kwargs):
                    if command[0] == 'dpkg-query':
                        output = 'install ok installed\n0.1.89'
                    elif command[1] == 'list-sessions':
                        output = ''.join(f'{index} ignored\n' for index in range(len(users)))
                    else:
                        kind, uid = users[int(command[2])]
                        output = f'Type={kind}\nUser={uid}\n'
                    return subprocess.CompletedProcess(command, 0, output)
                with patch.object(lifecycle, 'INTENT_APP', app), \
                        patch.object(lifecycle, 'INTENT_CONFIG', config), \
                        patch.object(lifecycle, 'run', side_effect=run), \
                        patch.object(lifecycle.pwd, 'getpwuid') as lookup:
                    with self.assertRaisesRegex(lifecycle.CheckFailed, 'one graphical session user'):
                        self.job.intent_context()
                lookup.assert_not_called()

    def test_uninstalled_client_does_not_read_files_or_sessions(self):
        for code, status in ((1, ''), (0, 'deinstall ok config-files\n0.1.89')):
            with self.subTest(status=status), patch.object(lifecycle, 'run', return_value=
                    subprocess.CompletedProcess([], code, status)) as run:
                self.assertIsNone(self.job.intent_context())
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], 'dpkg-query')

    def test_probe_timeout_still_stops_and_confirms_transient_unit(self):
        self.backup()
        context = dict(uid=1000, gid=1000, home='/home/test-user', version='0.1.89')
        calls = []
        def run(*command, **kwargs):
            calls.append(command)
            if command[0] == 'systemd-run':
                raise subprocess.TimeoutExpired(command, 450)
            return subprocess.CompletedProcess(command, 0, 'inactive\n')
        with patch.object(lifecycle, 'run', side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.job.run_intent_probe(context)
        self.assertEqual([command[:2] for command in calls[1:]],
                         [('systemctl', 'stop'), ('systemctl', 'is-active')])
        self.assertEqual(calls[1][2], calls[2][2])

    def test_probe_does_not_succeed_if_transient_unit_is_still_active(self):
        self.backup()
        context = dict(uid=1000, gid=1000, home='/home/test-user', version='0.1.89')
        def run(*command, **kwargs):
            return subprocess.CompletedProcess(command, 0,
                json.dumps(self.result()) if command[0] == 'systemd-run' else 'active\n', '')
        with patch.object(lifecycle, 'run', side_effect=run):
            with self.assertRaisesRegex(lifecycle.CheckFailed, 'probe did not stop'):
                self.job.run_intent_probe(context)

    def test_probe_failure_does_not_disclose_subprocess_output(self):
        self.backup()
        context = dict(uid=1000, gid=1000, home='/home/test-user', version='0.1.87')
        def run(*command, **kwargs):
            return subprocess.CompletedProcess(command, 1 if command[0] == 'systemd-run' else 0,
                'inactive\n', 'api_key=private-probe-key')
        with patch.object(lifecycle, 'run', side_effect=run):
            with self.assertRaises(lifecycle.CheckFailed) as error:
                self.job.run_intent_probe(context)
        self.assertNotIn('private-probe-key', str(error.exception))


class FixedIntentProbeTests(unittest.TestCase):
    def setUp(self):
        self.backends = ModuleType('bibit_intent.models.backends')
        self.pipeline = ModuleType('bibit_intent.pipeline')
        self.config = SimpleNamespace(inference_backend='ai_gateway',
                                      gateway_url='http://127.0.0.1:18790')
        self.pipeline.PipelineSettings = Mock()
        self.pipeline.PipelineSettings.from_environment.return_value = self.config
        self.local, self.embed = Mock(), Mock()
        self.local.is_available.return_value = self.embed.is_available.return_value = True
        self.local._post.return_value = dict(content='CALL mute on=true',
                                             completion_probabilities=[{}, {}])
        self.local._tokenize.return_value = 12
        self.embed.embed.return_value = [0.6, 0.8]
        self.backends.local_client = Mock(return_value=self.local)
        self.backends.embedding_client = Mock(return_value=self.embed)

    def execute(self, uid=1000):
        output, errors = io.StringIO(), io.StringIO()
        modules = {'bibit_intent.models.backends': self.backends,
                   'bibit_intent.pipeline': self.pipeline}
        with patch.dict(sys.modules, modules), patch.object(sys, 'path', sys.path.copy()), \
                patch.object(os, 'geteuid', return_value=uid), \
                redirect_stdout(output), redirect_stderr(errors):
            try:
                exec(compile(lifecycle.INTENT_PROBE, '<fixed-intent-probe>', 'exec'), {})
            except SystemExit as error:
                code = error.code
            else:
                code = 0
        return code, output.getvalue(), errors.getvalue()

    def test_original_factories_receive_original_settings_and_fixed_inputs(self):
        code, output, errors = self.execute()
        self.assertEqual((code, errors), (0, ''))
        result = json.loads(output)
        self.assertEqual(lifecycle.compare_intent_results(result, result)['browser']['dimension'], 2)
        self.backends.local_client.assert_called_once_with(self.config)
        self.backends.embedding_client.assert_called_once_with(self.config)
        self.local._post.assert_called_once_with('/completion', {
            'prompt': '请输出 CALL mute on=true', 'grammar': 'root ::= "CALL mute on=true"',
            'n_probs': 10, 'n_predict': 30, 'temperature': 0})
        self.local._tokenize.assert_called_once_with('你好，帮我打开浏览器')
        self.assertEqual([call.args[0] for call in self.embed.embed.call_args_list],
            ['帮我打开浏览器', '你知道我密码吗，为什么你能安装file2md app', '查看当前系统版本'])

    def test_root_remote_or_different_backend_cannot_make_client_calls(self):
        for uid, backend, url in ((0, 'ai_gateway', 'http://127.0.0.1:18790'),
                (1000, 'local', 'http://127.0.0.1:18790'),
                (1000, 'ai_gateway', 'https://operator:private-key@other-host')):
            with self.subTest(uid=uid, backend=backend):
                self.config.inference_backend, self.config.gateway_url = backend, url
                code, output, errors = self.execute(uid)
                self.assertEqual((code, output), (1, ''))
                self.assertIn('configuration:RuntimeError', errors)
                self.assertNotIn('private-key', errors)
                self.backends.local_client.assert_not_called()
                self.backends.embedding_client.assert_not_called()

    def test_inference_failure_reports_only_stage_and_exception_type(self):
        for stage, target in (('llm_availability', self.local.is_available),
                              ('embedding_availability', self.embed.is_available),
                              ('completion', self.local._post),
                              ('tokenize', self.local._tokenize),
                              ('embeddings', self.embed.embed)):
            with self.subTest(stage=stage):
                target.side_effect = RuntimeError('api_key=private-key')
                code, output, errors = self.execute()
                target.side_effect = None
                self.assertEqual((code, output), (1, ''))
                self.assertIn(stage + ':RuntimeError', errors)
                self.assertNotIn('private-key', errors)


if __name__ == '__main__':
    unittest.main()
