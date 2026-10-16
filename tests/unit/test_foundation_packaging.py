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
def legacy_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "grant_storage_access", lambda *args: None)
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


def test_legacy_config_is_backed_up_and_migrated_once_preserving_host(legacy_tree):
    root, etc, legacy = legacy_tree
    original = legacy.read_bytes()
    runtime.migrate_legacy(root=root)
    migrated = yaml.safe_load((etc / "base.yaml").read_text())
    assert migrated["app"] == {"host": "0.0.0.0", "port": 19876}
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
    assert data["app"]["host"] == "0.0.0.0"
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
    assert migrated["app"] == {"host": "0.0.0.0", "port": 19876}
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


@pytest.fixture
def intent_upgrade(tmp_path, monkeypatch):
    """只升级 Gateway；旧 Intent 的配置和无 key 客户端保持原样。"""
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    config = {
        'app': {'host': '0.0.0.0', 'port': 18790},
        'auth': {'enabled': False, 'api_keys': []},
    }
    for domain in ('llm', 'embed', 'rerank'):
        config[domain] = {'storage': {
            'base_dir': f'~/.cache/spacemit-ai-gateway/{domain}',
            'models_dir': f'~/.cache/models/{domain}',
            'db_path': f'~/.cache/spacemit-ai-gateway/{domain}/db.sqlite',
        }}
    write_file(tmp_path / 'opt/spacemit-ai-gateway/configs/base.yaml', yaml.safe_dump(config))
    write_file(tmp_path / 'usr/share/spacemit-ai-gateway/base.yaml', (ROOT / 'configs/base.yaml').read_text())
    etc.mkdir(parents=True)
    # ACL 在后续真实本机文件系统测试中另行检验；此处验证路径/事务边界。
    grants = []
    monkeypatch.setattr(runtime, 'grant_storage_access', lambda root, paths: grants.extend(paths), raising=False)
    return tmp_path, etc, config, grants


@pytest.mark.parametrize('home', ['/root', '/home/bianbu'])
def test_upgrade_preserves_legacy_home_and_no_key_intent(intent_upgrade, home):
    root, etc, config, grants = intent_upgrade
    if home != '/root':
        write_file(root / 'etc/systemd/system/spacemit-ai-gateway.service.d/runtime-user.conf', GENERATED_DROPIN)
    original = (root / 'opt/spacemit-ai-gateway/configs/base.yaml').read_bytes()
    runtime.migrate_legacy(root=root)
    migrated = yaml.safe_load((etc / 'base.yaml').read_text())
    for domain in ('llm', 'embed', 'rerank'):
        for field, value in config[domain]['storage'].items():
            assert migrated[domain]['storage'][field] == value.replace('~', home, 1)
        assert (f'{home}/.cache/models/{domain}', True) in grants
        assert (f'{home}/.cache/spacemit-ai-gateway/{domain}', True) in grants
    credentials = (etc / 'credentials.env').read_text()
    assert 'AUTH__ENABLED=true' not in credentials
    assert 'AUTH__API_KEYS=' not in credentials
    assert migrated['auth'] == config['auth']
    assert (etc / 'legacy-base.yaml').read_bytes() == original
    assert (etc / 'legacy-base.yaml').stat().st_mode & 0o777 == 0o600
    migrated_bytes = (etc / 'base.yaml').read_bytes()
    credentials_bytes = (etc / 'credentials.env').read_bytes()
    runtime.migrate_legacy(root=root)
    assert (etc / 'base.yaml').read_bytes() == migrated_bytes
    assert (etc / 'credentials.env').read_bytes() == credentials_bytes


def test_fresh_install_still_requires_generated_key(tmp_path):
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    runtime.migrate_legacy(root=tmp_path)
    credentials = (etc / 'credentials.env').read_text()
    assert 'SPACEMIT_AI_GATEWAY_AUTH__ENABLED=true' in credentials
    assert 'SPACEMIT_AI_GATEWAY_AUTH__API_KEYS=' in credentials
    assert (etc / 'credentials.env').stat().st_mode & 0o777 == 0o600


def test_upgrade_keeps_admin_auth_environment_and_existing_key(intent_upgrade):
    root, etc, _, _ = intent_upgrade
    admin = 'SPACEMIT_AI_GATEWAY_AUTH__ENABLED=true\nSPACEMIT_AI_GATEWAY_AUTH__API_KEYS=\'["admin-key"]\'\n'
    write_file(etc / 'environment', admin)
    runtime.migrate_legacy(root=root)
    assert (etc / 'environment').read_text() == admin
    assert 'AUTH__API_KEYS=' not in (etc / 'credentials.env').read_text()


def test_upgrade_materializes_admin_environment_storage_without_losing_auth(intent_upgrade):
    root, etc, _, grants = intent_upgrade
    original = ('# local administrator\nSPACEMIT_AI_GATEWAY_AUTH__ENABLED=false\n'
                'SPACEMIT_AI_GATEWAY_LLM__STORAGE__MODELS_DIR="~/.cache/private models"\n')
    write_file(etc / 'environment', original)
    runtime.migrate_legacy(root=root)
    migrated = (etc / 'environment').read_text()
    assert 'AUTH__ENABLED=false' in migrated
    assert '/root/.cache/private models' in migrated
    assert ('/root/.cache/private models', True) in grants
    assert (etc / 'legacy-environment').read_text() == original


def test_upgrade_replays_preinst_effective_environment(intent_upgrade):
    root, etc, _, grants = intent_upgrade
    write_file(etc / 'legacy-context.json', json.dumps({
        'home': '/home/operator', 'user': 'operator',
        'environment': {'SPACEMIT_AI_GATEWAY_AUTH__ENABLED': 'false',
                        'SPACEMIT_AI_GATEWAY_EMBED__STORAGE__MODELS_DIR': '~/models/embed'},
    }))
    runtime.migrate_legacy(root=root)
    creds = (etc / 'credentials.env').read_text()
    assert 'SPACEMIT_AI_GATEWAY_AUTH__ENABLED="false"' in creds
    assert '/home/operator/models/embed' in creds
    assert ('/home/operator/models/embed', True) in grants
    assert yaml.safe_load((etc / 'base.yaml').read_text())['llm']['storage']['models_dir'].startswith('/home/operator/')


def test_upgrade_acl_failure_does_not_publish_or_discard_old_identity(intent_upgrade, monkeypatch):
    root, etc, _, _ = intent_upgrade
    dropin = write_file(root / 'etc/systemd/system/spacemit-ai-gateway.service.d/runtime-user.conf', GENERATED_DROPIN)
    def fail(*args):
        raise RuntimeError('ACL unavailable')
    monkeypatch.setattr(runtime, 'grant_storage_access', fail, raising=False)
    with pytest.raises(RuntimeError, match='ACL unavailable'):
        runtime.migrate_legacy(root=root)
    assert dropin.is_file()
    assert not (etc / '.legacy-migrated').exists()
    assert not (etc / 'credentials.env').exists()
    assert (etc / 'legacy-base.yaml').is_file()


def preinst_snapshot():
    source = (ROOT / 'debian/spacemit-ai-gateway.preinst').read_text()
    script = source.split("<<'PYCONTEXT'\n", 1)[1].split('\nPYCONTEXT', 1)[0]
    namespace = {'__name__': 'test_preinst'}
    exec(compile(script, 'preinst-snapshot', 'exec'), namespace)
    return namespace['snapshot']


def test_preinst_snapshots_actual_root_process_home_and_effective_auth(tmp_path, monkeypatch):
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    # unit 中 false 可被 EnvironmentFile 覆盖；/proc 才是当前有效值。
    shown = 'User=root\nMainPID=123\nEnvironment=HOME=/wrong SPACEMIT_AI_GATEWAY_AUTH__ENABLED=false\n'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, shown, ''))
    write_file(tmp_path / 'proc/123/environ', 'HOME=/root\0SPACEMIT_AI_GATEWAY_AUTH__ENABLED=true\0SECRET=do-not-copy\0')
    snapshot = preinst_snapshot()
    snapshot(root=tmp_path, proc=tmp_path / 'proc')
    path = etc / 'legacy-context.json'
    context = json.loads(path.read_text())
    assert context['home'] == '/root'
    assert context['user'] == 'root'
    assert context['environment'] == {'SPACEMIT_AI_GATEWAY_AUTH__ENABLED': 'true'}
    assert path.stat().st_mode & 0o777 == 0o600
    original = path.read_bytes()
    write_file(tmp_path / 'proc/123/environ', 'HOME=/var/lib/new\0')
    snapshot(root=tmp_path, proc=tmp_path / 'proc')
    assert path.read_bytes() == original


def test_preinst_stopped_service_reads_admin_environment(tmp_path, monkeypatch):
    shown = 'User=root\nMainPID=0\nEnvironment=HOME=/root\n'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, shown, ''))
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    write_file(etc / 'credentials.env', 'SPACEMIT_AI_GATEWAY_AUTH__ENABLED=true\n')
    write_file(etc / 'environment', 'SPACEMIT_AI_GATEWAY_AUTH__ENABLED=false\n')
    preinst_snapshot()(root=tmp_path, proc=tmp_path / 'proc')
    assert json.loads((etc / 'legacy-context.json').read_text())['environment']['SPACEMIT_AI_GATEWAY_AUTH__ENABLED'] == 'false'


def test_preinst_refuses_unknown_old_identity_instead_of_guessing(tmp_path, monkeypatch):
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, '', 'unavailable'))
    with pytest.raises(RuntimeError, match='identity'):
        preinst_snapshot()(root=tmp_path)
    assert not (etc / 'legacy-context.json').exists()


def acl_entries(path, default=False):
    import struct
    attribute = 'system.posix_acl_default' if default else 'system.posix_acl_access'
    value = path.__fspath__()
    import os
    return {(tag, uid): bits for tag, bits, uid in struct.iter_unpack('<HHI', os.getxattr(value, attribute)[4:])}


def test_storage_acl_preserves_models_owner_others_and_masked_groups(tmp_path):
    import os
    import struct
    import sqlite3
    root = tmp_path
    (root / 'etc/spacemit-ai-gateway').mkdir(parents=True)
    home = root / 'root'
    model = write_file(home / '.cache/models/llm/model.gguf', 'existing model bytes')
    db = write_file(home / '.cache/spacemit-ai-gateway/llm/db.sqlite', '')
    home.chmod(0o700)
    (home / '.cache').chmod(0o700)
    model.chmod(0o600)
    db.chmod(0o600)
    db.parent.chmod(0o700)
    unrelated = write_file(home / 'private.txt', 'private')
    unrelated.chmod(0o600)
    original_inode, original_uid = model.stat().st_ino, model.stat().st_uid
    # group 对文件有 rwx 但被 mask=r-- 屏蔽；新增服务 rw 不能激活其 w/x。
    original_acl = [(1, 6, 0xffffffff), (4, 7, 0xffffffff), (8, 7, 60002), (16, 4, 0xffffffff), (32, 0, 0xffffffff)]
    os.setxattr(model, 'system.posix_acl_access', struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in original_acl))
    runtime.grant_storage_access(root, [('/root/.cache/models/llm', True),
                                        ('/root/.cache/spacemit-ai-gateway/llm', True)], uid=60001)
    entries = acl_entries(model)
    assert entries[2, 60001] == 6
    assert entries[4, 0xffffffff] == 4
    assert entries[8, 60002] == 4
    assert entries[32, 0xffffffff] == 0
    assert acl_entries(home)[2, 60001] == 1
    assert not (home.stat().st_mode & 0o007)
    assert (model.stat().st_ino, model.stat().st_uid) == (original_inode, original_uid)
    assert model.read_text() == 'existing model bytes'
    assert unrelated.stat().st_mode & 0o777 == 0o600
    with pytest.raises(OSError):
        os.getxattr(unrelated, 'system.posix_acl_access')
    assert acl_entries(db.parent, default=True)[2, 60001] == 7
    assert acl_entries(db.parent, default=True)[4, 0xffffffff] == 0
    assert acl_entries(db.parent, default=True)[32, 0xffffffff] == 0
    # 原 SQLite 原地可继续 WAL 写入；新建文件实际继承服务 UID ACL。
    with sqlite3.connect(db) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE IF NOT EXISTS models (name TEXT)')
        connection.execute("INSERT INTO models VALUES ('kept')")
    new = write_file(db.parent / 'future.db', 'next')
    assert acl_entries(new)[2, 60001] & 6 == 6
    log = root / 'etc/spacemit-ai-gateway/legacy-storage-acl.jsonl'
    before = log.read_bytes()
    runtime.grant_storage_access(root, [('/root/.cache/models/llm', True),
                                        ('/root/.cache/spacemit-ai-gateway/llm', True)], uid=60001)
    # 重入只会补新文件的恢复记录，已有 inode 的初始 ACL 记录不被覆盖。
    records = [json.loads(line) for line in log.read_text().splitlines()]
    original_records = [json.loads(line) for line in before.decode().splitlines()]
    assert records[:len(original_records)] == original_records
    assert sum(entry['path'] == str(model) for entry in records) == 1
    assert log.stat().st_mode & 0o777 == 0o600


def test_storage_acl_creates_missing_cache_and_refuses_home_tree(tmp_path):
    (tmp_path / 'etc/spacemit-ai-gateway').mkdir(parents=True)
    runtime.grant_storage_access(tmp_path, [('/root/.cache/spacemit-ai-gateway/rerank', True)], uid=60001)
    created = tmp_path / 'root/.cache/spacemit-ai-gateway/rerank'
    assert created.is_dir()
    assert acl_entries(created)[2, 60001] == 7
    for broad in ('/root', '/home/bianbu', '/', '/etc'):
        with pytest.raises(RuntimeError, match='broad'):
            runtime.grant_storage_access(tmp_path, [(broad, True)], uid=60001)


def test_existing_credentials_and_admin_yaml_remain_authoritative(intent_upgrade):
    root, etc, _, grants = intent_upgrade
    original = 'SPACEMIT_AI_GATEWAY_AUTH__ENABLED=true\nSPACEMIT_AI_GATEWAY_AUTH__API_KEYS=\'["unchanged"]\'\n'
    write_file(etc / 'credentials.env', original)
    admin_yaml = 'auth: {enabled: true, api_keys: [yaml-key]}\nllm:\n  storage:\n    models_dir: ~/.cache/admin-models/llm\n'
    write_file(etc / 'base.yaml', admin_yaml)
    runtime.migrate_legacy(root=root)
    assert (etc / 'credentials.env').read_text().startswith(original)
    assert 'LLM__STORAGE__MODELS_DIR="/root/.cache/admin-models/llm"' in (etc / 'credentials.env').read_text()
    assert (etc / 'base.yaml').read_text() == admin_yaml
    assert ('/root/.cache/admin-models/llm', True) in grants


def test_migration_retries_after_config_publish_without_losing_environment(intent_upgrade, monkeypatch):
    root, etc, _, _ = intent_upgrade
    write_file(etc / 'legacy-context.json', json.dumps({'home': '/root', 'environment': {
        'SPACEMIT_AI_GATEWAY_AUTH__ENABLED': 'false',
        'SPACEMIT_AI_GATEWAY_LLM__STORAGE__MODELS_DIR': '~/.cache/models/llm',
    }}))
    real = runtime.private_write
    def fail(path, text):
        if path.name == 'credentials.env':
            raise OSError('power interruption')
        return real(path, text)
    monkeypatch.setattr(runtime, 'private_write', fail)
    with pytest.raises(OSError, match='interruption'):
        runtime.migrate_legacy(root=root)
    assert not (etc / '.legacy-migrated').exists()
    published = (etc / 'base.yaml').read_bytes()
    monkeypatch.setattr(runtime, 'private_write', real)
    runtime.migrate_legacy(root=root)
    assert (etc / 'base.yaml').read_bytes() == published
    assert '/root/.cache/models/llm' in (etc / 'credentials.env').read_text()
    assert 'AUTH__ENABLED="false"' in (etc / 'credentials.env').read_text()


def test_preinst_copies_effective_custom_yaml_before_unpack(tmp_path, monkeypatch):
    shown = 'User=root\nMainPID=0\nEnvironment=HOME=/root SPACEMIT_AI_GATEWAY_CONFIG=/srv/gateway.yaml\n'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, shown, ''))
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    config = write_file(tmp_path / 'srv/gateway.yaml', 'auth: {enabled: false}\n')
    preinst_snapshot()(root=tmp_path, proc=tmp_path / 'proc')
    backup = etc / 'legacy-effective-base.yaml'
    assert backup.read_bytes() == config.read_bytes()
    assert backup.stat().st_mode & 0o777 == 0o600
    config.write_text('auth: {enabled: true}\n')
    preinst_snapshot()(root=tmp_path, proc=tmp_path / 'proc')
    assert 'false' in backup.read_text()


def test_upgrade_after_old_foundation_marker_still_migrates_storage(intent_upgrade):
    root, etc, _, grants = intent_upgrade
    write_file(etc / '.legacy-migrated', '')
    runtime.migrate_legacy(root=root)
    assert ('/root/.cache/models/llm', True) in grants
    assert (etc / '.legacy-storage-migrated').exists()


def test_storage_acl_interrupted_journal_can_retry_without_discarding_original(tmp_path):
    (tmp_path / 'etc/spacemit-ai-gateway').mkdir(parents=True)
    model = write_file(tmp_path / 'root/.cache/models/llm/model.gguf', 'kept')
    runtime.grant_storage_access(tmp_path, [('/root/.cache/models/llm', True)], uid=60001)
    log = tmp_path / 'etc/spacemit-ai-gateway/legacy-storage-acl.jsonl'
    original = log.read_bytes()
    with log.open('a') as stream:
        stream.write('{"path":')
    runtime.grant_storage_access(tmp_path, [('/root/.cache/models/llm', True)], uid=60001)
    assert log.read_bytes() == original
    assert model.read_text() == 'kept'


def test_upgrade_preserves_relative_admin_environment_paths(intent_upgrade):
    root, etc, _, grants = intent_upgrade
    write_file(etc / 'environment', 'SPACEMIT_AI_GATEWAY_LLM__STORAGE__MODELS_DIR=local/models/llm\n')
    write_file(etc / 'legacy-context.json', json.dumps({'home': '/root', 'working_directory': '/srv/gateway', 'environment': {}}))
    runtime.migrate_legacy(root=root)
    assert ('/srv/gateway/local/models/llm', True) in grants
    assert '"/srv/gateway/local/models/llm"' in (etc / 'environment').read_text()


@pytest.mark.parametrize('installation', ['legacy', 'fresh', 'admin'])
def test_installed_auth_precedence_through_http_middleware(intent_upgrade, monkeypatch, installation):
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spacemit_ai_gateway.app.settings import Settings
    from spacemit_ai_gateway.gateway.auth import GatewayAuthMiddleware
    from spacemit_ai_gateway.gateway.errors import setup_exception_handlers

    root, etc, _, _ = intent_upgrade
    if installation == 'fresh':
        (root / 'opt/spacemit-ai-gateway/configs/base.yaml').unlink()
        write_file(etc / 'base.yaml', (ROOT / 'configs/base.yaml').read_text())
    elif installation == 'admin':
        write_file(etc / 'environment', 'SPACEMIT_AI_GATEWAY_AUTH__ENABLED=true\nSPACEMIT_AI_GATEWAY_AUTH__API_KEYS=\'["admin-key"]\'\n')
    runtime.migrate_legacy(root=root)
    for key in list(os.environ):
        if key.startswith('SPACEMIT_AI_GATEWAY_'):
            monkeypatch.delenv(key)
    for name in ('credentials.env', 'environment'):
        for key, value in runtime.environment_values(etc / name).items():
            monkeypatch.setenv(key, value)
    settings = Settings(**yaml.safe_load((etc / 'base.yaml').read_text()))
    monkeypatch.setattr('spacemit_ai_gateway.gateway.auth.get_settings', lambda: settings)
    app = FastAPI()
    setup_exception_handlers(app)
    app.add_middleware(GatewayAuthMiddleware)
    @app.post('/v1/embed')
    async def request():
        return {'ok': True}
    with TestClient(app) as client:
        # 旧 Intent 不发送 X-API-Key；仅旧 disabled 安装允许原样调用。
        assert client.post('/v1/embed').status_code == (200 if installation == 'legacy' else 401)
        if installation != 'legacy':
            assert settings.auth.enabled
            assert client.post('/v1/embed', headers={'X-API-Key': settings.auth.api_keys[0]}).status_code == 200


def test_unsupported_acl_fails_without_config_publication(intent_upgrade, monkeypatch):
    import errno
    root, etc, _, _ = intent_upgrade
    actual_grant = load_script('foundation_acl_real', 'debian/runtime.py').grant_storage_access
    monkeypatch.setattr(runtime, 'grant_storage_access', lambda root, paths: actual_grant(root, paths, uid=60001))
    def fail(*args):
        raise OSError(errno.EOPNOTSUPP, 'unsupported ACL')
    monkeypatch.setattr(runtime.os, 'setxattr', fail)
    with pytest.raises(RuntimeError, match='ACL'):
        runtime.migrate_legacy(root=root)
    assert not (etc / 'credentials.env').exists()
    assert not (etc / '.legacy-migrated').exists()
    assert not (etc / 'base.yaml').exists()


@pytest.mark.parametrize('installation,expected', [
    ('legacy', '0.0.0.0'), ('unit-env', '10.0.90.159'),
    ('admin-env', '192.0.2.10'), ('admin-yaml', '192.0.2.20'),
    ('fresh', '127.0.0.1'),
])
def test_installed_listener_preserves_upgrade_and_admin_choices(intent_upgrade, monkeypatch, installation, expected):
    import os
    from spacemit_ai_gateway.app.settings import Settings
    root, etc, _, _ = intent_upgrade
    original_admin = None
    if installation in ('unit-env', 'admin-env'):
        write_file(etc / 'legacy-context.json', json.dumps({
            'home': '/root', 'environment': {'SPACEMIT_AI_GATEWAY_APP__HOST': '10.0.90.159'},
        }))
    if installation == 'admin-env':
        original_admin = 'SPACEMIT_AI_GATEWAY_APP__HOST=192.0.2.10\n'
        write_file(etc / 'environment', original_admin)
    elif installation == 'admin-yaml':
        original_admin = '# administrator listener\napp: {host: 192.0.2.20, port: 19876}\n'
        write_file(etc / 'base.yaml', original_admin)
    elif installation == 'fresh':
        (root / 'opt/spacemit-ai-gateway/configs/base.yaml').unlink()
        write_file(etc / 'base.yaml', (ROOT / 'configs/base.yaml').read_text())
    runtime.migrate_legacy(root=root)
    for key in list(os.environ):
        if key.startswith('SPACEMIT_AI_GATEWAY_'):
            monkeypatch.delenv(key)
    for name in ('credentials.env', 'environment'):
        for key, value in runtime.environment_values(etc / name).items():
            monkeypatch.setenv(key, value)
    assert Settings(**yaml.safe_load((etc / 'base.yaml').read_text())).app.host == expected
    if original_admin is not None:
        path = etc / ('environment' if installation == 'admin-env' else 'base.yaml')
        assert path.read_text() == original_admin


@pytest.mark.parametrize('unavailable', ['failed', 'missing'])
def test_preinst_offline_root_upgrade_preserves_listener_auth_and_storage(intent_upgrade, monkeypatch, unavailable):
    root, etc, _, grants = intent_upgrade
    write_file(root / 'usr/lib/systemd/system/spacemit-ai-gateway.service',
               '[Service]\nUser=root\nWorkingDirectory=/opt/spacemit-ai-gateway\n'
               'Environment=SPACEMIT_AI_GATEWAY_CONFIG=/opt/spacemit-ai-gateway/configs/base.yaml\n'
               'Environment=SPACEMIT_AI_GATEWAY_APP__HOST=10.0.90.159\n'
               'EnvironmentFile=-/etc/spacemit-ai-gateway/environment\n')
    original = 'SPACEMIT_AI_GATEWAY_AUTH__ENABLED=false\nSPACEMIT_AI_GATEWAY_APP__HOST=0.0.0.0\n'
    write_file(etc / 'environment', original)
    def unavailable_systemctl(*args, **kwargs):
        if unavailable == 'missing':
            raise FileNotFoundError('systemctl')
        return subprocess.CompletedProcess(args, 1, '', 'Running in chroot, ignoring command')
    monkeypatch.setattr(subprocess, 'run', unavailable_systemctl)
    snapshot = preinst_snapshot()
    snapshot(root=root, proc=root / 'proc')
    context_path = etc / 'legacy-context.json'
    context = json.loads(context_path.read_text())
    assert context['user'] == 'root'
    assert context['home'] == '/root'
    assert context['environment']['SPACEMIT_AI_GATEWAY_APP__HOST'] == '0.0.0.0'
    assert context_path.stat().st_mode & 0o777 == 0o600
    original_context = context_path.read_bytes()
    # unpack 后仅余 preinst 的备份，迁移依然使用旧 HOME 与配置。
    (root / 'opt/spacemit-ai-gateway/configs/base.yaml').unlink()
    runtime.migrate_legacy(root=root)
    assert yaml.safe_load((etc / 'base.yaml').read_text())['app']['host'] == '0.0.0.0'
    assert ('/root/.cache/models/llm', True) in grants
    assert (etc / 'environment').read_text() == original
    snapshot(root=root, proc=root / 'proc')
    assert context_path.read_bytes() == original_context


def test_preinst_offline_dropin_home_environment_order_and_custom_yaml(tmp_path, monkeypatch):
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    write_file(tmp_path / 'lib/systemd/system/spacemit-ai-gateway.service',
               '[Service]\nUser=root\nWorkingDirectory=/opt/spacemit-ai-gateway\n'
               'Environment=HOME=/root\nEnvironment=SPACEMIT_AI_GATEWAY_APP__HOST=0.0.0.0\n')
    dropins = tmp_path / 'etc/systemd/system/spacemit-ai-gateway.service.d'
    write_file(dropins / 'runtime-user.conf', GENERATED_DROPIN)
    write_file(dropins / 'zz-admin.conf',
               '[Service]\nEnvironment=SPACEMIT_AI_GATEWAY_CONFIG=~/gateway.yaml\n'
               'EnvironmentFile=-/etc/spacemit-ai-gateway/credentials.env\n'
               'EnvironmentFile=/etc/spacemit-ai-gateway/environment\n')
    write_file(etc / 'credentials.env', 'SPACEMIT_AI_GATEWAY_APP__HOST=192.0.2.1\n')
    write_file(etc / 'environment', 'SPACEMIT_AI_GATEWAY_APP__HOST=192.0.2.2\n')
    original = 'app: {host: 0.0.0.0}\nauth: {enabled: false}\n'
    write_file(tmp_path / 'home/bianbu/gateway.yaml', original)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, '', 'offline'))
    preinst_snapshot()(root=tmp_path)
    context = json.loads((etc / 'legacy-context.json').read_text())
    assert context['user'] == 'bianbu'
    assert context['home'] == '/home/bianbu'
    assert context['environment']['SPACEMIT_AI_GATEWAY_APP__HOST'] == '192.0.2.2'
    assert (etc / 'legacy-effective-base.yaml').read_text() == original


@pytest.mark.parametrize('directive', ['DynamicUser=yes', 'Environment=HOME=%h',
                                       'EnvironmentFile=/etc/missing-required'])
def test_preinst_offline_ambiguous_identity_or_environment_fails(tmp_path, monkeypatch, directive):
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    write_file(tmp_path / 'usr/lib/systemd/system/spacemit-ai-gateway.service',
               '[Service]\nUser=root\n' + directive + '\n')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, '', 'offline'))
    with pytest.raises((RuntimeError, FileNotFoundError)):
        preinst_snapshot()(root=tmp_path)
    assert not (etc / 'legacy-context.json').exists()


@pytest.mark.parametrize('chroot', [False, True])
def test_preinst_distinguishes_live_query_failure_from_bind_mounted_chroot(tmp_path, monkeypatch, chroot):
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    (tmp_path / 'run/systemd/system').mkdir(parents=True)
    proc = tmp_path / 'proc'
    (proc / '1').mkdir(parents=True)
    host_root = tmp_path / 'host-root'
    host_root.mkdir()
    (proc / '1/root').symlink_to(host_root if chroot else tmp_path)
    write_file(tmp_path / 'usr/lib/systemd/system/spacemit-ai-gateway.service',
               '[Service]\nUser=root\nEnvironment=HOME=/root\n')
    def query(*args, **kwargs):
        if chroot:
            pytest.fail('chroot must not query a bind-mounted host systemd')
        return subprocess.CompletedProcess(args, 1, '', 'live manager unavailable')
    monkeypatch.setattr(subprocess, 'run', query)
    if chroot:
        preinst_snapshot()(root=tmp_path, proc=proc)
        assert json.loads((etc / 'legacy-context.json').read_text())['home'] == '/root'
    else:
        with pytest.raises(RuntimeError, match='identity'):
            preinst_snapshot()(root=tmp_path, proc=proc)
        assert not (etc / 'legacy-context.json').exists()


def test_preinst_offline_snapshot_succeeds_in_subprocess_without_systemctl(tmp_path):
    import os
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    write_file(tmp_path / 'usr/lib/systemd/system/spacemit-ai-gateway.service',
               '[Service]\nUser=root\nEnvironment=HOME=/root\n'
               'Environment="SPACEMIT_AI_GATEWAY_APP__HOST=0.0.0.0"\n')
    empty_bin = tmp_path / 'empty-bin'
    empty_bin.mkdir()
    source = (ROOT / 'debian/spacemit-ai-gateway.preinst').read_text()
    script = source.split("<<'PYCONTEXT'\n", 1)[1].split('\nPYCONTEXT', 1)[0]
    # 独立进程运行实际 preinst Python；不模拟 subprocess.run，不依赖 systemd/登录用户。
    script = script.split("if __name__ == '__main__':", 1)[0]
    script += '\nimport sys\nsnapshot(root=Path(sys.argv[1]), proc=Path(sys.argv[1]) / "proc")\n'
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            env={**os.environ, 'PATH': str(empty_bin)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    context = json.loads((etc / 'legacy-context.json').read_text())
    assert context['home'] == '/root'
    assert context['environment']['SPACEMIT_AI_GATEWAY_APP__HOST'] == '0.0.0.0'


@pytest.fixture
def external_registered_models(intent_upgrade, monkeypatch):
    import sqlite3
    root, etc, _, _ = intent_upgrade
    real_acl = load_script('foundation_external_acl', 'debian/runtime.py').grant_storage_access
    monkeypatch.setattr(runtime, 'grant_storage_access', lambda root, paths: real_acl(root, paths, uid=60001))
    home = root / 'home/bianbu'
    paths = {
        'llm': '/home/bianbu/.cache/models/llm/glm-edge-1.5b-chat-q4_0.gguf',
        'embed': '/home/bianbu/.cache/models/embed/Qwen3-Embedding-0.6B-Q4_0.gguf',
    }
    databases = {}
    for domain, path in paths.items():
        model = write_file(root / path.lstrip('/'), domain + ' existing model')
        model.chmod(0o600)
        model.parent.chmod(0o700)
        database = root / f'root/.cache/spacemit-ai-gateway/{domain}/db.sqlite'
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE models (id TEXT PRIMARY KEY, source_type TEXT, local_path TEXT, api_key TEXT)')
            connection.execute('INSERT INTO models VALUES (?, ?, ?, ?)',
                               ('bibit-' + domain, 'local_path', path, 'unchanged-private-key'))
        databases[domain] = database
    home.chmod(0o750)
    sibling = write_file(home / '.cache/models/llm/unregistered.gguf', 'unrelated private model')
    sibling.chmod(0o600)
    return root, etc, paths, databases, sibling


def restore_external_model_acl(journal):
    import base64
    device = load_script('foundation_external_rollback', 'debian/tests/test-device-lifecycle.py')
    lifecycle = device.Lifecycle.__new__(device.Lifecycle)
    lifecycle.storage_acl_before = {'identity': None, 'content': ''}
    lifecycle.storage_acl_pass = 0
    lifecycle.read_storage_acl_journal = lambda: {
        'identity': {'exists': True}, 'content': base64.b64encode(journal.read_bytes()).decode(),
    }
    lifecycle.private_acl_evidence = lambda *args: None
    lifecycle.restore_storage_acl()


def test_upgrade_db_registered_external_files_get_exact_read_acl_and_rollback(external_registered_models):
    import os
    root, etc, paths, databases, sibling = external_registered_models
    before_db = {path: path.read_bytes() for path in databases.values()}
    before_model = {root / path.lstrip('/'): (root / path.lstrip('/')).stat() for path in paths.values()}
    model_contents = {path: path.read_bytes() for path in before_model}
    runtime.migrate_legacy(root=root)
    assert all(path.read_bytes() == content for path, content in model_contents.items())
    for path, original in before_model.items():
        assert acl_entries(path)[2, 60001] == 4
        assert (path.stat().st_ino, path.stat().st_uid) == (original.st_ino, original.st_uid)
        assert acl_entries(path.parent)[2, 60001] == 1
    assert acl_entries(root / 'home/bianbu')[2, 60001] == 1
    assert not (root / 'home/bianbu').stat().st_mode & 0o007
    with pytest.raises(OSError):
        os.getxattr(sibling, 'system.posix_acl_access')
    for path, contents in before_db.items():
        assert path.read_bytes() == contents
    journal = etc / 'legacy-storage-acl.jsonl'
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert set(map(str, before_model)) <= {record['path'] for record in records}
    assert journal.stat().st_mode & 0o777 == 0o600
    restore_external_model_acl(journal)
    for path, original in before_model.items():
        assert path.stat().st_mode == original.st_mode
        with pytest.raises(OSError):
            os.getxattr(path, 'system.posix_acl_access')
    assert (root / 'home/bianbu').stat().st_mode & 0o777 == 0o750


def test_upgrade_db_registered_external_directory_is_read_only_and_bounded(external_registered_models):
    import os
    import sqlite3
    root, _, _, databases, sibling = external_registered_models
    model_dir = root / 'home/bianbu/private-model'
    model = write_file(model_dir / 'weights/model.bin', 'directory model')
    write_file(model_dir / 'config.json', '{}')
    original_modes = {path: path.stat().st_mode for path in (model_dir, model.parent, model)}
    with sqlite3.connect(databases['llm']) as connection:
        connection.execute('UPDATE models SET local_path=?', ('/home/bianbu/private-model',))
    original_db = databases['llm'].read_bytes()
    runtime.migrate_legacy(root=root)
    assert acl_entries(model_dir)[2, 60001] == 5
    assert acl_entries(model.parent)[2, 60001] == 5
    assert acl_entries(model)[2, 60001] == 4
    with pytest.raises(OSError):
        os.getxattr(model_dir, 'system.posix_acl_default')
    with pytest.raises(OSError):
        os.getxattr(sibling, 'system.posix_acl_access')
    assert databases['llm'].read_bytes() == original_db
    assert model.read_text() == 'directory model'
    restore_external_model_acl(root / 'etc/spacemit-ai-gateway/legacy-storage-acl.jsonl')
    for path, mode in original_modes.items():
        assert path.stat().st_mode == mode
        with pytest.raises(OSError):
            os.getxattr(path, 'system.posix_acl_access')


@pytest.mark.parametrize('bad_path', ['/home/bianbu/missing.gguf', 'relative.gguf', '/home/bianbu'])
def test_upgrade_unconfirmed_external_reference_does_not_publish(external_registered_models, bad_path):
    import sqlite3
    root, etc, _, databases, _ = external_registered_models
    source = root / 'opt/spacemit-ai-gateway/configs/base.yaml'
    original = source.read_bytes()
    with sqlite3.connect(databases['llm']) as connection:
        connection.execute('UPDATE models SET local_path=?', (bad_path,))
    before_db = databases['llm'].read_bytes()
    with pytest.raises(RuntimeError):
        runtime.migrate_legacy(root=root)
    assert source.read_bytes() == original
    assert databases['llm'].read_bytes() == before_db
    assert not (etc / '.legacy-storage-migrated').exists()
    assert not (etc / 'credentials.env').exists()


def test_upgrade_reads_committed_wal_model_reference_without_mutating_db(external_registered_models):
    import sqlite3
    root, _, paths, databases, _ = external_registered_models
    new_path = '/home/bianbu/private/new-model.gguf'
    model = write_file(root / new_path.lstrip('/'), 'WAL registered model')
    database = databases['llm']
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('UPDATE models SET local_path=?', (new_path,))
        connection.commit()
        wal = Path(str(database) + '-wal')
        before = {path: path.read_bytes() for path in (database, wal)}
        runtime.migrate_legacy(root=root)
        assert acl_entries(model)[2, 60001] == 4
        assert all(path.read_bytes() == contents for path, contents in before.items())
        assert connection.execute('SELECT local_path, api_key FROM models').fetchone() == (new_path, 'unchanged-private-key')
    old_model = root / paths['llm'].lstrip('/')
    with pytest.raises(OSError):
        __import__('os').getxattr(old_model, 'system.posix_acl_access')


@pytest.mark.parametrize('shape', ['file', 'ancestor', 'tree', 'escape', 'fifo'])
def test_external_model_unsafe_path_fails_before_any_acl(external_registered_models, shape):
    import os
    import sqlite3
    root, etc, paths, databases, sibling = external_registered_models
    original_home_mode = (root / 'home/bianbu').stat().st_mode
    target = root / paths['llm'].lstrip('/')
    if shape == 'file':
        target.unlink()
        target.symlink_to(sibling)
    elif shape == 'ancestor':
        directory = target.parent
        directory.rename(directory.with_name('real-llm'))
        directory.symlink_to(directory.with_name('real-llm'), target_is_directory=True)
    elif shape == 'tree':
        target = root / 'home/bianbu/private-model'
        write_file(target / 'valid.bin', 'unchanged')
        (target / 'link.bin').symlink_to(sibling)
    elif shape == 'escape':
        target = root / 'home/bianbu/private-model'
        write_file(target / 'weights.bin', 'valid model behind traversal')
    else:
        target = root / 'home/bianbu/private.pipe'
        os.mkfifo(target)
    value = '/home/bianbu/../bianbu/private-model' if shape == 'escape' else '/' + str(target.relative_to(root))
    with sqlite3.connect(databases['llm']) as connection:
        connection.execute('UPDATE models SET local_path=?', (value,))
    before_db = databases['llm'].read_bytes()
    with pytest.raises(RuntimeError):
        runtime.migrate_legacy(root=root)
    assert databases['llm'].read_bytes() == before_db
    assert (root / 'home/bianbu').stat().st_mode == original_home_mode
    assert not (etc / '.legacy-storage-migrated').exists()
    assert not (etc / 'legacy-storage-acl.jsonl').exists()
    assert not (etc / 'credentials.env').exists()


@pytest.mark.parametrize('override', ['environment', 'credentials.env', 'context', 'yaml'])
def test_external_models_only_use_effective_database(external_registered_models, override):
    import sqlite3
    root, etc, _, databases, _ = external_registered_models
    model = write_file(root / 'home/bianbu/selected-model.gguf', 'selected')
    selected_db = root / 'root/.cache/selected/db.sqlite'
    selected_db.parent.mkdir(parents=True)
    with sqlite3.connect(selected_db) as connection:
        connection.execute('CREATE TABLE models (source_type TEXT, local_path TEXT)')
        connection.execute('INSERT INTO models VALUES (?, ?)', ('local_path', '/home/bianbu/selected-model.gguf'))
    # 被覆盖的旧 DB 即使有失效路径也不应被迁移读取。
    with sqlite3.connect(databases['llm']) as connection:
        connection.execute('UPDATE models SET local_path=?', ('/home/bianbu/missing.gguf',))
    value = '/root/.cache/selected/db.sqlite'
    key = 'SPACEMIT_AI_GATEWAY_LLM__STORAGE__DB_PATH'
    if override == 'context':
        write_file(etc / 'legacy-context.json', json.dumps({'home': '/root', 'environment': {key: value}}))
    elif override == 'yaml':
        config = yaml.safe_load((root / 'opt/spacemit-ai-gateway/configs/base.yaml').read_text())
        config['llm']['storage']['db_path'] = value
        config['app']['port'] = 19000
        write_file(etc / 'base.yaml', yaml.safe_dump(config))
    else:
        write_file(etc / override, runtime.env_line(key, value))
    runtime.migrate_legacy(root=root)
    assert acl_entries(model)[2, 60001] == 4


@pytest.mark.parametrize('shape', ['corrupt', 'symlink', 'schema'])
def test_unreadable_registered_database_preserves_upgrade_source(external_registered_models, shape):
    import sqlite3
    root, etc, _, databases, _ = external_registered_models
    database = databases['llm']
    if shape == 'corrupt':
        database.write_bytes(b'not a sqlite database')
    elif shape == 'symlink':
        original = database.with_name('real.sqlite')
        database.rename(original)
        database.symlink_to(original)
    else:
        with sqlite3.connect(database) as connection:
            connection.execute('DROP TABLE models')
            connection.execute('CREATE TABLE models (id TEXT)')
    before = database.read_bytes()
    with pytest.raises(RuntimeError):
        runtime.migrate_legacy(root=root)
    assert database.read_bytes() == before
    assert not (etc / '.legacy-storage-migrated').exists()
    assert not (etc / 'credentials.env').exists()


@pytest.mark.parametrize('omitted', ['domain', 'storage', 'db_path'])
@pytest.mark.parametrize('admin_yaml', [False, True])
def test_upgrade_materializes_omitted_storage_defaults_and_scans_db(external_registered_models, monkeypatch, omitted, admin_yaml):
    import os
    from spacemit_ai_gateway.app.settings import Settings
    root, etc, paths, _, _ = external_registered_models
    source = root / 'opt/spacemit-ai-gateway/configs/base.yaml'
    config = yaml.safe_load(source.read_text())
    if omitted == 'domain':
        del config['llm']
    elif omitted == 'storage':
        del config['llm']['storage']
        config['llm']['default_args'] = ['--ctx-size', '2048']
    else:
        del config['llm']['storage']['db_path']
        config['llm']['storage']['models_dir'] = '/srv/custom-models/llm'
    config['app']['port'] = 19001
    config['embed']['models'] = [{'id': 'administrator-model', 'source_type': 'remote'}]
    selected = etc / 'base.yaml' if admin_yaml else source
    write_file(selected, yaml.safe_dump(config))
    original = selected.read_bytes()
    for key in list(os.environ):
        if key.startswith('SPACEMIT_AI_GATEWAY_'):
            monkeypatch.delenv(key)
    runtime.migrate_legacy(root=root)
    for name in ('credentials.env', 'environment'):
        for key, value in runtime.environment_values(etc / name).items():
            monkeypatch.setenv(key, value)
    effective = Settings(**yaml.safe_load((etc / 'base.yaml').read_text()))
    for domain in ('llm', 'embed', 'rerank', 'vlm'):
        storage = getattr(effective, domain).storage
        assert storage.db_path == f'/root/.cache/spacemit-ai-gateway/{domain}/db.sqlite'
        assert storage.base_dir == f'/root/.cache/spacemit-ai-gateway/{domain}'
        expected = '/srv/custom-models/llm' if domain == 'llm' and omitted == 'db_path' else f'/root/.cache/models/{domain}'
        assert storage.models_dir == expected
    assert effective.app.port == 19001
    assert effective.embed.models == config['embed']['models']
    if omitted == 'storage':
        assert effective.llm.default_args == ['--ctx-size', '2048']
    assert acl_entries(root / paths['llm'].lstrip('/'))[2, 60001] == 4
    if admin_yaml:
        assert selected.read_bytes() == original


@pytest.mark.parametrize('home', ['/root', '/home/operator'])
@pytest.mark.parametrize('storage_done', [False, True])
def test_upgrade_preserves_native_default_home_and_cache(intent_upgrade, monkeypatch, home, storage_done):
    from spacemit_ai_gateway.app.settings import AsrConfig, TtsConfig, VadConfig
    from spacemit_ai_gateway.domains.asr.adapters.sensevoice import model_assets as asr_assets
    from spacemit_ai_gateway.domains.tts.adapters.matcha import model_assets as tts_assets
    from spacemit_ai_gateway.domains.vad.adapters.silero import model_assets as vad_assets
    from spacemit_ai_gateway.domains.vision.models import KNOWN_MODELS, _cache_root

    root, etc, config, grants = intent_upgrade
    config.update({name: {'model_dir': None} for name in ('asr', 'tts', 'vad')})
    write_file(root / 'opt/spacemit-ai-gateway/configs/base.yaml', yaml.safe_dump(config))
    write_file(etc / 'legacy-context.json', json.dumps({
        'home': home, 'environment': {'XDG_CACHE_HOME': '/srv/operator-cache'},
    }))
    if storage_done:
        (etc / '.legacy-storage-migrated').touch()
    runtime.migrate_legacy(root=root)
    environment = runtime.environment_values(etc / 'credentials.env')
    assert environment['HOME'] == home
    assert environment['XDG_CACHE_HOME'] == '/srv/operator-cache'
    monkeypatch.setenv('HOME', environment['HOME'])
    monkeypatch.setenv('XDG_CACHE_HOME', environment['XDG_CACHE_HOME'])
    for asset in (asr_assets(AsrConfig()), tts_assets(TtsConfig().backend, TtsConfig()), vad_assets(VadConfig())):
        for artifact in asset.artifacts:
            assert artifact.path.is_relative_to(Path(home))
            assert any(writable and artifact.path.is_relative_to(Path(directory))
                       for directory, writable in grants)
    for model in KNOWN_MODELS.values():
        for artifact in model['models']:
            destination = Path(artifact['dest']).expanduser()
            assert any(writable and destination.is_relative_to(Path(directory))
                       for directory, writable in grants)
    assert (str(_cache_root() / 'vision/configs'), True) in grants
    assert (f'{home}/.cache/spacemit-ai-gateway', True) in grants
    assert (home, True) not in grants
    assert ('/srv/operator-cache', True) not in grants
    assert (etc / '.legacy-assets-migrated').is_file()
    before = (etc / 'credentials.env').read_bytes()
    runtime.migrate_legacy(root=root)
    assert (etc / 'credentials.env').read_bytes() == before


def test_preinst_preserves_effective_xdg_cache_without_other_secrets(tmp_path, monkeypatch):
    etc = tmp_path / 'etc/spacemit-ai-gateway'
    etc.mkdir(parents=True)
    shown = 'User=root\nMainPID=123\nEnvironment=HOME=/root XDG_CACHE_HOME=/wrong\n'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, shown, ''))
    write_file(tmp_path / 'proc/123/environ',
               'HOME=/root\0XDG_CACHE_HOME=/srv/operator-cache\0OTHER_SECRET=private\0')
    preinst_snapshot()(root=tmp_path, proc=tmp_path / 'proc')
    context = json.loads((etc / 'legacy-context.json').read_text())
    assert context['environment'] == {'XDG_CACHE_HOME': '/srv/operator-cache'}


@pytest.mark.parametrize('source_kind', ['legacy', 'admin', 'environment'])
def test_upgrade_materializes_native_model_directories(intent_upgrade, source_kind):
    root, etc, config, grants = intent_upgrade
    config.update({name: {'model_dir': 'models/' + name} for name in ('asr', 'tts', 'vad')})
    source = etc / 'base.yaml' if source_kind == 'admin' else root / 'opt/spacemit-ai-gateway/configs/base.yaml'
    write_file(source, yaml.safe_dump(config))
    original = source.read_bytes()
    write_file(etc / 'legacy-context.json', json.dumps({
        'home': '/home/operator', 'working_directory': '/srv/old-gateway', 'environment': {},
    }))
    if source_kind == 'environment':
        write_file(etc / 'environment', ''.join(
            f'SPACEMIT_AI_GATEWAY_{name.upper()}__MODEL_DIR="~/private models/{name}"\n'
            for name in ('asr', 'tts', 'vad')))
    runtime.migrate_legacy(root=root)
    migrated = yaml.safe_load((etc / 'base.yaml').read_text())
    environment = {**runtime.environment_values(etc / 'credentials.env'),
                   **runtime.environment_values(etc / 'environment')}
    for name in ('asr', 'tts', 'vad'):
        expected = (f'/home/operator/private models/{name}' if source_kind == 'environment'
                    else f'/srv/old-gateway/models/{name}')
        effective = environment.get(f'SPACEMIT_AI_GATEWAY_{name.upper()}__MODEL_DIR', migrated[name]['model_dir'])
        assert effective == expected
        assert (expected, True) in grants
    if source_kind == 'admin':
        assert source.read_bytes() == original


def test_native_default_acl_keeps_private_siblings_and_rolls_back(intent_upgrade, monkeypatch):
    import os
    root, etc, _, _ = intent_upgrade
    real_acl = load_script('foundation_native_acl', 'debian/runtime.py').grant_storage_access
    monkeypatch.setattr(runtime, 'grant_storage_access', lambda root, paths: real_acl(root, paths, uid=60001))
    models = [write_file(root / path, 'existing native model') for path in (
        'root/.cache/models/asr/sensevoice/model.onnx',
        'root/.cache/models/tts/matcha-tts/model.onnx',
        'root/.cache/models/tts/kokoro-tts/voices/voice.bin',
        'root/.cache/models/vad/silero/silero_vad.onnx',
        'root/.cache/models/vision/yolov8/model.onnx',
        'root/.cache/spacemit-ai-gateway/errors.sqlite',
    )]
    siblings = [write_file(root / path, 'unrelated secret') for path in (
        'root/.cache/unrelated/private.key',
        'root/.cache/models/asr/another-model/private.key',
        'root/.cache/models/tts/another-model/private.key',
    )]
    for path in models + siblings:
        path.chmod(0o600)
    home = root / 'root'
    domains = [root / 'root/.cache/models' / domain for domain in ('asr', 'tts')]
    for directory in [home] + domains:
        directory.chmod(0o700)
    before = {path: path.stat() for path in models + [home] + domains}
    runtime.migrate_legacy(root=root)
    for path in models:
        assert acl_entries(path)[2, 60001] == 6
        assert (path.stat().st_ino, path.stat().st_uid) == (before[path].st_ino, before[path].st_uid)
        assert path.read_text() == 'existing native model'
    assert acl_entries(home)[2, 60001] == 1
    for sibling in siblings:
        with pytest.raises(OSError):
            os.getxattr(sibling, 'system.posix_acl_access')
    for domain in ('asr', 'tts'):
        assert acl_entries(root / 'root/.cache/models' / domain)[2, 60001] == 1
    restore_external_model_acl(etc / 'legacy-storage-acl.jsonl')
    for path, previous in before.items():
        assert path.stat().st_mode == previous.st_mode
        with pytest.raises(OSError):
            os.getxattr(path, 'system.posix_acl_access')


@pytest.mark.parametrize('location', ['context', 'credentials.env', 'environment'])
@pytest.mark.parametrize('value,expected', [('~/cache', '/home/operator/cache'),
                                            ('cache', '/srv/old-gateway/cache'),
                                            ('', '/srv/old-gateway')])
def test_upgrade_materializes_xdg_cache_with_environment_precedence(intent_upgrade, location, value, expected):
    root, etc, _, grants = intent_upgrade
    context = {'home': '/home/operator', 'working_directory': '/srv/old-gateway', 'environment': {}}
    if location == 'context':
        context['environment']['XDG_CACHE_HOME'] = value
    else:
        context['environment']['XDG_CACHE_HOME'] = '/srv/unused-cache'
        write_file(etc / location, runtime.env_line('XDG_CACHE_HOME', value))
    write_file(etc / 'legacy-context.json', json.dumps(context))
    runtime.migrate_legacy(root=root)
    effective = {**runtime.environment_values(etc / 'credentials.env'),
                 **runtime.environment_values(etc / 'environment')}
    assert effective['XDG_CACHE_HOME'] == expected
    assert (expected + '/spacemit-ai-gateway/vision/configs', True) in grants
    assert (expected, True) not in grants


@pytest.mark.parametrize('address,port', [('::', 8326), ('0.0.0.0', 8326), ('127.0.0.1', 18326)])
def test_upgrade_preserves_original_frontend_listener(legacy_tree, monkeypatch, address, port):
    root, etc, _ = legacy_tree
    write_file(root / 'usr/lib/systemd/system/spacemit-ai-gateway.service',
               '[Service]\nUser=root\nWorkingDirectory=/opt/spacemit-ai-gateway\n')
    command = f'/usr/bin/python3 -m http.server {port} --bind {address}'
    write_file(root / 'usr/lib/systemd/system/spacemit-ai-gateway-frontend.service',
               '[Service]\nUser=root\nExecStart=' + command + '\n')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, '', 'offline'))
    preinst_snapshot()(root=root)
    context = json.loads((etc / 'legacy-context.json').read_text())
    assert context['frontend_exec_start'] == command
    runtime.migrate_legacy(root=root)
    dropin = root / 'etc/systemd/system/spacemit-ai-gateway-frontend.service.d/legacy-listener.conf'
    assert dropin.read_text() == '[Service]\nExecStart=\nExecStart=' + command + '\n'
    # 重复 configure 不能覆盖管理员之后调整的监听配置。
    dropin.write_text('[Service]\nExecStart=\nExecStart=/usr/bin/python3 -m http.server 18327 --bind 127.0.0.1\n')
    changed = dropin.read_bytes()
    runtime.migrate_legacy(root=root)
    assert dropin.read_bytes() == changed


@pytest.mark.parametrize('scope', ['spacemit-ai-gateway-frontend.service.d', 'spacemit-ai-gateway-.service.d'])
def test_upgrade_frontend_listener_uses_effective_dropin(legacy_tree, monkeypatch, scope):
    root, etc, _ = legacy_tree
    write_file(root / 'usr/lib/systemd/system/spacemit-ai-gateway.service', '[Service]\nUser=root\n')
    write_file(root / 'usr/lib/systemd/system/spacemit-ai-gateway-frontend.service',
               '[Service]\nExecStart=/usr/bin/python3 -m http.server 8326 --bind ::\n')
    command = '/usr/bin/python3 -m http.server 18326 --bind 192.0.2.10'
    write_file(root / 'etc/systemd/system' / scope / 'zz-admin.conf',
               '[Service]\nExecStart=\nExecStart=' + command + '\n')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, '', 'offline'))
    preinst_snapshot()(root=root)
    assert json.loads((etc / 'legacy-context.json').read_text())['frontend_exec_start'] == command


@pytest.mark.parametrize('command', ['', 123, '/usr/bin/python3\nUser=root', '/bin/false\rRestart=always', '/bin/false\0'])
def test_upgrade_rejects_invalid_frontend_command(legacy_tree, command):
    root, etc, _ = legacy_tree
    write_file(etc / 'legacy-context.json', json.dumps({
        'home': '/root', 'environment': {}, 'frontend_exec_start': command,
    }))
    with pytest.raises(RuntimeError, match='Invalid legacy frontend start command'):
        runtime.migrate_legacy(root=root)
    assert not (root / 'etc/systemd/system/spacemit-ai-gateway-frontend.service.d/legacy-listener.conf').exists()


def test_fresh_install_keeps_default_frontend_listener(tmp_path):
    runtime.migrate_legacy(root=tmp_path)
    assert not (tmp_path / 'etc/systemd/system/spacemit-ai-gateway-frontend.service.d/legacy-listener.conf').exists()


def test_upgrade_keeps_masked_frontend_without_migration(legacy_tree, monkeypatch):
    root, etc, _ = legacy_tree
    write_file(root / 'usr/lib/systemd/system/spacemit-ai-gateway.service', '[Service]\nUser=root\n')
    unit = root / 'etc/systemd/system/spacemit-ai-gateway-frontend.service'
    unit.parent.mkdir(parents=True)
    unit.symlink_to('/dev/null')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, '', 'offline'))
    preinst_snapshot()(root=root)
    assert 'frontend_exec_start' not in json.loads((etc / 'legacy-context.json').read_text())
    runtime.migrate_legacy(root=root)
    assert unit.is_symlink() and unit.readlink() == Path('/dev/null')
    assert not (unit.parent / (unit.name + '.d') / 'legacy-listener.conf').exists()
