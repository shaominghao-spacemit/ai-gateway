#!/usr/bin/python3
"""在平台独占租约内运行的 root 验收；备份永不自动删除。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import base64
import errno
import fcntl
import grp
from uuid import uuid4
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
import shlex
import signal
import stat
import subprocess
import tempfile
import threading
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
ENGINE_LOCK = Path('/run/lock/bibit-asr/engine.lock')
INTENT_APP = Path('/opt/bibit-intent')
INTENT_CONFIG = Path('/etc/bibit-intent/inference.env')
DOMAINS = {'asr', 'tts', 'vad', 'llm', 'embed', 'rerank', 'vlm', 'vision'}
PATHS = [APP, ETC, DATA, Path('/usr/lib') / PACKAGE,
         Path('/usr/lib/tmpfiles.d/spacemit-ai-gateway-asr.conf')]
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
    r'(?i)(["\']?(?:api[_ -]?key|token|password|secret|authorization|bearer)["\']?)'
    r'(\s*(?:(?:[=:]\s*)?(?:bearer\s+)?|[a-z]+\s+))[^\s,;]+')


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
                  if re.search(r'voice|intent|onnx|onnxruntime|(^|[-_])ort([-_:]|$)|llama',
                               line.split('\t', 1)[0], re.I))


def credential_key(path):
    lines = [line for line in path.read_text().splitlines()
             if line.startswith('SPACEMIT_AI_GATEWAY_AUTH__API_KEYS=')]
    require(len(lines) == 1, 'expected one packaged API_KEYS assignment')
    value = lines[0].split('=', 1)[1]
    require(len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0],
            'API_KEYS must be quoted JSON')
    try:
        values = shlex.split(value, comments=False)
        require(len(values) == 1, 'expected one API_KEYS JSON value')
        keys = json.loads(values[0])
    except ValueError:
        raise CheckFailed('invalid packaged API_KEYS JSON') from None
    require(isinstance(keys, list) and keys and all(isinstance(k, str) and k for k in keys),
            'packaged API_KEYS must contain a key')
    return keys[0]


def request(path, key='', payload=None, content_type='application/json', timeout=10, port=18790):
    headers = {'Content-Type': content_type}
    if key:
        headers['X-API-Key'] = key
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    req = urllib.request.Request(f'http://127.0.0.1:{port}' + path, body, headers)
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


INTENT_PROBE = r"""
import json, math, os, sys
from urllib.parse import urlsplit
sys.path.insert(0, '/opt/bibit-intent/src')
stage = 'configuration'
try:
    if os.geteuid() == 0:
        raise RuntimeError('non-root client required')
    from bibit_intent.models.backends import embedding_client, local_client
    from bibit_intent.pipeline import PipelineSettings
    config = PipelineSettings.from_environment()
    origin = urlsplit(config.gateway_url)
    if config.inference_backend != 'ai_gateway' or origin.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise RuntimeError('original local Gateway configuration required')
    local, embed = local_client(config), embedding_client(config)
    stage = 'llm_availability'
    if not local.is_available():
        raise RuntimeError('original LLM unavailable')
    stage = 'embedding_availability'
    if not embed.is_available():
        raise RuntimeError('original Embedding unavailable')
    stage = 'completion'
    response = local._post('/completion', {
        'prompt': '请输出 CALL mute on=true', 'grammar': 'root ::= "CALL mute on=true"',
        'n_probs': 10, 'n_predict': 30, 'temperature': 0})
    stage = 'tokenize'
    count = local._tokenize('你好，帮我打开浏览器')
    stage = 'embeddings'
    vectors = {label: embed.embed(value) for label, value in (
        ('browser', '帮我打开浏览器'),
        ('question', '你知道我密码吗，为什么你能安装file2md app'),
        ('version', '查看当前系统版本'))}
    print(json.dumps({'completion': response['content'], 'token_count': count,
        'probabilities_count': len(response['completion_probabilities']),
        'vectors': vectors}, allow_nan=False))
except Exception as error:
    print('original Intent probe failed: ' + stage + ':' + type(error).__name__, file=sys.stderr)
    sys.exit(1)
"""


def compare_intent_results(before, after):
    for result in (before, after):
        require(isinstance(result, dict) and isinstance(result.get('completion'), str)
                and result['completion'].strip() == 'CALL mute on=true',
                'original Intent completion mismatch')
        require(type(result.get('token_count')) is int and result['token_count'] > 0
                and type(result.get('probabilities_count')) is int and result['probabilities_count'] > 0,
                'original Intent token/probability contract failed')
        vectors = result.get('vectors')
        require(isinstance(vectors, dict) and set(vectors) == {'browser', 'question', 'version'},
                'original Intent embedding inputs mismatch')
        for vector in vectors.values():
            require(isinstance(vector, list) and 0 < len(vector) <= 16384
                    and all(type(value) in (int, float) and math.isfinite(value) for value in vector)
                    and sum(value * value for value in vector) > 0,
                    'original Intent embedding vector invalid')
    require(before['token_count'] == after['token_count']
            and before['probabilities_count'] == after['probabilities_count']
            and before['completion'] == after['completion'], 'original Intent inference changed')
    compared = {}
    for label, left in before['vectors'].items():
        right = after['vectors'][label]
        require(len(left) == len(right), 'original Intent embedding dimension changed')
        cosine = sum(a * b for a, b in zip(left, right)) / (
            math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right)))
        error = max(abs(a - b) for a, b in zip(left, right))
        require(cosine > 0.99999 and error < 0.0001, 'original Intent embedding changed')
        compared[label] = {'dimension': len(right), 'cosine': cosine, 'max_abs_error': error}
    return compared


class Lifecycle:
    def __init__(self, args):
        self.args = args
        self.checks = []
        self.backup = None
        self.mutated = False
        self.services_changed = False
        self.restored = False
        self.storage_acl_before = None
        self.storage_acl_pass = 0
        self.old_deb = None
        self.old_version = ''
        self.key = secrets.token_urlsafe(32)
        self.original_contract = None
        self.original_uids = {}
        self.protected_before = None
        self.protected_after = None
        self.intent_before = None
        self.intent_original = None
        self.legacy_intent_evidence = {'result': 'not_run', 'reason': 'original_upgrade_not_checked'}
        self.managed_cases = None
        self.managed_evidence = {'contract': 'managed-asr-v2', 'result': 'not_run', 'models': [],
                                 't18': {'result': 'not_passed', 'reason': 'both_models_not_verified'}}

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

    def legacy_databases(self):
        pid = int(run('systemctl', 'show', '-p', 'MainPID', '--value', UNITS[0]).stdout.strip())
        require(pid > 0, 'cannot inspect original gateway configuration')
        process = Path('/proc') / str(pid)
        environment = dict(item.split('=', 1) for item in
                           (process / 'environ').read_bytes().decode().split('\0') if '=' in item)
        uid = (process / 'status').read_text().split('Uid:', 1)[1].split()[0]
        environment.setdefault('HOME', pwd.getpwuid(int(uid)).pw_dir)
        script = """
import json
from pathlib import Path
from spacemit_ai_gateway.app.settings import get_settings
settings = get_settings()
print(json.dumps([str(Path(getattr(settings, domain).storage.db_path).expanduser().absolute())
                  for domain in ('llm', 'embed', 'rerank', 'vlm')]))
"""
        result = subprocess.run([str(APP / 'venv/bin/python'), '-c', script],
                                cwd=os.readlink(process / 'cwd'), env=environment,
                                capture_output=True, text=True, timeout=30)
        require(result.returncode == 0, 'cannot read original gateway database paths')
        try:
            paths = json.loads(result.stdout)
        except ValueError:
            raise CheckFailed('invalid original gateway database paths') from None
        require(isinstance(paths, list) and len(paths) == 4
                and all(isinstance(value, str) and Path(value).is_absolute()
                        for value in paths), 'invalid original gateway database paths')
        return paths

    def snapshot_external_databases(self, databases):
        records, parents, missing = {}, {}, set()
        for value in databases:
            path = Path(os.path.abspath(value))
            require(path.name not in ('', '.', '..'), 'invalid external database path')
            if any(path == covered or covered in path.parents for covered in PATHS):
                continue
            for parent in path.parents:
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    missing.add(str(parent))
                    continue
                require(stat.S_ISDIR(info.st_mode), 'external database parent must be a directory')
                parents[str(parent)] = [info.st_dev, info.st_ino]
            for suffix in ('', '-wal', '-shm', '-journal'):
                target = Path(str(path) + suffix)
                try:
                    info = target.lstat()
                except FileNotFoundError:
                    records[str(target)] = None
                    continue
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                        'external database must be a single regular file')
                records[str(target)] = {
                    'sha256': sha256(target), 'uid': info.st_uid, 'gid': info.st_gid,
                    'mode': stat.S_IMODE(info.st_mode),
                    'xattrs': {key: base64.b64encode(os.getxattr(target, key)).decode()
                              for key in os.listxattr(target)}}
        if not records:
            return
        present = [name.lstrip('/') for name, value in records.items() if value is not None]
        archive = self.backup / 'external-databases.tar'
        run('tar', '--acls', '--xattrs', '--numeric-owner', '-cpf', str(archive),
            '-C', '/', *(('--', *present) if present else ('--files-from=/dev/null',)), timeout=600)
        archive.chmod(0o600)
        manifest = self.backup / 'external-databases.json'
        with manifest.open('x') as stream:
            json.dump({'files': records, 'parents': parents,
                       'missing_parents': sorted(missing, key=lambda name: len(Path(name).parts),
                                                 reverse=True)}, stream)
        manifest.chmod(0o600)
        self.checked('external-model-databases-snapshotted')

    def restore_external_databases(self):
        manifest = self.backup / 'external-databases.json'
        if not manifest.exists():
            return
        state = json.loads(manifest.read_text())
        for name, identity in state['parents'].items():
            try:
                info = Path(name).lstat()
            except FileNotFoundError:
                raise CheckFailed('external database parent changed; backup retained') from None
            require(stat.S_ISDIR(info.st_mode) and [info.st_dev, info.st_ino] == identity,
                    'external database parent changed; backup retained')
        # 先验证全部目标，再删除精确文件；绝不递归清理模型目录。
        for name in state['files']:
            path = Path(name)
            for parent in path.parents:
                if parent.exists() or parent.is_symlink():
                    require(stat.S_ISDIR(parent.lstat().st_mode),
                            'external database parent changed; backup retained')
            if path.exists() or path.is_symlink():
                info = path.lstat()
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                        'external database must remain a single regular file')
        for name in state['files']:
            Path(name).unlink(missing_ok=True)
        run('tar', '--acls', '--xattrs', '--numeric-owner', '-xpf',
            str(self.backup / 'external-databases.tar'), '-C', '/', timeout=600)
        for name, expected in state['files'].items():
            path = Path(name)
            if expected is None:
                require(not path.exists() and not path.is_symlink(),
                        'external database absence was not restored')
                continue
            info = path.lstat()
            actual = {'sha256': sha256(path), 'uid': info.st_uid, 'gid': info.st_gid,
                      'mode': stat.S_IMODE(info.st_mode),
                      'xattrs': {key: base64.b64encode(os.getxattr(path, key)).decode()
                                for key in os.listxattr(path)}}
            require(stat.S_ISREG(info.st_mode) and actual == expected,
                    'external database content or permissions were not restored')
        for name in state['missing_parents']:
            path = Path(name)
            if path.exists():
                # 新增目录内有非本次数据时拒绝删除，不能以恢复为名清理他人文件。
                path.rmdir()
        self.checked('external-model-databases-restored')

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
        if self.old_version:
            require(self.services[UNITS[0]]['active'] == 'active',
                    'original gateway must be active for live upgrade verification')
            self.original_contract = self.service_contract()
            for unit in UNITS:
                pid = int(run('systemctl', 'show', '-p', 'MainPID', '--value', unit).stdout)
                if pid:
                    uid = re.search(r'^Uid:\s+(\d+)\s+(\d+)', Path(f'/proc/{pid}/status').read_text(), re.M)
                    require(uid is not None, 'cannot inspect original service identity')
                    self.original_uids[unit] = [int(uid[1]), int(uid[2])]
            self.checked('original-listener-auth-contract-snapshotted')
        databases = self.legacy_databases() if self.old_version else []
        # 预检已完成；停服本身也可能部分成功，必须先登记恢复责任。
        self.services_changed = True
        self.stop_services()
        self.snapshot_storage_acl()
        self.snapshot_external_databases(databases)
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
        legacy_root = False
        for unit in UNITS:
            pid = int(run('systemctl', 'show', '-p', 'MainPID', '--value', unit).stdout.strip())
            status = Path(f'/proc/{pid}/status').read_text()
            ids = re.search(r'^Uid:\s+(\d+)\s+(\d+)', status, re.M)
            require(ids is not None, 'cannot inspect service identity')
            current_ids = [int(ids[1]), int(ids[2])]
            if 0 in current_ids:
                require(self.old_version and self.original_uids.get(unit) == current_ids,
                        'gateway runs as root without an original root baseline')
                legacy_root = True
        current = self.service_contract()
        self.health()
        if self.original_contract is not None:
            require(current == self.original_contract,
                    'original listener/auth contract changed during upgrade')
            self.checked('original-listener-auth-contract-preserved')
        else:
            for port, addresses in current['listeners'].items():
                require(addresses == [f'127.0.0.1:{port}'],
                        f'port {port} must bind only 127.0.0.1')
            require(current['unauthenticated_status'] == 401,
                    'packaged authentication must reject missing key with 401')
            packaged_key = credential_key(ETC / 'credentials.env')
            status, _ = request('/v1/vad/models/load', packaged_key,
                                {'model_id': 'lifecycle-absent-model'})
            require(status == 404, 'packaged key must reach unknown-model handler with 404')
            self.checked('packaged-credentials-authentication')
        self.checked('legacy-root-identity-preserved' if legacy_root else 'nonroot-eight-domain-health')

    def service_contract(self):
        sockets = run('ss', '-ltnH').stdout.splitlines()
        listeners = {}
        for port in (18790, 8326):
            listeners[port] = sorted({line.split()[3] for line in sockets
                                      if line.split()[3].rsplit(':', 1)[-1] == str(port)})
            require(listeners[port], f'expected service listener missing: {port}')
        status, _ = request('/v1/vad/models/load', payload={'model_id': 'lifecycle-absent-model'})
        require(status in (401, 403, 404), 'cannot establish original listener/auth contract')
        return {'listeners': listeners, 'unauthenticated_status': status}

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

    def intent_context(self):
        state = run('dpkg-query', '-W', '-f=${Status}\n${Version}', 'bibit-intent', check=False)
        if state.returncode or not state.stdout.startswith('install ok installed\n'):
            return None
        files = [INTENT_CONFIG, INTENT_APP / 'main.py', INTENT_APP / '.venv/pyvenv.cfg',
                 *sorted((INTENT_APP / 'src').rglob('*.py'))]
        require(len(files) > 3 and all(path.is_file() for path in files),
                'original Intent installation/configuration missing')
        users = set()
        for session in run('loginctl', 'list-sessions', '--no-legend', '--no-pager').stdout.splitlines():
            values = dict(line.split('=', 1) for line in run(
                'loginctl', 'show-session', session.split()[0], '-p', 'Type', '-p', 'User').stdout.splitlines()
                if '=' in line)
            if values.get('Type') in ('x11', 'wayland') and values.get('User', '0') != '0':
                users.add(int(values['User']))
        require(len(users) == 1, 'original Intent needs one graphical session user')
        account = pwd.getpwuid(users.pop())
        return {'version': state.stdout.splitlines()[1], 'uid': account.pw_uid,
                'gid': account.pw_gid, 'home': account.pw_dir,
                'identity': {str(path): sha256(path) for path in files}}

    def run_intent_probe(self, context):
        unit = 'gateway-intent-' + uuid4().hex + '.service'
        try:
            result = run('systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--unit=' + unit,
                         '--uid=' + str(context['uid']), '--gid=' + str(context['gid']),
                         '--property', 'WorkingDirectory=' + str(INTENT_APP),
                         '--property', 'EnvironmentFile=' + str(INTENT_CONFIG),
                         '--property', 'RuntimeMaxSec=420s', '--property', 'UMask=0077',
                         '--setenv=HOME=' + context['home'], '--setenv=PYTHONDONTWRITEBYTECODE=1',
                         '--', str(INTENT_APP / '.venv/bin/python'), '-B', '-c', INTENT_PROBE,
                         check=False, timeout=450)
            log = self.backup / (unit + '.log')
            log.write_text(result.stdout + '\n' + result.stderr)
            log.chmod(0o600)
            require(result.returncode == 0, 'original Intent real inference failed; private log retained')
            try:
                data = json.loads(result.stdout)
            except ValueError:
                raise CheckFailed('original Intent returned invalid probe data') from None
            compare_intent_results(data, data)
            return data
        finally:
            run('systemctl', 'stop', unit, check=False)
            active = run('systemctl', 'is-active', unit, check=False).stdout.strip()
            require(active in ('inactive', 'unknown', 'failed'), 'original Intent probe did not stop')

    def legacy_intent_probe(self, stage):
        if stage == 'after' and self.intent_original is None:
            return
        try:
            context = self.intent_context()
            if stage == 'before':
                if context is None:
                    self.legacy_intent_evidence = {'result': 'not_run', 'reason': 'client_not_installed'}
                    return
                self.intent_original = context
            else:
                require(context == self.intent_original, 'original Intent source/configuration changed')
            data = self.run_intent_probe(context)
            saved = self.backup / ('legacy-intent-' + stage + '.json')
            saved.write_text(json.dumps(data, allow_nan=False))
            saved.chmod(0o600)
            if stage == 'before':
                self.intent_before = data
                self.legacy_intent_evidence = {'result': 'baseline_verified',
                                               'client_version': context['version']}
            else:
                comparison = compare_intent_results(self.intent_before, data)
                self.legacy_intent_evidence = {'result': 'ok', 'client_version': context['version'],
                    'source_and_configuration_unchanged': True, 'comparison': comparison}
                self.checked('legacy-intent-original-config-real-llm-embedding-upgrade')
        except Exception:
            self.legacy_intent_evidence = {'result': 'failed', 'stage': stage}
            raise

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
        if getattr(self.args, 'managed_cases', None):
            existing += '\nSPACEMIT_AI_GATEWAY_ASR__MANAGED__ENABLED=true\n'
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

    def read_managed_cases(self):
        path = getattr(self.args, 'managed_cases', None)
        expected = getattr(self.args, 'managed_cases_sha256', None)
        if not path:
            require(expected is None, 'managed cases path required with digest')
            return
        try:
            content = Path(path).read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            require(expected is None or expected == digest, 'managed cases digest mismatch')
            cases = json.loads(content)
            require(isinstance(cases, list) and cases, 'managed cases must be a nonempty list')
            self.managed_cases = cases
            self.managed_evidence['cases_sha256'] = digest
        except Exception:
            self.managed_evidence['result'] = 'failed'
            raise

    def managed_contract(self):
        """可选本轮 U4 真模型验收；仅使用只读资产，不停止或抢占 Voice。"""
        cases_path = getattr(self.args, 'managed_cases', None)
        if not cases_path:
            return
        self.managed_evidence['result'] = 'running'
        try:
            if self.managed_cases is None:
                self.read_managed_cases()
            for case in self.managed_cases:
                model, language = case['model'], case['language']
                require(model in {'sensevoice', 'qwen3_asr_0.6b'}, 'invalid managed model case')
                require(isinstance(language, str) and language, 'managed case language required')
                audio = Path(case['audio']).read_bytes()
                require(audio, 'managed case audio must be nonempty')
                # 必须提供真人语音及期望子串，避免静音/假 text 冒充识别验收。
                require(isinstance(case['expect_text'], str) and case['expect_text'],
                        'managed case expected transcript required')
                self.managed_case(case, audio)
            self.managed_evidence['result'] = 'ok'
            verified = {item['model'] for item in self.managed_evidence['models'] if item['result'] == 'ok'}
            if verified == {'sensevoice', 'qwen3_asr_0.6b'}:
                self.managed_evidence['t18'] = {'result': 'ok', 'reason': 'both_models_verified'}
            self.checked('u4-managed-asr-v2-real-model-contract')
        except Exception:
            self.managed_evidence['result'] = 'failed'
            raise

    def managed_case(self, case, audio):
        from urllib.parse import urlencode
        model, language = case['model'], case['language']
        endpoint = '/v1/asr/managed/'
        owner = str(uuid4())
        query = endpoint + 'status?' + urlencode({'model': model, 'language': language})
        require(request(query)[0] == 401, 'managed status must enforce authentication')
        before = api(query, self.key)
        if model == 'sensevoice' and before.get('reason') == 'sensevoice_language_list_unsupported':
            self.managed_evidence['t18'] = {'result': 'blocked', 'reason': before['reason']}
        require(before.get('reason') not in {'sensevoice_language_list_unsupported', 'unsupported_language'},
                'managed language semantics unsupported; T18 not passed')
        require(before.get('protocol_version') == 2 and before.get('model') == model
                and before.get('languages') == language.split(',') and before.get('assets_ready') is True,
                'managed exact target preflight failed')
        require(before.get('resource_state') == 'free' and before.get('owner') is None,
                'managed resource blocked; release local Voice before device acceptance')
        lock = ENGINE_LOCK
        directory_stat, lock_stat = lock.parent.stat(), lock.stat()
        shared_gid = grp.getgrnam('bibit-asr').gr_gid
        require(directory_stat.st_uid == 0 and directory_stat.st_gid == shared_gid
                and directory_stat.st_mode & 0o7777 == 0o2775
                and lock_stat.st_uid == 0 and lock_stat.st_gid == shared_gid and lock_stat.st_mode & 0o777 == 0o660,
                'managed shared lock ownership or modes mismatch')
        # 无 token 时只允许精确 owner/model/恢复秘密找回；绝不据 API 故障推断资源释放。
        token = None
        already_released = False
        acquire_body = {'owner': owner, 'model': model, 'language': language,
                        'recovery_secret': secrets.token_hex(32)}
        try:
            try:
                acquired = api(endpoint + 'acquire', self.key, acquire_body, timeout=180)
            except (OSError, TimeoutError):
                acquired = api(endpoint + 'acquire', self.key, acquire_body, timeout=180)
            token = acquired.get('token')
            require(isinstance(token, str) and token and acquired.get('ready') is True
                    and acquired.get('owner') == owner and acquired.get('instance_id') == before.get('instance_id')
                    and acquired.get('model') == model and acquired.get('languages') == language.split(',')
                    and acquired.get('resource_state') == 'owned',
                    'managed acquisition identity failed')
            code, _ = request(endpoint + 'acquire', self.key,
                              dict(acquire_body, recovery_secret=secrets.token_hex(32)))
            require(code == 403, 'public owner must not recover another client token')
            recovered = api(endpoint + 'acquire', self.key, acquire_body, timeout=180)
            require(recovered.get('token') == token, 'same recovery secret must recover original token')
            visible = api(query, self.key)
            require('token' not in visible and 'recovery_secret' not in visible
                    and 'recovery_digest' not in visible, 'managed status exposes a session secret')
            require(self.health()['domains']['asr'].get('ready') is True,
                    'managed healthy backend missing from global health')
            for invalid_owner, invalid_token in ((str(uuid4()), token), (owner, 'invalid-token')):
                code, _ = request(endpoint + 'release', self.key,
                                  {'owner': invalid_owner, 'token': invalid_token})
                require(code == 403, 'managed release must reject wrong owner/token')
            # 由 kernel flock 证明仍然占有，不以 HTTP 健康代替。
            with lock.open('r+b') as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise CheckFailed('managed backend does not hold engine flock')
            for selected in dict.fromkeys([language, *case.get('switch_languages', [])]):
                state = api(endpoint + 'status?' + urlencode({'model': model, 'language': selected}), self.key)
                require(state.get('ready') is True and state.get('languages') == selected.split(',')
                        and state.get('instance_id') == before['instance_id'],
                        'managed language capability/status mismatch')
                boundary = 'managed-' + secrets.token_hex(16)
                body = bytearray()
                for name, value in {'owner': owner, 'token': token, 'language': selected}.items():
                    body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                                 f'\r\n\r\n{value}\r\n').encode())
                body.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                             'filename="speech.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode())
                body.extend(audio)
                body.extend(f'\r\n--{boundary}--\r\n'.encode())
                result = api(endpoint + 'recognize', self.key, bytes(body),
                             content_type='multipart/form-data; boundary=' + boundary, timeout=180)
                require(isinstance(result.get('text'), str) and case['expect_text'] in result['text'],
                        'managed real speech transcript mismatch')
            if model == 'qwen3_asr_0.6b':
                self.stalled_managed_release(owner, token, bytes(body), boundary, audio)
                already_released = True
        finally:
            if token is None:
                self.managed_evidence['models'].append({
                    'model': model, 'result': 'failed', 'reason': 'session_token_unavailable',
                    'resource_release': 'unconfirmed'})
            if token and not already_released:
                released = api(endpoint + 'release', self.key, {'owner': owner, 'token': token}, timeout=180)
                require(released.get('released') is True, 'managed release acknowledgement failed')
                with lock.open('r+b') as stream:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                after = api(query, self.key)
                require(after.get('owner') is None and after.get('resource_state') == 'free'
                        and after.get('ready') is False, 'managed resource remains after release')
        self.managed_evidence['models'].append({'model': model, 'language': language,
                                               'audio_sha256': hashlib.sha256(audio).hexdigest(),
                                               'instance_id': before['instance_id'], 'result': 'ok'})

    def stalled_managed_release(self, owner, token, body, boundary, audio):
        parent = int(run('systemctl', 'show', '-p', 'MainPID', '--value', UNITS[0]).stdout)
        children = run('ps', '-o', 'pid=', '--ppid', str(parent)).stdout.split()
        engines = [int(pid) for pid in children
                   if b'qwen3-asr' in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')]
        require(len(engines) == 1, 'cannot identify owned Qwen3 child')
        pid = engines[0]
        args = Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        port = int(args[args.index('--port') + 1])
        require(not self.slot_processing(port), 'Qwen3 slot was busy before the test request')
        with wave.open(io.BytesIO(audio), 'rb') as source:
            frames = source.readframes(source.getnframes())
            repeats = max(1, math.ceil(30 * source.getframerate() / source.getnframes()))
            output = io.BytesIO()
            with wave.open(output, 'wb') as target:
                target.setparams(source.getparams())
                target.writeframes(frames * repeats)
        body = body.replace(audio, output.getvalue(), 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(request, '/v1/asr/managed/recognize', self.key, body,
                                 'multipart/form-data; boundary=' + boundary, 90)
            try:
                self.stop_in_processing(pid, port, future)
                with ENGINE_LOCK.open('r+b') as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        raise CheckFailed('stalled managed engine lost shared lock')
                stop_observer, observer_ready = threading.Event(), threading.Event()
                observations, observation_errors = [], []
                def observe_lock():
                    try:
                        with ENGINE_LOCK.open('r+b') as stream:
                            while not stop_observer.is_set():
                                if not Path(f'/proc/{pid}').exists():
                                    observations.append('exited')
                                    return
                                try:
                                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                except BlockingIOError:
                                    observations.append('alive_locked')
                                else:
                                    if Path(f'/proc/{pid}').exists():
                                        raise CheckFailed('shared lock released before managed engine exited')
                                    fcntl.flock(stream, fcntl.LOCK_UN)
                                observer_ready.set()
                                stop_observer.wait(.005)
                    except Exception as exc:
                        observation_errors.append(exc)
                    finally:
                        observer_ready.set()
                observer = threading.Thread(target=observe_lock)
                observer.start()
                try:
                    require(observer_ready.wait(2), 'release lock observer did not start')
                    time.sleep(3)
                    require(not future.done(), 'stalled inference unexpectedly returned')
                    health = self.health()
                    require(health['status'] == 'degraded' and not health['domains']['asr']['ready'],
                            'stopped managed engine was hidden by global health')
                    started = time.monotonic()
                    released = api('/v1/asr/managed/release', self.key,
                                   {'owner': owner, 'token': token}, timeout=60)
                finally:
                    stop_observer.set()
                    observer.join(timeout=3)
                require(not observer.is_alive() and not observation_errors
                        and 'alive_locked' in observations, 'release lock observation failed')
                require(released.get('released') is True and time.monotonic() - started < 60,
                        'stalled inference release exceeded bound')
                require(not Path(f'/proc/{pid}').exists(), 'managed engine still exists after release')
                with ENGINE_LOCK.open('r+b') as stream:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                require(self.health()['domains']['asr']['state'] == 'idle',
                        'released managed engine did not return to lazy idle')
                self.checked('mr7-stalled-engine-health-release-real-process')
                self.managed_evidence.setdefault('inference_phase', 'verified_slot_transition')
                self.managed_evidence['release_lock_samples'] = observations.count('alive_locked')
            finally:
                if Path(f'/proc/{pid}').exists():
                    os.kill(pid, signal.SIGCONT)
                try:
                    future.result(timeout=90)
                except (OSError, TimeoutError):
                    pass

    def slot_processing(self, port):
        slots = api('/slots', port=port, timeout=2)
        require(isinstance(slots, list) and len(slots) == 1 and slots[0].get('id') == 0
                and isinstance(slots[0].get('is_processing'), bool), 'invalid owned Qwen3 slot state')
        return slots[0]['is_processing']

    def stop_in_processing(self, pid, port, future):
        deadline = time.monotonic() + 10
        while not self.slot_processing(port):
            require(not future.done() and time.monotonic() < deadline,
                    'cannot correlate the test request with Qwen3 slot processing')
            time.sleep(.02)
        require(not future.done(), 'Qwen3 request finished before fault injection')
        os.kill(pid, signal.SIGSTOP)

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
        if self.old_version:
            run('systemctl', 'start', UNITS[0])
            wait_for(lambda: api('/healthz'))
            self.legacy_intent_probe('before')
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
        self.legacy_intent_probe('after')
        self.configure_admin()
        self.managed_contract()
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
        self.purge_with_acl_recovery()
        self.preserved(purge=True)
        self.checked('reinstall-purge-preserves-dropin-and-data')
        self.check_admin_unit_upgrade()

    def purge_with_acl_recovery(self):
        self.restore_storage_acl()
        archives = set((ETC / 'migration-backups').glob('purge-*'))
        try:
            run('dpkg', '--force-depends', '--purge', PACKAGE, timeout=180)
        finally:
            new_archives = set((ETC / 'migration-backups').glob('purge-*')) - archives
            journals = [path / 'legacy-storage-acl.jsonl' for path in new_archives
                        if (path / 'legacy-storage-acl.jsonl').is_file()]
            require(len(journals) <= 1, 'ambiguous purge ACL recovery archive')
            self.storage_acl_purge_archive = journals[0] if journals else None

    def check_admin_unit_upgrade(self):
        if not self.old_version:
            return
        # 回到精确旧包后构造管理员 unit，外层 finally 仍负责恢复最初板端基线。
        self.restore()
        self.restored = False
        self.stop_services()
        legacy = APP / 'configs/base.yaml'
        require(legacy.is_file() and not legacy.is_symlink(), 'legacy packaged config missing for admin upgrade')
        account = pwd.getpwnam('bianbu')
        home = DATA / "audit-admin's-home"
        home.mkdir(parents=True, exist_ok=True)
        os.chown(home, account.pw_uid, account.pw_gid)
        environment = DATA / 'audit-admin.env'
        environment.write_text(
            f'HOME={home}\nSPACEMIT_AI_GATEWAY_CONFIG={legacy}\n'
            'SPACEMIT_AI_GATEWAY_LLM__STORAGE__MODELS_DIR=~/models/llm\n'
            f'SPACEMIT_AI_GATEWAY_VAD__MODEL_DIR={MODEL.parent}\n'
            'SPACEMIT_AI_GATEWAY_APP__HOST=127.0.0.1\n'
            'SPACEMIT_AI_GATEWAY_AUTH__ENABLED=true\n'
            'SPACEMIT_AI_GATEWAY_AUTH__IP_WHITELIST=[]\n'
            'SPACEMIT_AI_GATEWAY_AUTH__IP_WHITELIST_FILE=\n'
            'SPACEMIT_AI_GATEWAY_AUTH__API_KEYS=' + json.dumps([self.key]) + '\n')
        environment.chmod(0o644)
        unit = Path('/etc/systemd/system/' + UNITS[0])
        unit.write_text('[Unit]\nDescription=Gateway administrator upgrade acceptance\n'
                        '[Service]\nUser=bianbu\nGroup=bianbu\nSupplementaryGroups=bibit-asr\n'
                        f'WorkingDirectory={APP}\nEnvironmentFile={environment}\n'
                        f'ExecStart={APP}/venv/bin/spacemit-ai-gateway\nRestart=on-failure\n'
                        '[Install]\nWantedBy=multi-user.target\n')
        originals = (unit.read_bytes(), environment.read_bytes())
        # 模拟旧候选已完成固定 UID 迁移，但管理员 HOME 缓存仍未获授权。
        ETC.mkdir(parents=True, exist_ok=True)
        (ETC / 'base.yaml').write_bytes(legacy.read_bytes())
        for marker in ('.legacy-storage-migrated', '.legacy-assets-migrated'):
            (ETC / marker).touch()
        run('systemctl', 'daemon-reload')
        self.install()
        require((unit.read_bytes(), environment.read_bytes()) == originals,
                'complete administrator unit or custom environment was rewritten')
        require(legacy.is_symlink() and legacy.resolve() == ETC / 'base.yaml',
                'old administrator config path was not restored')
        readable = run('runuser', '-u', 'bianbu', '--', '/usr/bin/python3', '-c',
                       'import pathlib,sys; pathlib.Path(sys.argv[1]).open().read(1)', str(legacy), check=False)
        if readable.returncode:
            details = readable.stderr + readable.stdout
            details += run('namei', '-l', str(legacy), check=False).stdout
            details += run('getfacl', '-p', str(legacy), str(ETC), check=False).stdout
            details += run('systemctl', 'show', UNITS[0], '-p', 'User,DropInPaths', check=False).stdout
            raise CheckFailed('administrator config is not readable', details)
        run('systemctl', 'restart', UNITS[0])
        wait_for(lambda: api('/healthz'))
        pid = int(run('systemctl', 'show', '-p', 'MainPID', '--value', UNITS[0]).stdout)
        values = dict(item.split('=', 1) for item in
                      Path(f'/proc/{pid}/environ').read_bytes().decode().split('\0') if '=' in item)
        require(values.get('HOME') == str(home), 'custom HOME lost during stopped service upgrade')
        probe = ('import json,os; from spacemit_ai_gateway.app.settings import get_settings; '
                 "print(json.dumps({'home':os.environ['HOME'],"
                 "'models':str(get_settings().llm.storage.models_path)}))")
        result = subprocess.run([str(APP / 'venv/bin/python'), '-c', probe], env=values,
                                cwd=APP, capture_output=True, text=True, timeout=30)
        require(result.returncode == 0, 'administrator effective settings could not be read')
        require(json.loads(result.stdout) == {'home': str(home), 'models': str(home / 'models/llm')},
                'custom model path changed during stopped service upgrade')
        self.checked('mr7-admin-unit-stopped-environment-upgrade')
        self.checked('mr7-admin-existing-markers-systemd-environment-upgrade')

    def read_storage_acl_journal(self):
        path = ETC / 'legacy-storage-acl.jsonl'
        archive = getattr(self, 'storage_acl_purge_archive', None)
        if not path.exists() and archive is not None:
            require(not archive.parent.is_symlink()
                    and archive.parent.stat().st_uid == os.getuid()
                    and stat.S_IMODE(archive.parent.stat().st_mode) == 0o700,
                    'purge ACL recovery archive must remain private')
            path = archive
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {'identity': None, 'content': ''}
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            # prepare 要求 root；拒绝其他属主、公开可读或特殊文件的记录。
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, 'storage ACL journal must remain private')
            content = stream.read()
        return {'identity': [info.st_dev, info.st_ino],
                'content': base64.b64encode(content).decode('ascii')}

    def private_acl_evidence(self, name, value):
        fd = os.open(self.backup / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())

    def snapshot_storage_acl(self):
        self.storage_acl_purge_archive = None
        self.storage_acl_before = self.read_storage_acl_journal()
        self.private_acl_evidence('storage-acl-before.json', self.storage_acl_before)

    def restore_storage_acl(self):
        if self.storage_acl_before is None:
            return  # prepare 在快照之前失败时不会进入文件恢复。
        try:
            current = self.read_storage_acl_journal()
            self.storage_acl_pass += 1
            self.private_acl_evidence(f'storage-acl-rollback-{self.storage_acl_pass}.json', current)
            require(self.storage_acl_before['identity'] is None or current['identity'] is not None,
                    'original storage ACL journal disappeared')
            before = base64.b64decode(self.storage_acl_before['content'])
            content = base64.b64decode(current['content'])
            # runtime 先写完整行再改 ACL；中断的尾行不代表已生效的修改。
            before = before[:before.rfind(b'\n') + 1]
            content = content[:content.rfind(b'\n') + 1]
            require(content.startswith(before), 'storage ACL journal history changed')
            identities = set()
            records = []
            for line in content.splitlines():
                record = json.loads(line)
                path = Path(record['path'])
                attribute, mode, acl = record['attribute'], record['mode'], record['acl']
                require(path.is_absolute() and '..' not in path.parts
                        and attribute in ('system.posix_acl_access', 'system.posix_acl_default')
                        and type(mode) is int and 0 <= mode <= 0o7777,
                        'invalid storage ACL record')
                identity = (str(path), attribute)
                require(identity not in identities, 'duplicate storage ACL record')
                identities.add(identity)
                original = base64.b64decode(acl, validate=True) if acl is not None else None
                if original is not None:
                    require(len(original) >= 4 and original[:4] == b'\x02\x00\x00\x00'
                            and (len(original) - 4) % 8 == 0, 'invalid original storage ACL')
                records.append((path, attribute, mode, original))
            old_count = len(before.splitlines())
            # 倒序撤销：default 记录的 mode 可能已经包含先前的 access ACL mask。
            for path, attribute, mode, original in reversed(records[old_count:]):
                if any(path == root or path.is_relative_to(root) for root in PATHS):
                    continue  # 这些路径由 tar --acls 的原快照恢复。
                require(path.resolve(strict=True) == path, 'storage ACL target changed')
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(fd)
                    require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                            'invalid storage ACL target')
                    if original is None:
                        try:
                            os.removexattr(fd, attribute)
                        except OSError as error:
                            if error.errno != errno.ENODATA:
                                raise
                    else:
                        os.setxattr(fd, attribute, original)
                    os.fchmod(fd, mode)
                    try:
                        actual = os.getxattr(fd, attribute)
                    except OSError as error:
                        if error.errno != errno.ENODATA:
                            raise
                        actual = None
                    require(actual == original and stat.S_IMODE(os.fstat(fd).st_mode) == mode,
                            'storage ACL verification failed')
                finally:
                    os.close(fd)
        except Exception:
            # 路径、ACL 原值及 journal 内容仅留在 0700 备份目录，不能进入公开摘要。
            raise CheckFailed('RESTORE FAILED: storage ACL rollback failed; backup retained') from None

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
        # 必须在 purge/覆盖 ETC 前保存并撤销本轮授权，失败时保留原 journal。
        self.restore_storage_acl()
        self.storage_acl_purge_archive = None
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
        attempt(self.restore_external_databases)
        require(not errors, 'RESTORE FAILED: ' + ','.join(errors) + '; backup retained')
        if self.old_deb:
            def reinstall():
                require(sha256(self.old_deb) == self.old_digest, 'rollback deb digest changed')
                run('dpkg', '--force-confdef', '--force-confold', '-i', str(self.old_deb), timeout=300)
            attempt(reinstall)
        # 旧 postinst/debhelper 可能重写文件和 ACL；停服后再次撤销，最后还原 ETC。
        stop_before_files()
        self.restore_storage_acl()
        attempt(files)
        attempt(self.restore_external_databases)
        attempt(lambda: run('systemctl', 'daemon-reload'))
        require(not errors, 'RESTORE FAILED: ' + ','.join(errors) + '; backup retained')
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
            # 在任何停服、装包之前固定输入，避免验收读取另一份被替换的用例。
            self.read_managed_cases()
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
                  'restored': self.restored, 'checks': self.checks,
                  'request_id': self.args.request_id, 'attempt_id': self.args.attempt_id,
                  'artifact_id': self.args.artifact_id, 'managed_asr': self.managed_evidence,
                  'legacy_intent': self.legacy_intent_evidence}
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
    parser.add_argument('--managed-cases', help='本轮真模型验收 JSON；缺省明确记录 not_run')
    parser.add_argument('--managed-cases-sha256', help='托管用例 JSON 的预期 SHA256；停服前校验')
    args = parser.parse_args()
    args.binary = Path(args.binary).resolve()
    def interrupted(signum, frame):
        raise CheckFailed('platform interrupted lifecycle verification')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    return Lifecycle(args).execute()


if __name__ == '__main__':
    raise SystemExit(main())
