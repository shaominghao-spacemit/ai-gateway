#!/usr/bin/env python3
"""临时板端 DNS 验证工具；仅由同租约的平台受控进程触发，退出前恢复。"""
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile


def replace_bytes(path, data):
    with path.open('r+b') as stream:
        stream.write(data)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    assert path.read_bytes() == data


def self_check():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'hosts'
        path.write_bytes(b'127.0.0.1 localhost\n')
        path.chmod(0o640)
        original = path.read_bytes()
        before = path.stat()
        replace_bytes(path, original + b'151.101.0.223 files.pythonhosted.org\n')
        replace_bytes(path, original)
        after = path.stat()
        assert (before.st_mode, before.st_uid, before.st_gid, before.st_ino) == (
            after.st_mode, after.st_uid, after.st_gid, after.st_ino)
    print('restore-self-check: OK')


def watcher(address, attempt):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(address)
        client.sendall(attempt.encode() + b'\n')
        assert client.recv(4096) == b'ready\n'
        def restore(_signum, _frame):
            client.sendall(b'restore\n')
            assert client.recv(4096) == b'restored\n'
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, restore)
        signal.signal(signal.SIGINT, restore)
        while True:
            signal.pause()


def guard(address, attempt, fence, uid, url):
    assert os.geteuid() == 0
    path = Path('/etc/hosts')
    assert not path.is_symlink() and path.is_file()
    original = path.read_bytes()
    before = path.stat()
    assert b'files.pythonhosted.org' not in original
    backup = Path('/run') / f'gateway-dns-{attempt}.hosts-backup'
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    server = socket.socket(socket.AF_UNIX)
    client = None
    restored = False
    def stop(_signum, _frame):
        raise SystemExit(1)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.bind(address)
        os.chown(address, uid, -1)
        os.chmod(address, 0o600)
        server.settimeout(120)
        server.listen(1)
        print(json.dumps({'socket_ready': True, 'attempt': attempt}), flush=True)
        client, _ = server.accept()
        _, peer_uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        assert peer_uid == uid and client.recv(4096) == attempt.encode() + b'\n'
        state = json.loads(Path('/home/bianbu/.local/state/bibit-board-executor/state.json').read_text())['lease']
        assert state['attempt_id'] == attempt and state['fence'] == fence
        assert state['status'] == 'running'
        replace_bytes(path, original + b'\n151.101.0.223 files.pythonhosted.org\n')
        client.sendall(b'ready\n')
        result = subprocess.run(['curl', '--noproxy', '*', '--max-time', '20', '-sS', '-o', '/dev/null',
                                 '-w', 'http=%{http_code} tls_verify=%{ssl_verify_result}', url],
                                capture_output=True, text=True)
        print(json.dumps({'temporary_dns': socket.getaddrinfo('files.pythonhosted.org', 443)[0][4][0],
                          'tls_probe': result.stdout, 'probe_exit': result.returncode}), flush=True)
        client.settimeout(5400)
        client.recv(4096)
    finally:
        replace_bytes(path, original)
        after = path.stat()
        assert (before.st_mode, before.st_uid, before.st_gid, before.st_ino) == (
            after.st_mode, after.st_uid, after.st_gid, after.st_ino)
        restored = True
        print(json.dumps({'restored': True, 'hosts_sha256': hashlib.sha256(original).hexdigest(),
                          'mode': oct(after.st_mode & 0o7777), 'uid': after.st_uid, 'gid': after.st_gid,
                          'original_dns': socket.getaddrinfo('files.pythonhosted.org', 443)[0][4][0]}), flush=True)
        if client:
            try:
                client.sendall(b'restored\n')
            except OSError:
                pass
            client.close()
        server.close()
        Path(address).unlink(missing_ok=True)
        if restored:
            backup.unlink()


if __name__ == '__main__':
    if sys.argv[1] == 'self-check':
        self_check()
    elif sys.argv[1] == 'watch':
        watcher(*sys.argv[2:])
    elif sys.argv[1] == 'guard':
        guard(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6])
    else:
        raise SystemExit('unknown mode')
