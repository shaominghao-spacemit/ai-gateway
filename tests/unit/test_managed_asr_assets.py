import hashlib
import json
from pathlib import Path

import pytest

from spacemit_ai_gateway.app.settings import AsrConfig
from spacemit_ai_gateway.domains.asr.managed_assets import (
    QWEN_TEXT, QWEN_ENCODERS, _REQUIRED_MODEL_FILES, check_assets,
)


@pytest.fixture(params=['sensevoice', 'qwen3_asr_0.6b'])
def assets(request, tmp_path, monkeypatch):
    model = request.param
    binary, lib = tmp_path / 'llama-server', tmp_path / 'libbackend.so'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    lib.write_bytes(b'runtime-library')
    root = tmp_path / model
    root.mkdir()
    required = (*QWEN_ENCODERS, QWEN_TEXT, 'config.json') if model != 'sensevoice' else _REQUIRED_MODEL_FILES
    for name in required:
        (root / name).write_bytes(b'weights')
    if model != 'sensevoice':
        (root / 'config.json').write_text('{"audio_model": {}}')
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'model': model, 'version': 'test-v1',
                'files': {name: digest(root / name) for name in required},
                'runtime': {'packages': {'spacemit-asr': '1.0.2'},
                            'libraries': {str(lib): digest(lib)}, 'server_sha256': digest(binary)}}
    path = root / 'managed-asr-manifest.json'
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr('importlib.metadata.version', lambda name: '1.0.2')
    config = AsrConfig(managed={'enabled': True, 'sensevoice_dir': str(root),
                              'qwen3': {'model_dir': str(root), 'server_bin': str(binary)}})
    return config, model, root, manifest, path


def test_full_assets_and_digest_verification(assets):
    config, model, root, manifest, path = assets
    assert check_assets(config, model) == (True, 'assets_verified')
    first = next(iter(manifest['files']))
    (root / first).write_bytes(b'corrupt')
    assert 'digest_mismatch' in check_assets(config, model)[1]
    (root / first).unlink()
    assert not check_assets(config, model)[0]


@pytest.mark.parametrize('damage', ['manifest', 'identity', 'version', 'library', 'empty', 'missing_file', 'traversal', 'runtime_version'])
def test_incomplete_assets_never_report_ready(assets, damage, monkeypatch):
    config, model, root, manifest, path = assets
    if damage == 'manifest':
        path.unlink()
    elif damage == 'identity':
        manifest['model'] = 'wrong'
    elif damage == 'version':
        manifest['version'] = ''
    elif damage == 'library':
        Path(next(iter(manifest['runtime']['libraries']))).unlink()
    elif damage == 'empty':
        (root / next(iter(manifest['files']))).write_bytes(b'')
    elif damage == 'missing_file':
        manifest['files'].pop(next(iter(manifest['files'])))
    elif damage == 'traversal':
        manifest['files']['../bad'] = 'a' * 64
    elif damage == 'runtime_version':
        monkeypatch.setattr('importlib.metadata.version', lambda name: 'wrong')
    if damage != 'manifest':
        path.write_text(json.dumps(manifest))
    assert not check_assets(config, model)[0]


def test_stat_cache_invalidates_content_inode_and_manifest(assets, monkeypatch):
    import os
    from spacemit_ai_gateway.domains.asr import managed_assets as module
    config, model, root, manifest, path = assets
    original = module._check_file
    calls = []
    def checked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(module, '_check_file', checked)
    assert check_assets(config, model)[0]
    count = len(calls)
    assert check_assets(config, model)[0] and len(calls) == count
    asset = root / next(iter(manifest['files']))
    info = asset.stat()
    # 保持大小/mtime，ctime 仍能发现原地改写。
    asset.write_bytes(b'X' * info.st_size)
    os.utime(asset, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert not check_assets(config, model)[0] and len(calls) > count
    asset.write_bytes(b'weights')
    assert check_assets(config, model)[0]
    count = len(calls)
    replacement = root / 'replacement'
    replacement.write_bytes(asset.read_bytes())
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    replacement.replace(asset)
    assert check_assets(config, model)[0] and len(calls) > count
    count = len(calls)
    manifest['version'] = 'test-v2'
    path.write_text(json.dumps(manifest))
    assert check_assets(config, model)[0] and len(calls) > count


def test_offline_manifest_cli_generation_verify_and_empty_rejection(assets, monkeypatch, capsys):
    from spacemit_ai_gateway.domains.asr.managed_assets import main
    config, model, root, manifest, path = assets
    path.unlink()
    argv = ['--model', model, '--model-dir', str(root), '--version', 'offline-v1',
            '--library', next(iter(manifest['runtime']['libraries'])),
            '--server-bin', config.managed.qwen3.server_bin]
    assert main(argv) == 0
    assert path.is_file() and json.loads(path.read_text())['version'] == 'offline-v1'
    with pytest.raises(FileExistsError):
        main(argv)
    assert main(['--model', model, '--model-dir', str(root), '--verify',
                 '--server-bin', config.managed.qwen3.server_bin]) == 0
    (root / next(iter(manifest['files']))).write_bytes(b'')
    assert main(['--model', model, '--model-dir', str(root), '--verify',
                 '--server-bin', config.managed.qwen3.server_bin]) == 1
    with pytest.raises(ValueError, match='empty_or_invalid_asset'):
        main([*argv, '--force'])


def test_cache_invalidation_includes_runtime_library_and_packages(assets, monkeypatch):
    config, model, root, manifest, path = assets
    assert check_assets(config, model)[0]
    library = Path(next(iter(manifest['runtime']['libraries'])))
    library.write_bytes(b'changed-runtime')
    assert not check_assets(config, model)[0]
    library.write_bytes(b'runtime-library')
    assert check_assets(config, model)[0]
    monkeypatch.setattr('importlib.metadata.version', lambda name: '999')
    assert not check_assets(config, model)[0]
