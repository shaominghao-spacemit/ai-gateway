#!/usr/bin/env python3
"""在目标架构构建 wheelhouse；安装阶段只消费这些固定资产。"""
import argparse
import email
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
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VENDOR_INDEX = "https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple"
PUBLIC_INDEX = "https://pypi.org/simple"


def run(*args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run([str(arg) for arg in args], cwd=ROOT, env=env, check=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def metadata(wheel):
    with zipfile.ZipFile(wheel) as archive:
        return email.message_from_bytes(archive.read(next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))))


def dependency_roots(wheels, vendor_names):
    requirements = set()
    for wheel in wheels:
        for requirement in metadata(wheel).get_all("Requires-Dist", []):
            name = re.split(r"[\s(<>=!~\[]", requirement, maxsplit=1)[0].lower().replace("_", "-")
            # 包括 OpenCV 的 RISC-V wheel，平台依赖必须显式固定，不能流向公共源。
            if name in vendor_names:
                continue
            if name.startswith("spacemit-") or name == "opencv-python":
                raise ValueError(f"Unpinned vendor dependency: {name}")
            requirements.add(requirement)
    return sorted(requirements)


def write_runtime_metadata(output, *, initialize_platform):
    output = Path(output)
    wheels = output / "wheels"
    if initialize_platform:
        (output / "platform.json").write_text(json.dumps({
            "machine": platform.machine(), "soabi": sysconfig.get_config_var("SOABI"),
        }) + "\n")
    elif not (output / "platform.json").is_file():
        raise FileNotFoundError(output / "platform.json")

    lock = []
    for wheel in sorted(wheels.glob("*.whl")):
        info = metadata(wheel)
        digest = sha256(wheel)
        lock.append(f"{info['Name']}=={info['Version']} --hash=sha256:{digest}")
    (output / "runtime-lock.txt").write_text("\n".join(lock) + "\n")
    assets = sorted(path for path in output.rglob("*")
                    if path.is_file() and path != output / "SHA256SUMS")
    (output / "SHA256SUMS").write_text("".join(
        f"{sha256(path)}  {path.relative_to(output)}\n" for path in assets))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", type=Path, help="refresh metadata after Debian staging transformations")
    args = parser.parse_args()
    if args.refresh is not None:
        # dh_strip_nondeterminism may rewrite wheel ZIP metadata after dh_install.
        write_runtime_metadata(args.refresh, initialize_platform=False)
        return

    output = ROOT / "build/runtime"
    if output.exists():
        shutil.rmtree(output)
    wheels = output / "wheels"
    wheels.mkdir(parents=True)
    vendor_pins = [line.strip() for line in (ROOT / "requirements-runtime.txt").read_text().splitlines()
                   if line.strip() and not line.startswith(("#", "spacemit-ai-gateway=="))]
    vendor_names = {pin.split("==")[0] for pin in vendor_pins}
    run(sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", wheels,
        "--index-url", VENDOR_INDEX, *vendor_pins)
    run(sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", wheels, ROOT)
    requirements = dependency_roots(list(wheels.glob("*.whl")), vendor_names)
    # pip wheel 无视已安装的通用包，避免把构建板的偶然状态当成运行依赖。
    run(sys.executable, "-m", "pip", "wheel", "--prefer-binary", "--wheel-dir", wheels,
        "--index-url", PUBLIC_INDEX, *requirements)
    write_runtime_metadata(output, initialize_platform=True)


if __name__ == "__main__":
    main()
