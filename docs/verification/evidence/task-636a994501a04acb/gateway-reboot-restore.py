import argparse
import hashlib
import importlib.util
import json
import os
import stat
import tarfile
from pathlib import Path
attempt = 'att-53a39afa1fd94d028a6c41e80ff6bbbb'
request = 'req-af65ee337efe490f92f4cbeefd525bb2'
assert os.geteuid() == 0
assert Path('/etc/machine-id').read_text().strip() == '61671880ec8c41deae0cc13bb411f143'
lease = json.loads(Path('/home/bianbu/.local/state/bibit-board-executor/state.json').read_text())['lease']
assert lease['attempt_id'] == attempt and lease['request_id'] == request and lease['fence'] == 370
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip() == '309d066d-a1b4-4b44-b655-2566ff4ead3d'
backup = Path('/home/bianbu/bibit-build/evidence') / attempt / 'gateway-backup-t6741uxw'
assert backup.stat().st_uid == 0 and stat.S_IMODE(backup.stat().st_mode) == 0o700
metadata = json.loads((backup / 'state.json').read_text())
assert (metadata['attempt_id'] == attempt and metadata['request_id'] == request
        and metadata['version'] == '0.0.19')
helper = Path('/home/bianbu/bibit-build/artifacts/gateway-7ec492e9ea6c/test-device-lifecycle.py')
assert hashlib.sha256(helper.read_bytes()).hexdigest() == (
    'd199a8768eca7c36629c2abbe52a17f322078f13ad3fb946cd49353ae79b560b')
spec = importlib.util.spec_from_file_location('gateway_restore_helper', helper)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
x = m.Lifecycle(argparse.Namespace())
x.backup = backup
x.old_version = metadata['version']
x.services = metadata['services']
x.original_data = metadata['data_owner_mode']
x.old_deb = next(backup.glob('*.deb'))
x.old_digest = '52e32592d321e86e65c373ca7b296bcf753f40031d9de0f225730dbe0fe93c48'
x.storage_acl_before = json.loads((backup / 'storage-acl-before.json').read_text())
x.storage_acl_purge_archive = None
x.storage_acl_pass = max(
    [int(p.stem.rsplit('-', 1)[-1]) for p in backup.glob('storage-acl-rollback-*.json')] + [0])
print('verified-original-backup; starting-existing-helper-restore', flush=True)
x.restore()
assert x.restored
# 完整原文件内容核验；恢复助手另行核验外部数据库及权限。
with tarfile.open(backup / 'files.tar') as archive:
    count = 0
    for member in archive.getmembers():
        p = Path('/') / member.name
        if member.isfile():
            actual = p.read_bytes()
            expected = archive.extractfile(member).read()
            assert actual == expected, 'restored original file mismatch'
            count += 1
        elif member.issym():
            assert p.is_symlink() and os.readlink(p) == member.linkname
m.run('dpkg', '--audit')
result = {
    'result': 'failed',
    'restored': True,
    'restoration_only': True,
    'request_id': request,
    'attempt_id': attempt,
    'original_version': x.old_version,
    'original_files_checked': count,
    'checks': x.checks,
    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    'hosts_sha256': hashlib.sha256(Path('/etc/hosts').read_bytes()).hexdigest(),
    'error': 'original acceptance interrupted by board hang and operator reboot; '
             'restoration only, not an acceptance pass',
}
output = backup / 'operator-reboot-restoration.json'
with output.open('w') as stream:
    json.dump(result, stream, indent=2)
    stream.flush()
    os.fsync(stream.fileno())
output.chmod(0o600)
os.sync()
print(json.dumps(result), flush=True)
