#!/usr/bin/env python3
"""在原生命周期的快照/finally边界内增加真实 GET/HEAD 与 chunked 验收。"""
import http.client
from contextlib import closing
import importlib.util
from pathlib import Path
import sys


def parts(size):
    yield (b'--device-audit\r\nContent-Disposition: form-data; name="file"; '
           b'filename="test.wav"\r\nContent-Type: audio/wav\r\n\r\n')
    chunk = b'x' * 65536
    while size:
        value = chunk[:min(size, len(chunk))]
        yield value
        size -= len(value)
    yield b'\r\n--device-audit--\r\n'


def request_http(path, key='', method='GET', body=None):
    headers = {'Origin': 'http://device-console'}
    if key:
        headers['X-API-Key'] = key
    if body is not None:
        headers['Content-Type'] = 'multipart/form-data; boundary=device-audit'
    with closing(http.client.HTTPConnection('127.0.0.1', 18790, timeout=15)) as client:
        client.request(method, path, body=body, headers=headers, encode_chunked=body is not None)
        response = client.getresponse()
        response.read()
        return response.status, response.getheader('Access-Control-Allow-Origin')


def check(job, gateway):
    environment = gateway.ETC / 'environment'
    original = environment.read_bytes()
    try:
        environment.write_bytes(original + b'\nSPACEMIT_AI_GATEWAY_LIMITS__MAX_UPLOAD_BYTES=1310720\n')
        job.restart()
        for method, path in [('GET', '/v1/asr/engine'), ('HEAD', '/v1/asr/engine'),
                             ('GET', '/v1/asr/jobs/device-audit-absent'),
                             ('GET', '/v1/errors/recent')]:
            gateway.require(request_http(path, method=method)[0] == 401, 'anonymous data read accepted')
        gateway.require(request_http('/healthz')[0] == 200, 'public health requires a key')
        gateway.require(request_http('/v1/asr/engine', job.key)[0] == 200, 'valid data query rejected')
        gateway.require(request_http('/v1/asr/jobs/device-audit-absent', job.key)[0] == 404,
                        'valid missing job query rejected before endpoint')
        job.checked('mr7-real-get-head-data-auth')
        pid = int(gateway.run('systemctl', 'show', '-p', 'MainPID', '--value', gateway.UNITS[0]).stdout)
        def temporary_fds():
            result = set()
            for path in Path(f'/proc/{pid}/fd').iterdir():
                try:
                    target = str(path.readlink())
                except FileNotFoundError:
                    continue
                if target.startswith(('/tmp/', '/var/tmp/')) and '(deleted)' in target:
                    result.add(target)
            return result
        before = temporary_fds()
        code, cors = request_http('/v1/asr/managed/recognize', job.key, 'POST', parts(2 * 1024 * 1024))
        job.http_probe = {'overflow_status': code, 'cors': cors}
        gateway.require(code == 413 and cors in ('*', 'http://device-console'),
                        f'chunked overflow status={code}, cors={cors}')
        gateway.require(temporary_fds() <= before, 'multipart temporary file leaked')
        code, _ = request_http('/v1/asr/managed/recognize', job.key, 'POST', parts(16))
        gateway.require(code == 422, 'small multipart did not reach field validation')
        job.checked('mr7-real-chunked-413-small-422-no-temp-fd-leak')
    finally:
        environment.write_bytes(original)
        job.restart()


def main():
    if sys.argv[1] == 'self-check':
        assert len(b''.join(parts(0))) < 1310720
        assert len(b''.join(parts(16))) < 1310720
        assert len(b''.join(parts(2 * 1024 * 1024))) > 1310720
        from unittest.mock import patch, MagicMock
        connection = MagicMock()
        connection.getresponse.return_value.status = 422
        connection.getresponse.return_value.getheader.return_value = '*'
        with patch.object(http.client, 'HTTPConnection', return_value=connection):
            assert request_http('/v1/asr/managed/recognize', body=parts(16)) == (422, '*')
        connection.close.assert_called_once()
        assert connection.request.call_args.kwargs['encode_chunked'] is True
        print('multipart-boundary-and-http-wrapper-self-check: OK')
        return 0
    boundary_only = sys.argv[1] == '--boundary-only'
    if boundary_only:
        sys.argv.pop(1)
    spec = importlib.util.spec_from_file_location('gateway_acceptance', sys.argv.pop(1))
    gateway = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gateway
    spec.loader.exec_module(gateway)
    original = gateway.Lifecycle.configure_admin
    def configure_admin(job):
        original(job)
        check(job, gateway)
    gateway.Lifecycle.configure_admin = configure_admin
    if boundary_only:
        def exercise(job):
            job.mutated = True
            job.install()
            job.restart()
            job.check_services()
            job.configure_admin()
        gateway.Lifecycle.exercise = exercise
    return gateway.main()


if __name__ == '__main__':
    raise SystemExit(main())
