"""离线运行环境的完整性、切换与管理员资产保护回归。"""

import hashlib
import importlib.util
import json
import platform
import shutil
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("spacemit-ai-gateway", "spacemit-ai-gateway-frontend")
GENERATED_DROPIN = (
    "[Service]\nUser=bianbu\nGroup=bianbu\n"
    "Environment=HOME=/home/bianbu\nUMask=0022\n"
)


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load_script("foundation_runtime", "debian/runtime.py")
prepare = load_script("foundation_prepare_runtime", "scripts/prepare-runtime.py")


def write_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_wheel(path, name="gateway-test", requirements=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
    metadata += "".join(f"Requires-Dist: {item}\n" for item in requirements)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name.replace('-', '_')}-1.0.dist-info/METADATA", metadata)
    return path


def write_manifest(bundle):
    assets = sorted(path for path in bundle.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    return write_file(bundle / "SHA256SUMS", "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(bundle)}\n"
        for path in assets
    ))


@pytest.fixture
def app_dir(tmp_path):
    app = tmp_path / "opt/spacemit-ai-gateway"
    bundle = app / "runtime"
    wheel = write_wheel(bundle / "wheels/gateway_test-1.0-py3-none-any.whl")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    write_file(bundle / "runtime-lock.txt", f"gateway-test==1.0 --hash=sha256:{digest}\n")
    write_file(bundle / "platform.json", json.dumps({
        "machine": platform.machine(), "soabi": sysconfig.get_config_var("SOABI"),
    }))
    write_manifest(bundle)
    return app


@pytest.fixture
def forbid_install(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("validation must reject the candidate before environment creation or installation")

    monkeypatch.setattr(runtime.venv.EnvBuilder, "create", forbidden)
    monkeypatch.setattr(runtime.subprocess, "run", forbidden)


def test_bundle_digest_is_reproducible_and_identifies_manifest(app_dir):
    bundle = app_dir / "runtime"
    version = runtime.validate_bundle(bundle)
    assert re.fullmatch(r"[0-9a-f]{20}", version)
    assert version == hashlib.sha256((bundle / "SHA256SUMS").read_bytes()).hexdigest()[:20]
    assert runtime.validate_bundle(bundle) == version


@pytest.mark.parametrize("damage", [
    "changed-wheel", "changed-lock", "missing-wheel", "extra-asset", "duplicate-entry",
    "bad-digest", "missing-manifest", "empty-manifest", "missing-platform-entry",
])
def test_bundle_rejects_corrupt_incomplete_or_unlisted_assets(app_dir, damage):
    bundle = app_dir / "runtime"
    manifest = bundle / "SHA256SUMS"
    wheel = next((bundle / "wheels").glob("*.whl"))
    if damage == "changed-wheel":
        wheel.write_bytes(wheel.read_bytes() + b"tampered")
    elif damage == "changed-lock":
        (bundle / "runtime-lock.txt").write_text("untrusted==9.9\n")
    elif damage == "missing-wheel":
        wheel.unlink()
    elif damage == "extra-asset":
        write_file(bundle / "wheels/unlisted.whl", "unlisted")
    elif damage == "duplicate-entry":
        manifest.write_text(manifest.read_text() + manifest.read_text().splitlines()[0] + "\n")
    elif damage == "bad-digest":
        manifest.write_text("z" * 64 + manifest.read_text()[64:])
    elif damage == "missing-manifest":
        manifest.unlink()
    elif damage == "empty-manifest":
        manifest.write_text("")
    else:
        manifest.write_text("".join(
            line + "\n" for line in manifest.read_text().splitlines() if not line.endswith("  platform.json")
        ))
    with pytest.raises((ValueError, FileNotFoundError)):
        runtime.validate_bundle(bundle)


@pytest.mark.parametrize("escape", ["relative", "absolute", "symlink", "symlink-parent"])
def test_bundle_rejects_paths_outside_bundle(app_dir, tmp_path, escape):
    bundle = app_dir / "runtime"
    outside = write_file(tmp_path / "outside.whl", "administrator asset")
    if escape == "relative":
        name = "../../../outside.whl"
    elif escape == "absolute":
        name = str(outside)
    elif escape == "symlink":
        (bundle / "wheels/external.whl").symlink_to(outside)
        name = "wheels/external.whl"
    else:
        (bundle / "external").symlink_to(tmp_path, target_is_directory=True)
        name = "external/outside.whl"
    manifest = bundle / "SHA256SUMS"
    manifest.write_text(manifest.read_text() + f"{hashlib.sha256(outside.read_bytes()).hexdigest()}  {name}\n")
    with pytest.raises(ValueError):
        runtime.validate_bundle(bundle)
    assert outside.read_text() == "administrator asset"


@pytest.mark.parametrize("field", ["machine", "soabi"])
def test_bundle_rejects_incompatible_platform_even_with_valid_checksums(app_dir, field):
    bundle = app_dir / "runtime"
    path = bundle / "platform.json"
    target = json.loads(path.read_text())
    target[field] = "incompatible-platform"
    path.write_text(json.dumps(target))
    write_manifest(bundle)
    with pytest.raises(ValueError, match="ABI|architecture"):
        runtime.validate_bundle(bundle)


@pytest.mark.parametrize("legacy_directory", [False, True], ids=["symlink", "legacy-directory"])
@pytest.mark.parametrize("failure_stage", ["venv", "pip", "imports"])
def test_install_failure_preserves_old_environment_and_retry_succeeds(
    app_dir, monkeypatch, legacy_directory, failure_stage,
):
    old = app_dir / ("venv" if legacy_directory else "runtime-" + "a" * 20)
    write_file(old / "bin/python", "old working environment")
    if not legacy_directory:
        (old / runtime.MARKER).touch()
        (old / ".complete").touch()
        (app_dir / "venv").symlink_to(old.name, target_is_directory=True)
    candidate = app_dir / f"runtime-{runtime.validate_bundle(app_dir / 'runtime')}"
    failed = False
    commands = []

    def create(builder, env_dir):
        nonlocal failed
        write_file(Path(env_dir) / "bin/python", "candidate interpreter")
        if failure_stage == "venv" and not failed:
            failed = True
            raise RuntimeError("venv creation interrupted")

    def run(command, *, env, check):
        nonlocal failed
        commands.append((command, env, check))
        assert command[0] == str(candidate / "bin/python")
        if command[1:4] == ["-m", "pip", "install"]:
            stage = "pip"
        else:
            assert command[1] == "-c"
            assert "onnxruntime" in command[2] and "cv2" in command[2]
            stage = "imports"
        if stage == failure_stage and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runtime.venv.EnvBuilder, "create", create)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        runtime.install_runtime(app=app_dir)
    assert (app_dir / "venv/bin/python").read_text() == "old working environment"
    assert (app_dir / "venv").is_symlink() is (not legacy_directory)
    assert (candidate / runtime.MARKER).is_file()
    assert not (candidate / ".complete").exists()
    write_file(candidate / "interrupted.txt", "partial install")

    runtime.install_runtime(app=app_dir)
    assert (app_dir / "venv").is_symlink()
    assert (app_dir / "venv").resolve() == candidate
    assert (candidate / ".complete").is_file()
    assert not (candidate / "interrupted.txt").exists()
    preserved = app_dir / "venv.legacy" if legacy_directory else old
    assert (preserved / "bin/python").read_text() == "old working environment"
    assert commands
    for command, env, check in commands:
        assert check is True
        assert env["PYTHONNOUSERSITE"] == "1"
        if "install" in command:
            assert env["PIP_NO_INDEX"] == "1"
            assert env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
            assert {"--no-index", "--no-deps", "--require-hashes", "--ignore-installed"} <= set(command)
            assert command[command.index("--find-links") + 1] == str(app_dir / "runtime/wheels")
            assert command[command.index("-r") + 1] == str(app_dir / "runtime/runtime-lock.txt")
    installs = sum("install" in command for command, _, _ in commands)
    checks = sum(command[1] == "-c" for command, _, _ in commands)
    runtime.install_runtime(app=app_dir)
    assert sum("install" in command for command, _, _ in commands) == installs
    assert sum(command[1] == "-c" for command, _, _ in commands) == checks + 1
    assert (app_dir / "venv").resolve() == candidate


def test_complete_candidate_is_revalidated_before_switch_and_can_retry(app_dir, monkeypatch, forbid_install):
    old = app_dir / "administrator-venv"
    write_file(old / "bin/python", "working old environment")
    link = app_dir / "venv"
    link.symlink_to(old.name, target_is_directory=True)
    candidate = app_dir / f"runtime-{runtime.validate_bundle(app_dir / 'runtime')}"
    write_file(candidate / runtime.MARKER, "")
    write_file(candidate / ".complete", "")
    write_file(candidate / "bin/python", "installed candidate")
    available = False
    checks = 0

    def run(command, *, env, check):
        nonlocal checks
        assert command[0] == str(candidate / "bin/python")
        assert command[1] == "-c"
        assert "onnxruntime" in command[2] and "cv2" in command[2]
        assert env["PYTHONNOUSERSITE"] == "1"
        assert check is True
        checks += 1
        if not available:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runtime.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        runtime.install_runtime(app=app_dir)
    assert link.resolve() == old
    assert (link / "bin/python").read_text() == "working old environment"
    assert checks == 1
    available = True
    runtime.install_runtime(app=app_dir)
    assert link.resolve() == candidate
    assert (old / "bin/python").read_text() == "working old environment"
    assert checks == 2


def test_install_refuses_to_replace_unmarked_candidate(app_dir, forbid_install):
    candidate = app_dir / f"runtime-{runtime.validate_bundle(app_dir / 'runtime')}"
    asset = write_file(candidate / "administrator.txt", "preserve")
    with pytest.raises(RuntimeError, match="unowned"):
        runtime.install_runtime(app=app_dir)
    assert asset.read_text() == "preserve"
    assert not (app_dir / "venv").exists()


def test_install_refuses_marked_candidate_symlink(app_dir, tmp_path, forbid_install):
    candidate = app_dir / f"runtime-{runtime.validate_bundle(app_dir / 'runtime')}"
    external = tmp_path / "administrator-runtime"
    asset = write_file(external / "model.bin", "administrator model")
    write_file(external / runtime.MARKER, "")
    write_file(external / ".complete", "")
    candidate.symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unowned"):
        runtime.install_runtime(app=app_dir)
    assert candidate.is_symlink()
    assert asset.read_text() == "administrator model"
    assert not (app_dir / "venv").exists()


def test_invalid_bundle_does_not_create_or_switch_environment(app_dir, forbid_install):
    old = app_dir / "administrator-venv"
    write_file(old / "bin/python", "working environment")
    link = app_dir / "venv"
    link.symlink_to(old.name, target_is_directory=True)
    (app_dir / "runtime/runtime-lock.txt").write_text("tampered==1\n")

    with pytest.raises(ValueError, match="checksum"):
        runtime.install_runtime(app=app_dir)
    assert link.resolve() == old
    assert (link / "bin/python").read_text() == "working environment"
    assert not list(app_dir.glob("runtime-*"))


def test_remove_deletes_only_marked_environments_and_preserves_admin_assets(app_dir):
    owned = app_dir / ("runtime-" + "b" * 20)
    write_file(owned / runtime.MARKER, "")
    write_file(owned / "bin/python", "owned")
    incomplete = app_dir / ("runtime-" + "c" * 20)
    write_file(incomplete / runtime.MARKER, "")
    assets = [
        write_file(app_dir / "runtime-administrator/notes.txt", "administrator"),
        write_file(app_dir / ("runtime-" + "d" * 20) / "model.bin", "unmarked model"),
        write_file(app_dir / "models/model.bin", "model"),
        write_file(app_dir / "venv.legacy/bin/python", "legacy backup"),
    ]
    external = app_dir.parent / "administrator-environment"
    assets.append(write_file(external / runtime.MARKER, "administrator"))
    alias = app_dir / ("runtime-" + "e" * 20)
    alias.symlink_to(external, target_is_directory=True)
    (app_dir / "venv").symlink_to(owned.name, target_is_directory=True)
    original = {asset: asset.read_bytes() for asset in assets}
    runtime.remove_runtime(app=app_dir)
    runtime.remove_runtime(app=app_dir)
    assert not owned.exists()
    assert not incomplete.exists()
    assert not (app_dir / "venv").is_symlink()
    assert alias.is_symlink()
    for asset, content in original.items():
        assert asset.read_bytes() == content
    assert (app_dir / "runtime/SHA256SUMS").is_file()


@pytest.mark.parametrize("as_link", [True, False], ids=["symlink", "directory"])
def test_remove_preserves_administrator_venv(app_dir, as_link):
    admin = app_dir / ("administrator-venv" if as_link else "venv")
    asset = write_file(admin / "bin/python", "administrator")
    link = app_dir / "venv"
    if as_link:
        link.symlink_to(admin.name, target_is_directory=True)
    runtime.remove_runtime(app=app_dir)
    assert link.is_symlink() is as_link
    assert link.resolve() == admin
    assert asset.read_text() == "administrator"


@pytest.fixture
def legacy_tree(tmp_path):
    etc = tmp_path / "etc/spacemit-ai-gateway"
    etc.mkdir(parents=True)
    write_file(
        tmp_path / "usr/share/spacemit-ai-gateway/base.yaml",
        (ROOT / "configs/base.yaml").read_text(),
    )
    legacy = write_file(tmp_path / "opt/spacemit-ai-gateway/configs/base.yaml", yaml.safe_dump({
        "app": {"host": "0.0.0.0", "port": 19876},
        "asr": {"model_dir": "/srv/models/custom-asr"},
    }))
    return tmp_path, etc, legacy


def test_legacy_config_is_backed_up_and_migrated_once_to_loopback(legacy_tree):
    root, etc, legacy = legacy_tree
    original = legacy.read_bytes()
    runtime.migrate_legacy(root=root)
    migrated = yaml.safe_load((etc / "base.yaml").read_text())
    assert migrated["app"] == {"host": "127.0.0.1", "port": 19876}
    assert migrated["asr"]["model_dir"] == "/srv/models/custom-asr"
    assert (etc / "legacy-base.yaml").read_bytes() == original
    assert legacy.read_bytes() == original
    assert (etc / ".legacy-migrated").is_file()
    current = (etc / "base.yaml").read_bytes()
    legacy.write_text("app: {host: 0.0.0.0, port: 29999}\n")
    runtime.migrate_legacy(root=root)
    assert (etc / "base.yaml").read_bytes() == current
    assert (etc / "legacy-base.yaml").read_bytes() == original


def test_legacy_config_does_not_replace_administrator_config(legacy_tree):
    root, etc, legacy = legacy_tree
    admin = write_file(etc / "base.yaml", "# administrator config\napp: {host: 127.0.0.1, port: 29999}\n")
    original = admin.read_bytes()
    runtime.migrate_legacy(root=root)
    assert admin.read_bytes() == original
    assert (etc / "legacy-base.yaml").read_bytes() == legacy.read_bytes()
    assert (etc / ".legacy-migrated").is_file()


def test_legacy_config_can_replace_exact_bundled_default(legacy_tree):
    root, etc, legacy = legacy_tree
    bundled = (root / "usr/share/spacemit-ai-gateway/base.yaml").read_text()
    write_file(etc / "base.yaml", bundled)
    runtime.migrate_legacy(root=root)
    data = yaml.safe_load((etc / "base.yaml").read_text())
    assert data["app"]["host"] == "127.0.0.1"
    assert data["app"]["port"] == 19876
    assert data["asr"]["model_dir"] == "/srv/models/custom-asr"
    assert (etc / "legacy-base.yaml").read_bytes() == legacy.read_bytes()


def test_upgrade_migrates_preinst_backup_after_unpack_removes_legacy(legacy_tree):
    root, etc, legacy = legacy_tree
    original = legacy.read_bytes()
    backup = etc / "legacy-base.yaml"
    backup.write_bytes(original)
    legacy.unlink()
    default = root / "usr/share/spacemit-ai-gateway/base.yaml"
    current = etc / "base.yaml"
    current.write_bytes(default.read_bytes())

    runtime.migrate_legacy(root=root)

    migrated = yaml.safe_load(current.read_text())
    assert migrated["app"] == {"host": "127.0.0.1", "port": 19876}
    assert migrated["asr"]["model_dir"] == "/srv/models/custom-asr"
    assert backup.read_bytes() == original
    assert not legacy.exists()
    assert (etc / ".legacy-migrated").is_file()
    runtime.migrate_legacy(root=root)
    assert yaml.safe_load(current.read_text()) == migrated
    assert backup.read_bytes() == original


def test_legacy_config_preserves_edited_bundled_default(legacy_tree):
    root, etc, _ = legacy_tree
    bundled = (root / "usr/share/spacemit-ai-gateway/base.yaml").read_text()
    current = write_file(etc / "base.yaml", "# administrator edit\n" + bundled)
    original = current.read_bytes()
    runtime.migrate_legacy(root=root)
    assert current.read_bytes() == original


@pytest.mark.parametrize("service", SERVICES)
def test_exact_generated_dropin_is_backed_up_and_removed(legacy_tree, service):
    root, etc, _ = legacy_tree
    path = write_file(root / f"etc/systemd/system/{service}.service.d/runtime-user.conf", GENERATED_DROPIN)
    runtime.migrate_legacy(root=root)
    assert not path.exists()
    backup = etc / f"{service}-legacy-runtime-user.conf"
    assert backup.read_text() == GENERATED_DROPIN
    runtime.migrate_legacy(root=root)
    assert backup.read_text() == GENERATED_DROPIN


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("custom", [
    GENERATED_DROPIN + "Environment=ADMIN_SETTING=keep\n",
    GENERATED_DROPIN.replace("UMask=0022", "UMask=0077"),
    "# administrator override\n" + GENERATED_DROPIN,
    "[Service]\nUser=administrator\n",
])
def test_custom_dropin_is_preserved(legacy_tree, service, custom):
    root, etc, _ = legacy_tree
    path = write_file(root / f"etc/systemd/system/{service}.service.d/runtime-user.conf", custom)
    runtime.migrate_legacy(root=root)
    assert path.read_text() == custom
    assert not (etc / f"{service}-legacy-runtime-user.conf").exists()


def test_legacy_migration_preserves_existing_backup(legacy_tree):
    root, etc, _ = legacy_tree
    backup = write_file(etc / "legacy-base.yaml", "original recovery config\n")
    runtime.migrate_legacy(root=root)
    assert backup.read_text() == "original recovery config\n"


def test_wheel_metadata_and_public_dependency_roots_keep_numpy(tmp_path):
    requirements = [
        "numpy>=2.2.6", "httpx>=0.28; python_version >= \"3.10\"",
        "opencv-python>=4.8", "spacemit-asr>=1.0.2", "spacemit_tts>=1.0.4",
    ]
    wheel = write_wheel(tmp_path / "gateway_test-1.0-py3-none-any.whl", requirements=requirements)
    info = prepare.metadata(wheel)
    assert info["Name"] == "gateway-test"
    assert info["Version"] == "1.0"
    assert info.get_all("Requires-Dist") == requirements
    assert prepare.dependency_roots([wheel], {"spacemit-asr", "spacemit-tts", "opencv-python"}) == sorted(requirements[:2])


def test_dependency_roots_deduplicates_without_losing_requirement_markers(tmp_path):
    requirement = 'numpy>=2; python_version >= "3.10"'
    first = write_wheel(tmp_path / "first.whl", "first", [requirement, "httpx>=0.28"])
    second = write_wheel(tmp_path / "second.whl", "second", [requirement])
    assert prepare.dependency_roots([first, second], set()) == sorted([requirement, "httpx>=0.28"])


@pytest.mark.parametrize("requirement", ["opencv-python>=4.8", "spacemit-secret>=1", "spacemit_unknown[extra]>=1"])
def test_dependency_roots_rejects_unpinned_vendor_dependency(tmp_path, requirement):
    wheel = write_wheel(tmp_path / "unknown.whl", requirements=[requirement])
    with pytest.raises(ValueError, match="Unpinned vendor dependency"):
        prepare.dependency_roots([wheel], {"spacemit-asr", "spacemit-tts"})


@pytest.mark.parametrize("legacy_directory", [False, True], ids=["symlink", "legacy-directory"])
def test_final_switch_failure_preserves_entry_and_retry_succeeds(app_dir, monkeypatch, legacy_directory):
    old = app_dir / ("venv" if legacy_directory else "administrator-venv")
    write_file(old / "bin/python", "working old environment")
    link = app_dir / "venv"
    if not legacy_directory:
        link.symlink_to(old.name, target_is_directory=True)
    candidate = app_dir / f"runtime-{runtime.validate_bundle(app_dir / 'runtime')}"
    write_file(candidate / runtime.MARKER, "")
    write_file(candidate / ".complete", "")
    write_file(candidate / "bin/python", "working candidate")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: None)
    replace = runtime.os.replace

    def fail_switch(source, destination):
        raise OSError("injected final switch failure")

    monkeypatch.setattr(runtime.os, "replace", fail_switch)
    with pytest.raises(OSError, match="final switch failure"):
        runtime.install_runtime(app=app_dir)
    assert (link / "bin/python").read_text() == "working old environment"
    assert link.is_symlink() is (not legacy_directory)
    assert (candidate / ".complete").is_file()
    assert not (app_dir / "venv.legacy").exists()
    monkeypatch.setattr(runtime.os, "replace", replace)
    runtime.install_runtime(app=app_dir)
    assert link.resolve() == candidate
    preserved = app_dir / "venv.legacy" if legacy_directory else old
    assert (preserved / "bin/python").read_text() == "working old environment"


def test_debhelper_installs_hardened_frontend_unit(tmp_path):
    if shutil.which("dh_installsystemd") is None:
        pytest.skip("requires debhelper")
    staging = tmp_path / "package"
    debian = staging / "debian"
    debian.mkdir(parents=True)
    for name in ("control", "changelog"):
        shutil.copyfile(ROOT / "debian" / name, debian / name)
    for unit in (ROOT / "debian").glob("*.service"):
        shutil.copyfile(unit, debian / unit.name)
    subprocess.run(["dh_installsystemd", "--name=spacemit-ai-gateway-frontend"], cwd=staging, check=True)
    units = list((debian / "spacemit-ai-gateway").rglob("spacemit-ai-gateway-frontend.service"))
    assert len(units) == 1
    content = units[0].read_text()
    assert "User=spacemit-ai-gateway\n" in content
    assert "Group=spacemit-ai-gateway\n" in content
    assert "--bind 127.0.0.1\n" in content
    assert "NoNewPrivileges=true\n" in content
    assert "ProtectSystem=full\n" in content


def test_marker_write_failure_does_not_block_offline_retry(app_dir, monkeypatch):
    old = write_file(app_dir / "venv/bin/python", "old environment")
    candidate = app_dir / f"runtime-{runtime.validate_bundle(app_dir / 'runtime')}"
    touch = Path.touch

    def fail_marker(path, *args, **kwargs):
        if path.name == runtime.MARKER:
            raise OSError(28, "No space left on device")
        return touch(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "touch", fail_marker)
        with pytest.raises(OSError, match="No space"):
            runtime.install_runtime(app=app_dir)
    assert not candidate.exists()
    assert old.read_text() == "old environment"
    assert not list(app_dir.glob(".runtime-*"))
    monkeypatch.setattr(runtime.venv.EnvBuilder, "create",
                        lambda self, path: write_file(Path(path) / "bin/python", "new environment"))
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: None)
    runtime.install_runtime(app=app_dir)
    assert (app_dir / "venv").resolve() == candidate
    assert (candidate / ".complete").is_file()
    assert candidate.stat().st_mode & 0o777 == 0o755


def test_staging_refresh_accepts_rewritten_wheels_and_preserves_platform(app_dir):
    bundle = app_dir / "runtime"
    platform_bytes = (bundle / "platform.json").read_bytes()
    original_version = runtime.validate_bundle(bundle)
    wheel = next((bundle / "wheels").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        entries = [(item.filename, archive.read(item)) for item in archive.infolist()]
    # 模拟 dh_strip_nondeterminism 改变 wheel ZIP 的时间戳与压缩方式。
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in reversed(entries):
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(item, content, compress_type=zipfile.ZIP_DEFLATED)
    extra = write_file(bundle / "metadata/SHA256SUMS", "nested asset")
    with pytest.raises(ValueError, match="checksum"):
        runtime.validate_bundle(bundle)
    subprocess.run([sys.executable, str(ROOT / "scripts/prepare-runtime.py"),
                    "--refresh", str(bundle)], check=True, capture_output=True)
    assert (bundle / "platform.json").read_bytes() == platform_bytes
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert (bundle / "runtime-lock.txt").read_text() == f"gateway-test==1.0 --hash=sha256:{digest}\n"
    assert f"  {extra.relative_to(bundle)}\n" in (bundle / "SHA256SUMS").read_text()
    version = runtime.validate_bundle(bundle)
    assert version != original_version
    result = subprocess.run([sys.executable, str(ROOT / "debian/runtime.py"),
                             "validate", str(bundle)], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == version
    prepare.write_runtime_metadata(bundle, initialize_platform=False)
    assert runtime.validate_bundle(bundle) == version


def test_staging_refresh_requires_original_platform_metadata(app_dir):
    bundle = app_dir / "runtime"
    (bundle / "platform.json").unlink()
    before = (bundle / "SHA256SUMS").read_bytes()
    with pytest.raises(FileNotFoundError, match="platform.json"):
        prepare.write_runtime_metadata(bundle, initialize_platform=False)
    assert (bundle / "SHA256SUMS").read_bytes() == before
