#!/usr/bin/env python3
"""包拥有的离线环境与迁移文件；不删除管理员文件或模型。"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import venv

APP = Path("/opt/spacemit-ai-gateway")
MARKER = ".gateway-runtime"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_bundle(bundle):
    manifest = bundle / "SHA256SUMS"
    names = set()
    for line in manifest.read_text().splitlines():
        digest, name = line.split("  ", 1)
        path = Path(name)
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or path.is_absolute() or ".." in path.parts:
            raise ValueError("Invalid runtime manifest")
        if (name in names or not (bundle / path).is_file() or (bundle / path).is_symlink()
                or (bundle / path).resolve() != bundle.resolve() / path):
            raise ValueError(f"Invalid runtime asset: {name}")
        names.add(name)
        if sha256(bundle / path) != digest:
            raise ValueError(f"Runtime checksum mismatch: {name}")
    expected = {str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file()}
    if names != expected - {"SHA256SUMS"} or not {"runtime-lock.txt", "platform.json"} <= names:
        raise ValueError("Runtime manifest does not cover all assets")
    target = json.loads((bundle / "platform.json").read_text())
    if target != {"machine": platform.machine(), "soabi": sysconfig.get_config_var("SOABI")}:
        raise ValueError("Runtime Python ABI or architecture mismatch; rebuild the deb for this OS")
    return sha256(manifest)[:20]


def install_runtime(app=APP):
    bundle = app / "runtime"
    version = validate_bundle(bundle)
    candidate = app / f"runtime-{version}"
    if candidate.is_symlink() or (candidate.exists() and not (candidate / MARKER).is_file()):
        raise RuntimeError(f"Refusing to replace unowned directory: {candidate}")
    # 同一 bundle 的已验证环境可复用；中断安装的候选从头重建。
    if not (candidate / ".complete").is_file():
        if candidate.exists():
            shutil.rmtree(candidate)
        # 标记就绪后才发布目录，写标记失败或进程中断均不阻塞重试。
        with tempfile.TemporaryDirectory(prefix=".runtime-", dir=app) as staging:
            (Path(staging) / MARKER).touch()
            Path(staging).chmod(0o755)
            Path(staging).rename(candidate)
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(candidate)
        python = candidate / "bin/python"
        env = {**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1"}
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--require-hashes", "--ignore-installed",
                        "--find-links", str(bundle / "wheels"), "-r", str(bundle / "runtime-lock.txt")],
                       env=env, check=True)
    # 复用已安装环境时也验证系统 ORT/OpenCV，缺依赖时不能切换或假报成功。
    subprocess.run([str(candidate / "bin/python"), "-c", "import spacemit_ai_gateway, spacemit_asr, spacemit_tts, spacemit_vad, spacemit_audio, spacemit_vision, onnxruntime, cv2"],
                   env={**os.environ, "PYTHONNOUSERSITE": "1"}, check=True)
    (candidate / ".complete").touch()
    link = app / "venv"
    pending = app / "venv.next"
    if pending.is_symlink():
        pending.unlink()
    pending.symlink_to(candidate.name, target_is_directory=True)
    backup = None
    if link.exists() and not link.is_symlink():
        backup = app / "venv.legacy"
        if backup.exists():
            raise RuntimeError("Legacy venv backup exists; preserve it before retrying")
        link.rename(backup)
    try:
        os.replace(pending, link)
    except OSError:
        if backup is not None:
            backup.rename(link)
        raise


def migrate_legacy(root=Path("/")):
    etc = root / "etc/spacemit-ai-gateway"
    legacy = root / "opt/spacemit-ai-gateway/configs/base.yaml"
    marker = etc / ".legacy-migrated"
    backup = etc / "legacy-base.yaml"
    source = legacy if legacy.is_file() else backup
    if not marker.exists() and source.is_file():
        if not backup.exists():
            shutil.copy2(source, backup)
        current = etc / "base.yaml"
        default = root / "usr/share/spacemit-ai-gateway/base.yaml"
        # 仅替换本包默认配置，已有管理员配置交由管理员从备份手动合并。
        if not current.exists() or (default.is_file() and current.read_bytes() == default.read_bytes()):
            import yaml
            data = yaml.safe_load(source.read_text()) or {}
            data.setdefault("app", {})["host"] = "127.0.0.1"
            current.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        marker.touch()
    pattern = re.compile(r"\[Service\]\nUser=[^\n]+\nGroup=[^\n]+\nEnvironment=HOME=[^\n]+\nUMask=0022\n")
    for name in ("spacemit-ai-gateway", "spacemit-ai-gateway-frontend"):
        path = root / f"etc/systemd/system/{name}.service.d/runtime-user.conf"
        if path.is_file() and pattern.fullmatch(path.read_text()):
            backup = etc / f"{name}-legacy-runtime-user.conf"
            if not backup.exists():
                shutil.copy2(path, backup)
            path.unlink()


def remove_runtime(app=APP):
    link = app / "venv"
    if link.is_symlink() and re.fullmatch(r"runtime-[a-f0-9]{20}", os.readlink(link)):
        link.unlink()
    for path in app.glob("runtime-*"):
        if path.is_dir() and not path.is_symlink() and (path / MARKER).is_file():
            shutil.rmtree(path)
    # venv.legacy 是旧安装的恢复副本，保留给管理员，不递归清理 /opt。


if __name__ == "__main__":
    if sys.argv[1] == "validate":
        print(validate_bundle(Path(sys.argv[2])))
    else:
        {"install": install_runtime, "migrate": migrate_legacy, "remove": remove_runtime}[sys.argv[1]]()
