#!/usr/bin/python3
"""在平台独占租约内运行的 root 验收；备份永不自动删除。"""

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import wave

PACKAGE = 'spacemit-ai-gateway'
UNITS = (PACKAGE + '.service', PACKAGE + '-frontend.service')
APP = Path('/opt') / PACKAGE
ETC = Path('/etc') / PACKAGE
DATA = Path('/var/lib') / PACKAGE
MODEL = Path('/opt/bibit/models/vad/silero_vad.onnx')
DOMAINS = {'asr', 'tts', 'vad', 'llm', 'embed', 'rerank', 'vlm', 'vision'}
PATHS = [APP, ETC, DATA, Path('/usr/lib') / PACKAGE]
for unit in UNITS:
    for directory in ('/lib/systemd/system', '/usr/lib/systemd/system', '/etc/systemd/system'):
        PATHS.extend((Path(directory) / unit, Path(directory) / (unit + '.d')))


class CheckFailed(RuntimeError):
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or message


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


_SENSITIVE_DIAGNOSTIC = re.compile(
    r'(?i)(api[_ -]?key|token|password|secret|authorization|bearer)'
    r'(\s*[=:]\s*)[^\s,;]+')


def safe_diagnostic(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ''
    # 只把最后几行带回平台摘要；完整 stderr 另存于 0700 备份目录。
    tail = '\n'.join(lines[-8:])[:1600]
    return _SENSITIVE_DIAGNOSTIC.sub(r'\1\2[redacted]', tail)


def run(*command, cwd=None, check=True, timeout=180):
    env = dict(os.environ, PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
               DEBIAN_FRONTEND='noninteractive', LC_ALL='C')
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=timeout)
    if check and result.returncode:
        safe = safe_diagnostic(result.stderr)
        message = f'{command[0]} failed (exit {result.returncode})'
        if safe:
            message += f': {safe}'
        raise CheckFailed(message, detail=(
            f'$ {" ".join(command)}\nexit={result.returncode}\n'
            f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        ))
    return result


def safe_plan(text):
    for line in text.splitlines():
        match = re.match(r'^(Inst|Remv|Purg)\s+(\S+)', line)
        if match:
            require(match[2].split(':')[0] == PACKAGE,
                    'dependency simulation would change another package')


def protected_packages():
    result = run('dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Status}\n')
    return sorted(line for line in result.stdout.splitlines()
                  if re.search(r'voice|onnx|onnxruntime|(^|[-_])ort([-_:]|$)|llama',
                               line.split('\t', 1)[0], re.I))


def credential_key(path):
    lines = [line for line in path.read_text().splitlines()
             if line.startswith('SPACEMIT_AI_GATEWAY_AUTH__API_KEYS=')]
    require(len(lines) == 1, 'expected one packaged API_KEYS assignment')
    value = lines[0].split('=', 1)[1]
    require(value.startswith("'") and value.endswith("'"), 'API_KEYS must be single quoted JSON')
    try:
        keys = json.loads(value[1:-1])
    except ValueError:
        raise CheckFailed('invalid packaged API_KEYS JSON') from None
    require(isinstance(keys, list) and keys and all(isinstance(k, str) and k for k in keys),
            'packaged API_KEYS must contain a key')
    return keys[0]


def request(path, key='', payload=None, content_type='application/json', timeout=10):
    headers = {'Content-Type': content_type}
    if key:
        headers['X-API-Key'] = key
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    req = urllib.request.Request('http://127.0.0.1:18790' + path, body, headers)
    # 永远绕过主机代理，凭据只发往固定的 loopback URL。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def api(path, key='', payload=None, **kwargs):
    status, body = request(path, key, payload, **kwargs)
    require(status == 200, f'HTTP check failed: {path} ({status})')
    return json.loads(body)


def wait_for(callback, seconds=90):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = callback()
            if value:
                return value
        except (CheckFailed, OSError, ValueError):
            pass
        require(time.monotonic() < deadline, 'service readiness timed out')
        time.sleep(1)


def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


class Lifecycle:
    def __init__(self, args):
        self.args = args
        self.checks = []
        self.backup = None
        self.mutated = False
        self.services_changed = False
        self.restored = False
        self.old_deb = None
        self.old_version = ''
        self.key = secrets.token_urlsafe(32)
        self.protected_before = None
        self.protected_after = None

    def checked(self, name):
        self.checks.append(name)

    def stop_services(self):
        for unit in UNITS:
            stopped = run('systemctl', 'stop', unit, check=False)
            active = run('systemctl', 'is-active', unit, check=False).stdout.strip()
            # 全新安装时 unit 可以不存在；其它停服错误一律阻断文件操作。
            absent = (active in ('inactive', 'unknown') and stopped.returncode != 0
                      and run('systemctl', 'is-enabled', unit, check=False).stdout.strip()
                      == 'not-found')
            require((stopped.returncode == 0 or absent) and active in ('inactive', 'unknown'),
                    'service did not stop; files left untouched')

    def restore_active_services(self):
        # 快照尚未完成时只恢复启停状态，不能使用不完整归档回滚文件。
        errors = []
        for unit, state in self.services.items():
            try:
                if state['active'] == 'active':
                    run('systemctl', 'start', unit)
                active = run('systemctl', 'is-active', unit, check=False).stdout.strip()
                require(active == 'active' if state['active'] == 'active'
                        else active in ('inactive', 'unknown'), 'original service state did not recover')
            except Exception as error:
                errors.append(type(error).__name__)
        require(not errors, 'RESTORE FAILED: ' + ','.join(errors) + '; backup retained')
        self.restored = True
        self.checked('original-services-restored-without-file-changes')

    def prepare(self):
        # 必须先校验哈希；错误输入不能创建目录、更不能改包或服务。
        require(re.fullmatch(r'[0-9a-fA-F]{64}', self.args.expect_sha256), 'invalid SHA256')
        require(sha256(self.args.binary) == self.args.expect_sha256.lower(), 'SHA256 mismatch')
        require(os.geteuid() == 0, 'root is required')
        require(run('dpkg-deb', '-f', str(self.args.binary), 'Package').stdout.strip() == PACKAGE,
                'unexpected package identity')
        require(MODEL.is_file() and MODEL.stat().st_size > 0, 'real Silero model is missing')
        state = run('dpkg-query', '-W', '-f=${Status}\n${Version}', PACKAGE, check=False)
        if state.returncode == 0 and state.stdout.startswith('install ok installed\n'):
            self.old_version = state.stdout.splitlines()[1]
        else:
            require(state.returncode != 0 or 'not-installed' in state.stdout
                    or 'config-files' in state.stdout, 'existing package is not fully configured')
            # 保留 rc 包的状态需要专门恢复 dpkg conffile 账本；当前不冒险安装。
            require('config-files' not in state.stdout, 'purged or fully installed baseline required')
        evidence = Path(self.args.evidence_dir)
        evidence.mkdir(parents=True, exist_ok=True, mode=0o755)
        require(not evidence.is_symlink(), 'evidence directory must not be a symlink')
        evidence.chmod(0o755)
        self.protected_before = protected_packages()
        self.backup = Path(tempfile.mkdtemp(prefix='gateway-backup-', dir=evidence))
        os.chmod(self.backup, 0o700)
        if self.old_version:
            run('apt-get', 'download', f'{PACKAGE}={self.old_version}', cwd=self.backup, timeout=300)
            candidates = list(self.backup.glob('*.deb'))
            require(len(candidates) == 1, 'exact rollback package was not downloaded')
            self.old_deb = candidates[0]
            for field, wanted in (('Package', PACKAGE), ('Version', self.old_version)):
                require(run('dpkg-deb', '-f', str(self.old_deb), field).stdout.strip() == wanted,
                        'rollback package metadata mismatch')
            self.old_digest = sha256(self.old_deb)
            control = self.backup / 'old-control'
            run('dpkg-deb', '-e', str(self.old_deb), str(control))
            postinst = control / 'postinst'
            # 旧版无条件 pip install；离线 dry-run 先证明现有 venv 能满足它。
            if postinst.exists() and 'pip install' in postinst.read_text():
                python = APP / 'venv/bin/python'
                requirements = APP / 'requirements-runtime.txt'
                require(python.is_file() and requirements.is_file(), 'old offline runtime missing')
                run(str(python), '-m', 'pip', 'install', '--no-index', '--dry-run', '-U', 'pip',
                    '-r', str(requirements), timeout=180)
        plan = run('apt-get', '-s', '--no-install-recommends', 'install', str(self.args.binary))
        safe_plan(plan.stdout)
        (self.backup / 'apt-plan.txt').write_text(plan.stdout)
        self.services = {
            unit: {'active': run('systemctl', 'is-active', unit, check=False).stdout.strip(),
                   'enabled': run('systemctl', 'is-enabled', unit, check=False).stdout.strip()}
            for unit in UNITS
        }
        require(all(s['active'] in ('active', 'inactive', 'unknown')
                    and s['enabled'] in ('enabled', 'disabled', 'static', 'masked', 'not-found', '')
                    for s in self.services.values()), 'unsupported original systemd state')
        # 预检已完成；停服本身也可能部分成功，必须先登记恢复责任。
        self.services_changed = True
        self.stop_services()
        self.present = [str(p).lstrip('/') for p in PATHS if p.exists() or p.is_symlink()]
        self.original_data = None
        if DATA.exists():
            stat = DATA.stat()
            self.original_data = [stat.st_uid, stat.st_gid, stat.st_mode & 0o7777]
        metadata = {'version': self.old_version, 'services': self.services,
                    'present': self.present, 'data_owner_mode': self.original_data,
                    'request_id': self.args.request_id, 'attempt_id': self.args.attempt_id,
                    'artifact_id': self.args.artifact_id}
        (self.backup / 'state.json').write_text(json.dumps(metadata, indent=2))
        query = run('dpkg-query', '-s', PACKAGE, check=False)
        (self.backup / 'dpkg-status.txt').write_text(query.stdout)
        # tar 保留属主、权限、软链、ACL 和 xattr；同样保存完整数据而非只保存哨兵。
        run('tar', '--acls', '--xattrs', '--numeric-owner', '-cpf', str(self.backup / 'files.tar'),
            '-C', '/', *(self.present or ['--files-from=/dev/null']), timeout=600)
        info = sorted(Path('/var/lib/dpkg/info').glob(PACKAGE + '.*'))
        if info:
            run('tar', '-cpf', str(self.backup / 'dpkg-info.tar'), '-C', '/',
                *(str(p).lstrip('/') for p in info))
        self.checked('preflight-backup-rollback-deb')

    def install(self):
        run('dpkg', '--force-confdef', '--force-confold', '-i', str(self.args.binary), timeout=300)

    def restart(self):
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', *UNITS)
        wait_for(lambda: api('/healthz'))
        wait_for(lambda: run('systemctl', 'is-active', UNITS[1], check=False).returncode == 0)

    def health(self):
        data = api('/healthz')
        require(set(data.get('domains', {})) == DOMAINS, 'health must contain eight domains')
        return data

    def check_services(self):
        for unit in UNITS:
            pid = int(run('systemctl', 'show', '-p', 'MainPID', '--value', unit).stdout.strip())
            status = Path(f'/proc/{pid}/status').read_text()
            ids = re.search(r'^Uid:\s+(\d+)\s+(\d+)', status, re.M)
            require(ids and int(ids[1]) != 0 and int(ids[2]) != 0, 'gateway runs as root')
        sockets = run('ss', '-ltnH').stdout.splitlines()
        for port in (18790, 8326):
            addresses = [line.split()[3] for line in sockets
                         if line.split()[3].rsplit(':', 1)[-1] == str(port)]
            require(addresses and set(addresses) == {f'127.0.0.1:{port}'},
                    f'port {port} must bind only 127.0.0.1')
        self.health()
        status, _ = request('/v1/vad/models/load', payload={'model_id': 'lifecycle-absent-model'})
        require(status == 401, 'packaged authentication must reject missing key with 401')
        packaged_key = credential_key(ETC / 'credentials.env')
        status, _ = request('/v1/vad/models/load', packaged_key,
                            {'model_id': 'lifecycle-absent-model'})
        require(status == 404, 'packaged key must reach unknown-model handler with 404')
        self.checked('packaged-credentials-authentication')
        self.checked('nonroot-loopback-eight-domain-health')

    def check_storage_permissions(self):
        probe = """
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
app, data = map(Path, sys.argv[1:])
if os.geteuid() == 0 or not app.is_dir() or not data.is_dir():
    raise RuntimeError('invalid service identity or storage paths')
for directory, dirs, files in os.walk(app):
    for path in [Path(directory), *(Path(directory) / name for name in dirs + files)]:
        if os.access(path, os.W_OK):
            raise RuntimeError('service can write program files or directories')
with tempfile.TemporaryDirectory(prefix='lifecycle-write-', dir=data) as temporary:
    directory = Path(temporary)
    sentinel = directory / 'sentinel'
    sentinel.write_text('writable')
    if sentinel.read_text() != 'writable':
        raise RuntimeError('data write/read failed')
    database = directory / 'probe.sqlite'
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE probe (value TEXT)')
        connection.execute('INSERT INTO probe VALUES (?)', ('committed',))
    with sqlite3.connect(database) as connection:
        if connection.execute('SELECT value FROM probe').fetchone() != ('committed',):
            raise RuntimeError('SQLite commit/read failed')
"""
        run('runuser', '-u', PACKAGE, '--', '/usr/bin/python3', '-c', probe, str(APP), str(DATA))
        self.checked('r2-program-readonly-data-sqlite-writable')

    def configure_admin(self):
        config = ETC / 'base.yaml'
        with config.open('a') as stream:
            stream.write('\n# gateway lifecycle administrator sentinel\n')
        environment = ETC / 'environment'
        existing = environment.read_text() if environment.exists() else ''
        existing += ('\nSPACEMIT_AI_GATEWAY_AUTH__ENABLED=true\n'
                     'SPACEMIT_AI_GATEWAY_AUTH__IP_WHITELIST=[]\n'
                     'SPACEMIT_AI_GATEWAY_AUTH__IP_WHITELIST_FILE=\n'
                     "SPACEMIT_AI_GATEWAY_AUTH__API_KEYS='" + json.dumps([self.key]) + "'\n"
                     'SPACEMIT_AI_GATEWAY_VAD__MODEL_DIR=' + str(DATA / 'lifecycle-model') + '\n')
        environment.write_text(existing)
        environment.chmod(0o600)
        model_dir = DATA / 'lifecycle-model'
        model_dir.mkdir(mode=0o755)
        shutil.copy2(MODEL, model_dir / 'silero_vad.onnx')
        (model_dir / 'silero_vad.onnx').chmod(0o644)
        self.admin_paths = [config, environment]
        for unit in UNITS:
            dropin = Path('/etc/systemd/system') / (unit + '.d') / '99-lifecycle-admin.conf'
            require(not dropin.exists(), 'administrator sentinel dropin already exists')
            dropin.parent.mkdir(parents=True, exist_ok=True)
            dropin.write_text('[Service]\nEnvironment=GATEWAY_LIFECYCLE_ADMIN=preserve\n')
            self.admin_paths.append(dropin)
        sentinel = DATA / 'lifecycle-admin-data'
        require(not sentinel.exists(), 'data sentinel already exists')
        sentinel.write_text('gateway lifecycle preserved data\n')
        account = pwd.getpwnam(PACKAGE)
        os.chown(sentinel, account.pw_uid, account.pw_gid)
        self.admin_paths.append(sentinel)
        self.admin_hashes = {str(p): sha256(p) for p in self.admin_paths}
        self.sentinel_owner = (sentinel.stat().st_uid, sentinel.stat().st_gid)
        self.restart()
        status, _ = request('/v1/vad/models/load', payload={'model_id': 'silero'})
        require(status == 401, 'unauthenticated model load must return 401')
        self.vad()
        self.checked('administrator-auth-config-environment-dropin-data')

    def preserved(self, purge=False):
        for path, digest in self.admin_hashes.items():
            if purge and Path(path) in (ETC / 'base.yaml', ETC / 'environment'):
                continue
            require(Path(path).is_file() and sha256(path) == digest, 'administrator state was lost')
        sentinel = DATA / 'lifecycle-admin-data'
        require((sentinel.stat().st_uid, sentinel.stat().st_gid) == self.sentinel_owner,
                'administrator data ownership changed')

    def vad(self):
        loaded = api('/v1/vad/models/load', self.key, {'model_id': 'silero'}, timeout=90)
        require(loaded.get('loaded') is True, 'real VAD load failed')
        backend = self.health()['domains']['vad']
        require(backend.get('ready') is True and backend.get('state') == 'ready'
                and backend.get('backend') == 'silero', 'VAD is not a real ready Silero backend')
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 16000)
        boundary = 'gateway-lifecycle-' + secrets.token_hex(12)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="silence.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
        body += audio.getvalue() + f'\r\n--{boundary}--\r\n'.encode()
        data = api('/v1/vad/analyze?sample_rate=16000', self.key, body,
                   content_type='multipart/form-data; boundary=' + boundary, timeout=60)
        probability = data.get('probability')
        require(isinstance(data.get('is_speech'), bool) and isinstance(probability, (float, int))
                and math.isfinite(probability) and 0 <= probability <= 1
                and data.get('processing_ms', -1) >= 0, 'invalid real VAD inference result')
        self.checked('real-vad-load-inference')

    def check_failed_model_health(self):
        model = DATA / 'lifecycle-model/silero_vad.onnx'
        saved = model.with_name('silero_vad.onnx.lifecycle-good')
        require(not saved.exists(), 'VAD recovery model already exists')
        api('/v1/vad/models/unload', self.key, {'model_id': 'silero'})
        model.rename(saved)
        try:
            # 非空坏文件通过本地资产存在性检查，确保真正进入 SDK 加载且不触发下载。
            model.write_bytes(b'gateway lifecycle invalid ONNX model\n')
            model.chmod(0o644)
            status, body = request('/v1/vad/models/load', self.key,
                                   {'model_id': 'silero'}, timeout=90)
            require(status == 503 and json.loads(body).get('error') == 'load_failed',
                    'corrupt Silero must fail actual model loading')
            health = self.health()
            require(health.get('status') == 'degraded'
                    and health['domains']['vad'].get('ready') is False,
                    'R6 failed VAD loading must degrade health, not report ok')
            self.checked('r6-failed-model-degrades-health')
        finally:
            model.unlink(missing_ok=True)
            saved.rename(model)
            self.restart()
            self.vad()
        require(self.health().get('status') == 'ok', 'health did not recover after model restore')
        self.checked('r6-real-model-reload-health-recovered')

    def exercise(self):
        self.mutated = True
        self.stop_services()
        run('systemctl', 'unmask', *UNITS)
        self.install()
        # 已配置包的 dpkg --configure 会拒绝；直接重入实际安装的 configure 脚本。
        version = run('dpkg-query', '-W', '-f=${Version}', PACKAGE).stdout.strip()
        run('/var/lib/dpkg/info/' + PACKAGE + '.postinst', 'configure', version, timeout=300)
        self.checked('postinst-configure-reentry')
        self.restart()
        self.check_services()
        self.check_storage_permissions()
        self.configure_admin()
        self.check_failed_model_health()
        # 暂停主进程使 health 明确失败，然后 kill，验证 systemd 自动恢复。
        pid = int(run('systemctl', 'show', '-p', 'MainPID', '--value', UNITS[0]).stdout)
        try:
            run('systemctl', 'kill', '--kill-whom=main', '--signal=STOP', UNITS[0])
            try:
                request('/healthz', timeout=2)
            except (OSError, TimeoutError):
                pass
            else:
                raise CheckFailed('health did not report a stopped process failure')
        finally:
            run('systemctl', 'kill', '--kill-whom=main', '--signal=KILL', UNITS[0])
        wait_for(lambda: int(run('systemctl', 'show', '-p', 'MainPID', '--value', UNITS[0]).stdout)
                 not in (0, pid))
        wait_for(lambda: self.health())
        self.vad()
        self.checked('health-failure-kill-auto-recovery')
        self.install()
        self.preserved()
        self.checked('upgrade-preserves-administrator-state')
        # 独占验收中暂时移除 Gateway，保留所有反向依赖包，最后恢复依赖关系。
        run('dpkg', '--force-depends', '--remove', PACKAGE, timeout=180)
        self.preserved()
        self.checked('remove-preserves-administrator-state')
        self.install()
        self.preserved()
        self.restart()
        self.vad()
        run('dpkg', '--force-depends', '--purge', PACKAGE, timeout=180)
        self.preserved(purge=True)
        self.checked('reinstall-purge-preserves-dropin-and-data')

    def restore(self):
        errors = []

        def attempt(callback):
            try:
                callback()
            except Exception as error:
                errors.append(str(error) if isinstance(error, CheckFailed) else type(error).__name__)

        def stop_before_files():
            try:
                self.stop_services()
            except Exception:
                raise CheckFailed('RESTORE FAILED: services not stopped; backup retained') from None

        stop_before_files()
        attempt(lambda: run('systemctl', 'disable', *UNITS, check=False))
        # 先清理候选包，之后还原旧 venv，避免新 postrm 删除回滚运行环境。
        def purge():
            current = run('dpkg-query', '-W', '-f=${Status}', PACKAGE, check=False)
            if current.returncode == 0 and 'not-installed' not in current.stdout:
                run('dpkg', '--force-depends', '--purge', PACKAGE, timeout=180)
        attempt(purge)

        def files():
            for path in PATHS:
                remove_path(path)
            run('tar', '--acls', '--xattrs', '--numeric-owner', '-xpf',
                str(self.backup / 'files.tar'), '-C', '/', timeout=600)

        attempt(files)
        if self.old_deb:
            def reinstall():
                require(sha256(self.old_deb) == self.old_digest, 'rollback deb digest changed')
                run('dpkg', '--force-confdef', '--force-confold', '-i', str(self.old_deb), timeout=300)
            attempt(reinstall)
        # 旧 postinst/debhelper 可能重写文件和状态，安装后再次还原快照。
        stop_before_files()
        attempt(files)
        attempt(lambda: run('systemctl', 'daemon-reload'))
        for unit, state in self.services.items():
            if state['enabled'] == 'enabled':
                attempt(lambda u=unit: run('systemctl', 'enable', u))
            elif state['enabled'] == 'disabled':
                attempt(lambda u=unit: run('systemctl', 'disable', u))
            elif state['enabled'] == 'masked':
                attempt(lambda u=unit: run('systemctl', 'mask', u))
            if state['active'] == 'active':
                attempt(lambda u=unit: run('systemctl', 'start', u))
                attempt(lambda u=unit: require(
                    run('systemctl', 'is-active', u, check=False).stdout.strip() == 'active',
                    'original active service did not recover'))
            if state['active'] != 'active':
                attempt(lambda u=unit: require(
                    run('systemctl', 'is-active', u, check=False).stdout.strip() != 'active',
                    'original inactive service was started'))
            attempt(lambda u=unit, s=state: require(
                run('systemctl', 'is-enabled', u, check=False).stdout.strip() == s['enabled'],
                'original service enablement changed'))
        if self.old_version:
            attempt(lambda: require(run('dpkg-query', '-W', '-f=${Status}\n${Version}', PACKAGE)
                                    .stdout == 'install ok installed\n' + self.old_version,
                                    'rollback package state mismatch'))
        attempt(lambda: require(DATA.exists() == (self.original_data is not None),
                                'original data directory existence changed'))
        if self.original_data is not None:
            def data_owner():
                stat = DATA.stat()
                require([stat.st_uid, stat.st_gid, stat.st_mode & 0o7777] == self.original_data,
                        'original data ownership changed')
            attempt(data_owner)
        require(not errors, 'RESTORE FAILED: ' + ','.join(errors) + '; backup retained')
        self.restored = True
        self.checked('original-package-files-data-services-restored')

    def execute(self):
        error = ''
        private_diagnostics = []
        try:
            self.prepare()
            self.exercise()
        except Exception as exc:
            error = str(exc) if isinstance(exc, CheckFailed) else type(exc).__name__
            if isinstance(exc, CheckFailed):
                private_diagnostics.append(exc.detail)
        finally:
            if self.mutated or self.services_changed:
                try:
                    if self.mutated:
                        self.restore()
                    else:
                        self.restore_active_services()
                except Exception as exc:
                    error += '; RESTORE FAILED: ' + (str(exc) if isinstance(exc, CheckFailed)
                                                     else type(exc).__name__)
                    if isinstance(exc, CheckFailed):
                        private_diagnostics.append(exc.detail)
            else:
                self.restored = True
        if self.protected_before is not None:
            try:
                self.protected_after = protected_packages()
                require(self.protected_after == self.protected_before,
                        'protected voice/ORT/llama package inventory changed')
                self.checked('protected-package-inventory-unchanged')
            except Exception as exc:
                error += '; ' + (str(exc) if isinstance(exc, CheckFailed) else type(exc).__name__)
        result = {'result': 'failed' if error else 'ok', 'sha256': self.args.expect_sha256,
                  'restored': self.restored, 'checks': self.checks}
        if error:
            # 只输出我们定义的消息或异常类型，绝不输出响应、密钥或子进程日志。
            result['error'] = error
        if self.backup:
            try:
                if private_diagnostics:
                    detail = self.backup / 'command-errors.log'
                    detail.write_text('\n\n'.join(private_diagnostics))
                    detail.chmod(0o600)
                # 公开证据只含摘要和只读包清单；凭据及原始日志留在 0700 备份内。
                log = Path(self.args.evidence_dir) / (self.backup.name + '-summary.json')
                summary = dict(result, protected_packages_before=self.protected_before,
                               protected_packages_after=self.protected_after)
                with log.open('x') as stream:
                    json.dump(summary, stream, indent=2)
                log.chmod(0o644)
                result['log'] = str(log)
            except Exception:
                error += '; evidence summary write failed'
                result.update(result='failed', error=error)
            # 备份路径只用于人工回滚，不作为平台 log/screenshot 取证目标。
            result['backup'] = str(self.backup)
        print(json.dumps(result), flush=True)
        return 1 if error else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('binary', 'expect-sha256', 'evidence-dir', 'request-id', 'attempt-id', 'artifact-id'):
        parser.add_argument('--' + option, required=True)
    args = parser.parse_args()
    args.binary = Path(args.binary).resolve()
    def interrupted(signum, frame):
        raise CheckFailed('platform interrupted lifecycle verification')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    return Lifecycle(args).execute()


if __name__ == '__main__':
    raise SystemExit(main())
