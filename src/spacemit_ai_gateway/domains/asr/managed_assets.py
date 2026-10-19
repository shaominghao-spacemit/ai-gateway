"""托管模式只验证本地完整清单，绝不下载或触发模型初始化。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import threading
from collections import OrderedDict
from pathlib import Path

from ...app.settings import AsrConfig
from .adapters.sensevoice import _REQUIRED_MODEL_FILES

QWEN_TEXT = "Qwen3-ASR-0.6B-text-q40.gguf"
QWEN_ENCODERS = (
    "Qwen3-ASR-0.6B-encoder-frontend.dynq.onnx",
    "Qwen3-ASR-0.6B-encoder-backend.dynq.onnx",
)
LANGUAGES = {"zh": "Chinese", "en": "English", "ja": "Japanese",
             "ko": "Korean", "yue": "Cantonese"}


def languages(value: str) -> list[str]:
    result = list(dict.fromkeys(part.strip().lower() for part in value.split(",")))
    if not result or any(x not in {*LANGUAGES, "auto"} for x in result):
        raise ValueError("unsupported_language")
    if "auto" in result and len(result) != 1:
        raise ValueError("auto_must_be_used_alone")
    return result


def language_support(model: str, selected: list[str]) -> str:
    # SDK 的 Language 是单值枚举；不能把列表偷偷映射成 AUTO。
    return "sensevoice_language_list_unsupported" if model == "sensevoice" and len(selected) > 1 else ""


def model_dir(config: AsrConfig, model: str) -> Path:
    value = (config.managed.qwen3.model_dir if model == "qwen3_asr_0.6b"
             else config.managed.sensevoice_dir or config.model_dir or "~/.cache/models/asr/sensevoice")
    return Path(value).expanduser().resolve()


def _check_file(path: Path, expected: str) -> None:
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError(f"invalid_digest:{path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            raise ValueError(f"empty_asset:{path.name}")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        raise ValueError(f"digest_mismatch:{path.name}")


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _signature(path: Path):
    info = path.stat()
    return (str(path.resolve()), info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def check_assets(config: AsrConfig, model: str) -> tuple[bool, str]:
    # 每次采样全部 stat 与清单内容；只缓存同一完整快照的摘要核验，不缓存缺失/失败。
    with _CACHE_LOCK:
        try:
            root = model_dir(config, model)
            manifest_path = root / "managed-asr-manifest.json"
            content = manifest_path.read_bytes()
            manifest = json.loads(content)
            paths = [(root / name).resolve() for name in manifest['files']]
            paths.extend(Path(name) for name in manifest['runtime']['libraries'])
            packages = tuple(sorted((name, importlib.metadata.version(name))
                                    for name in manifest['runtime']['packages']))
            if model == 'qwen3_asr_0.6b':
                binary = shutil.which(config.managed.qwen3.server_bin)
                if not binary:
                    return False, 'assets_unavailable:llama_server_missing'
                paths.append(Path(binary))
            paths.append(manifest_path)
            snapshot = tuple(_signature(path) for path in paths)
            key = (model, str(root), hashlib.sha256(content).digest(), packages, snapshot)
            if key in _CACHE:
                _CACHE.move_to_end(key)
                return True, 'assets_verified'
            result = _verify_assets(config, model, manifest)
            if result[0]:
                if snapshot != tuple(_signature(path) for path in paths) or content != manifest_path.read_bytes():
                    return False, 'assets_changed_during_verification'
                _CACHE[key] = True
                while len(_CACHE) > 32:
                    _CACHE.popitem(last=False)
            return result
        except (OSError, ValueError, KeyError, TypeError, AttributeError,
                importlib.metadata.PackageNotFoundError) as exc:
            return False, f"assets_unavailable:{exc}"


def _verify_assets(config: AsrConfig, model: str, manifest) -> tuple[bool, str]:
    root = model_dir(config, model)
    try:
        if manifest["model"] != model or not manifest["version"]:
            raise ValueError("asset_identity_mismatch")
        files = manifest["files"]
        required = (*QWEN_ENCODERS, QWEN_TEXT, "config.json") if model == "qwen3_asr_0.6b" else _REQUIRED_MODEL_FILES
        if not set(required).issubset(files):
            raise ValueError("incomplete_asset_manifest")
        for name, digest in files.items():
            path = (root / name).resolve()
            if not path.is_relative_to(root):
                raise ValueError("asset_outside_model_directory")
            _check_file(path, digest)
        runtime = manifest["runtime"]
        for package, version in runtime["packages"].items():
            if importlib.metadata.version(package) != version:
                raise ValueError(f"runtime_version_mismatch:{package}")
        libs = runtime["libraries"]
        if not libs:
            raise ValueError("runtime_libraries_missing")
        for name, digest in libs.items():
            if not Path(name).is_absolute():
                raise ValueError("runtime_library_requires_absolute_path")
            _check_file(Path(name), digest)
        if model == "sensevoice":
            if "spacemit-asr" not in runtime["packages"]:
                raise ValueError("spacemit_asr_version_missing")
        else:
            source = json.loads((root / "config.json").read_text())
            if not isinstance(source["audio_model"], dict):
                raise ValueError("invalid_audio_model_config")
            binary = shutil.which(config.managed.qwen3.server_bin)
            if not binary:
                raise ValueError("llama_server_missing")
            _check_file(Path(binary), runtime["server_sha256"])
        return True, "assets_verified"
    except (OSError, ValueError, KeyError, TypeError, AttributeError, importlib.metadata.PackageNotFoundError) as exc:
        return False, f"assets_unavailable:{exc}"


def _digest_nonempty(path):
    if path.stat().st_size == 0 or not path.is_file():
        raise ValueError(f'empty_or_invalid_asset:{path.name}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    """离线库存生成入口；清单不能代替来源验证与真模型验收。"""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', choices=['sensevoice', 'qwen3_asr_0.6b'], required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--version')
    parser.add_argument('--library', type=Path, action='append', default=[])
    parser.add_argument('--runtime-package', action='append', default=[])
    parser.add_argument('--server-bin', default='llama-server')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    root = args.model_dir.resolve()
    config = AsrConfig(managed={'sensevoice_dir': str(root),
                              'qwen3': {'model_dir': str(root), 'server_bin': args.server_bin}})
    if args.verify:
        ok, reason = check_assets(config, args.model)
        print(json.dumps({'assets_ready': ok, 'reason': reason}))
        return 0 if ok else 1
    if not args.version or not args.library:
        parser.error('生成清单必须指定 --version 和全部 --library')
    required = (*QWEN_ENCODERS, QWEN_TEXT, 'config.json') if args.model != 'sensevoice' else _REQUIRED_MODEL_FILES
    packages = set(args.runtime_package)
    if args.model == 'sensevoice':
        packages.add('spacemit-asr')
    runtime = {'packages': {name: importlib.metadata.version(name) for name in sorted(packages)},
               'libraries': {str(path.resolve()): _digest_nonempty(path.resolve()) for path in args.library}}
    if args.model != 'sensevoice':
        binary = shutil.which(args.server_bin)
        if not binary:
            raise ValueError('llama_server_missing')
        runtime['server_sha256'] = _digest_nonempty(Path(binary))
        if not isinstance(json.loads((root / 'config.json').read_text())['audio_model'], dict):
            raise ValueError('invalid_audio_model_config')
    manifest = {'model': args.model, 'version': args.version,
                'files': {name: _digest_nonempty(root / name) for name in required}, 'runtime': runtime}
    path = root / 'managed-asr-manifest.json'
    if not args.force:
        with path.open('x') as stream:
            json.dump(manifest, stream, indent=2)
    else:
        import tempfile
        fd, name = tempfile.mkstemp(dir=root, prefix='.managed-manifest-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(manifest, stream, indent=2)
            os.chmod(name, 0o644)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    print(json.dumps({'manifest': str(path), 'model': args.model}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
