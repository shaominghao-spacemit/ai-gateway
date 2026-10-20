#!/usr/bin/env python3
"""包拥有的离线环境与迁移文件；不删除管理员文件或模型。"""
import base64
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import sqlite3
import stat
import struct
import pwd
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import venv

APP = Path("/opt/spacemit-ai-gateway")
MARKER = ".gateway-runtime"
VOICE_KEY = Path("/etc/spacemit-ai-gateway/voice-api-key")
VOICE_KEY_TMPFILES = Path("/etc/tmpfiles.d/spacemit-ai-gateway-voice-access.conf")
VOICE_KEY_GROUP = "bibit-asr"


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
        # 升级→回滚→再升级会看到上一轮留下的备份；当前 venv 才是最新一代，
        # 替换旧备份而不是让安装永久失败。
        if backup.is_symlink() or (backup.exists() and not backup.is_dir()):
            backup.unlink()
        elif backup.is_dir():
            shutil.rmtree(backup)
        link.rename(backup)
    try:
        os.replace(pending, link)
    except OSError:
        if backup is not None:
            backup.rename(link)
        raise


def private_write(path, text):
    """原子发布迁移状态，临时文件与恢复副本均只允许 root 读取。"""
    fd, temporary = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _valid_voice_key(path, gid):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != gid
            or stat.S_IMODE(info.st_mode) != 0o640):
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        data = os.read(fd, 4097)
    finally:
        os.close(fd)
    if data.endswith(b"\n"):
        data = data[:-1]
    return 1 <= len(data) <= 4096 and all(0x21 <= byte <= 0x7e for byte in data)


def ensure_voice_key(path=VOICE_KEY, group=VOICE_KEY_GROUP):
    """发布 Voice 专用 Key；合法旧 Key 保留，损坏或符号链接被原子替换。"""
    gid = grp.getgrnam(group).gr_gid
    if _valid_voice_key(path, gid):
        return False
    if path.exists() and not (path.is_symlink() or path.is_file()):
        raise RuntimeError(f"Refusing to replace non-file Voice key path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".voice-api-key-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o640)
            os.fchown(stream.fileno(), 0, gid)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def grant_voice_key_access(path=VOICE_KEY, tmpfiles=VOICE_KEY_TMPFILES, group=VOICE_KEY_GROUP):
    """给当前 Voice 服务用户即时 ACL，并生成重启后恢复规则。"""
    if not path.is_file() or path.is_symlink():
        return
    entry = grp.getgrnam(group)
    users = set(entry.gr_mem)
    users.update(item.pw_name for item in pwd.getpwall() if item.pw_gid == entry.gr_gid)
    uids = sorted({item.pw_uid for item in (pwd.getpwnam(name) for name in users) if item.pw_uid != 0})
    for uid in uids:
        subprocess.run(["setfacl", "-m", f"u:{uid}:r--", str(path)], check=True)
    tmpfiles.parent.mkdir(parents=True, exist_ok=True)
    text = "# Generated by spacemit-ai-gateway.postinst; do not edit.\n"
    text += "".join(f"a+ {path} - - - - u:{uid}:r--\n" for uid in uids)
    private_write(tmpfiles, text)
    tmpfiles.chmod(0o644)


def preserve(source, destination):
    if not destination.exists():
        private_write(destination, source.read_text())


def parse_environment(text):
    # preinst 在 unpack 前不能导入此文件；两份解析器由回归检查一致性。
    if any(char in '\0\ufeff' or 0xfdd0 <= ord(char) <= 0xfdef
           or ord(char) & 0xffff in (0xfffe, 0xffff) for char in text):
        raise ValueError('Invalid character in systemd EnvironmentFile')
    result = {}
    index = 0
    while index < len(text):
        newline = re.search(r'[\r\n]', text[index:])
        line_end = index + newline.start() if newline else len(text)
        assignment = re.match(r'[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=', text[index:line_end])
        if assignment is None:
            index = line_end + 1
            continue
        key = assignment[1]
        index += assignment.end()
        state = 'start'
        value = []
        end = 0
        while index < len(text):
            char = text[index]
            index += 1
            if state == 'start':
                if char in ' \t':
                    continue
                if char in '\r\n':
                    break
                if char in "'\"":
                    state = char
                    continue
                state = 'plain'
            if state == 'plain':
                if char in '\r\n':
                    break
                if char == '\\':
                    if index < len(text):
                        following = text[index]
                        index += 1
                        if following not in '\r\n':
                            value.append(following)
                        end = len(value)
                    continue
                value.append(char)
                if char not in ' \t\r':
                    end = len(value)
            else:
                if char == state:
                    state = 'start'
                    continue
                if state == '"' and char == '\\':
                    if index == len(text):
                        continue
                    char = text[index]
                    index += 1
                    if char == '\n':
                        continue
                    if char not in '\\"`$':
                        value.append('\\')
                value.append(char)
                end = len(value)
        result[key] = ''.join(value[:end])
    return result


def environment_values(path):
    # 直接解码，保留多行引号里的 CR；不展开变量或执行命令。
    return parse_environment(path.read_bytes().decode('utf-8')) if path.is_file() else {}


def env_line(key, value):
    # systemd EnvironmentFile 的双引号转义规则；不使用 shell 的拼接引号规则。
    value = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{value}"\n'


def storage_field(key):
    parts = key.removeprefix("SPACEMIT_AI_GATEWAY_").lower().split("__")
    return len(parts) == 3 and parts[1] == "storage" and parts[2] in {"base_dir", "models_dir", "db_path"}


def model_directory_field(key):
    parts = key.removeprefix("SPACEMIT_AI_GATEWAY_").lower().split("__")
    return len(parts) == 2 and parts[0] in {"asr", "tts", "vad"} and parts[1] == "model_dir"


def legacy_default_assets(home, cache_home):
    from spacemit_ai_gateway.domains.asr.adapters.sensevoice import _DEFAULT_MODEL_DIR as asr_dir
    from spacemit_ai_gateway.domains.tts.adapters.matcha import _DEFAULT_MODEL_DIR as matcha_dir
    from spacemit_ai_gateway.domains.tts.adapters.kokoro import _KOKORO_DEFAULT_DIR as kokoro_dir
    from spacemit_ai_gateway.domains.vad.adapters.silero import _SILERO_DEFAULT_DIR as vad_dir
    from spacemit_ai_gateway.domains.vision.models import KNOWN_MODELS

    paths = [(materialize(value, home), True) for value in (asr_dir, matcha_dir, kokoro_dir, vad_dir)]
    paths.extend((str(Path(materialize(artifact["dest"], home)).parent), True)
                 for model in KNOWN_MODELS.values() for artifact in model["models"])
    paths.extend([(materialize("~/.cache/spacemit-ai-gateway", home), True),
                  (str(Path(materialize(cache_home, home)) / "spacemit-ai-gateway/vision/configs"), True)])
    return paths


def materialize(value, home, working_directory="/opt/spacemit-ai-gateway"):
    if value == "~" or value.startswith("~/"):
        return home + value[1:]
    if value.startswith("~"):
        return str(Path(value).expanduser())
    if not Path(value).is_absolute():
        return str(Path(working_directory) / value)
    return value


def _acl_grant(path, uid, permissions, default, journal):
    """Linux POSIX ACL；只增加服务 UID 权限，不激活其他主体被 mask 屏蔽的权限。"""
    import errno
    attribute = "system.posix_acl_default" if default else "system.posix_acl_access"
    try:
        original = os.getxattr(path, attribute)
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise RuntimeError(f"Cannot read storage ACL: {path}: {exc}") from exc
        original = None
    mode = path.stat().st_mode
    if not default and permissions == 1 and mode & 1:
        return  # 所有人已有穿越权限的公共父目录不需要新增 ACL。
    if original:
        version, = struct.unpack_from("<I", original)
        if version != 2:
            raise RuntimeError(f"Unsupported POSIX ACL: {path}")
        entries = list(struct.iter_unpack("<HHI", original[4:]))
    else:
        group_bits = (mode >> 3) & 7
        if default:
            # stat 的 group bits 是 access mask，不能当成 owning group 的有效权限。
            # 否则给 UID 增加 rwx 后会意外把 rwx 也默认继承给原 owning group。
            try:
                access = list(struct.iter_unpack("<HHI", os.getxattr(path, "system.posix_acl_access")[4:]))
                group_bits = next(bits for tag, bits, _ in access if tag == 4)
                group_bits &= next((bits for tag, bits, _ in access if tag == 16), 7)
            except OSError as exc:
                if exc.errno != errno.ENODATA:
                    raise
            # 新文件遵守服务 0027 的最小权限；不引入其他用户或 group 写权限。
            entries = [(1, 7, 0xffffffff), (4, group_bits & 5, 0xffffffff), (32, 0, 0xffffffff)]
        else:
            entries = [(1, (mode >> 6) & 7, 0xffffffff),
                       (4, group_bits, 0xffffffff), (32, mode & 7, 0xffffffff)]
    mask = next((bits for tag, bits, _ in entries if tag == 16), 7)
    # 新 mask 扩大时，先固化其他 group-class 条目的原有效权限。
    entries = [(tag, bits & mask if tag in (2, 4, 8) else bits, who)
               for tag, bits, who in entries if tag != 16]
    old = next((bits for tag, bits, who in entries if tag == 2 and who == uid), 0)
    entries = [entry for entry in entries if not (entry[0] == 2 and entry[2] == uid)]
    entries.append((2, old | permissions, uid))
    new_mask = 0
    for tag, bits, _ in entries:
        if tag in (2, 4, 8):
            new_mask |= bits
    entries.append((16, new_mask, 0xffffffff))
    entries.sort(key=lambda entry: (entry[0], entry[2]))
    updated = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    if updated == original:
        return
    # 先持久化恢复记录，再修改 ACL；中断重入不会丢失最初状态。
    record = {"path": str(path), "attribute": attribute, "mode": stat.S_IMODE(mode),
              "acl": base64.b64encode(original).decode() if original is not None else None}
    identity = (str(path), attribute)
    if identity not in journal[1]:
        with journal[0].open("a") as stream:
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        journal[1].add(identity)
    try:
        os.setxattr(path, attribute, updated)
    except OSError as exc:
        raise RuntimeError(f"Cannot grant gateway storage ACL: {path}: {exc}") from exc


def checked_storage_path(root, value):
    """拒绝路径逃逸及任意级 symlink；不把不明确的引用解析到其他授权树。"""
    value = Path(value)
    if not value.is_absolute() or ".." in value.parts:
        raise RuntimeError(f"Storage must be an absolute path without traversal: {value}")
    path = root
    for part in value.parts[1:]:
        path = path / part
        if path.is_symlink():
            raise RuntimeError(f"Storage contains a symlink; configure its real storage path: {path}")
    return path


def registered_model_paths(root, databases):
    """只读取有效旧 DB 的本地引用；mode=ro 仍读取已提交 WAL，绝不改注册记录。"""
    paths = []
    for value in sorted(set(databases)):
        database = checked_storage_path(root, value)
        if not database.exists():
            continue  # 尚未初始化的服务；不创建 SQLite 文件。
        if not database.is_file():
            raise RuntimeError(f"Model database is not a regular file: {database}")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(database) + suffix)
            if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
                raise RuntimeError(f"Unsafe SQLite sidecar: {sidecar}")
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                connection.execute("PRAGMA query_only=ON")
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='models'").fetchone():
                    continue
                for source_type, local_path in connection.execute("SELECT source_type, local_path FROM models"):
                    if source_type == "remote":
                        continue
                    if not local_path and source_type != "local_path":
                        continue  # 尚未下载的 local_url 没有可复用文件。
                    if not isinstance(local_path, str) or not local_path:
                        raise RuntimeError(f"Unconfirmed registered local model path in {database}")
                    paths.append((local_path, False))
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Cannot read legacy model database: {database}") from exc
    return paths


def validate_storage_paths(root, paths):
    """先验证完整授权集合，再修改 ACL；无效引用不能留下半途递归授权。"""
    checked = []
    for value, writable in sorted(set(paths)):
        path = checked_storage_path(root, value)
        parts = Path(value).parts[1:]
        minimum = 4 if writable and parts and parts[0] == "home" else 3
        if len(parts) < minimum or parts[0] in {"dev", "proc", "sys", "run"}:
            raise RuntimeError(f"Refusing broad storage ACL: {value}")
        if not writable and not path.exists():
            raise RuntimeError(f"Registered local model does not exist: {value}")
        if path.exists() and not (path.is_dir() or (not writable and path.is_file())):
            raise RuntimeError(f"Storage has an unsupported file type: {value}")
        # 验证目录全树，不允许借内部 symlink 越出已确认模型目录。
        def walk_error(exc):
            raise RuntimeError(f"Cannot inspect model storage: {path}") from exc
        tree = os.walk(path, followlinks=False, onerror=walk_error) if path.is_dir() else ()
        for directory, names, files in tree:
            for name in names + files:
                child = Path(directory) / name
                if child.is_symlink() or not (child.is_file() or child.is_dir()):
                    raise RuntimeError(f"Storage contains a symlink or non-regular file: {child}")
        checked.append((path, writable))
    return checked


def grant_storage_access(root, paths, uid=None, traverse=()):
    """模型/状态目录原地复用；父目录仅 x，存储目录允许 SQLite WAL/下载更新。"""
    if not paths and not traverse:
        return
    checked = validate_storage_paths(root, paths)
    # 兼容配置软链的源目录只授予遍历，不递归授予目录或兄弟文件的读取权限。
    traversal = [checked_storage_path(root, value) for value in traverse]
    for directory in traversal:
        if not directory.is_dir():
            raise RuntimeError(f"Traversal ACL requires an existing directory: {directory}")
    uid = pwd.getpwnam("spacemit-ai-gateway").pw_uid if uid is None else uid
    if uid == 0:
        raise RuntimeError("Gateway storage ACL requires a non-root service account")
    log = root / "etc/spacemit-ai-gateway/legacy-storage-acl.jsonl"
    if not log.exists():
        private_write(log, "")
    history = log.read_text()
    if history and not history.endswith("\n"):
        # 上次在追加原 ACL、实际修改前中断，残缺尾行可丢弃；已完成记录不可丢。
        history = history[:history.rfind("\n") + 1]
        private_write(log, history)
    journal = (log, {(entry["path"], entry["attribute"])
                     for entry in map(json.loads, history.splitlines())})
    for directory in traversal:
        for parent in [*reversed(directory.parents), directory]:
            if parent.is_relative_to(root):
                _acl_grant(parent, uid, 1, False, journal)
    for resolved, writable in checked:
        for parent in reversed(resolved.parents):
            if parent == root.parent or not parent.is_relative_to(root):
                continue
            if not parent.exists():
                parent.mkdir(mode=0o750)
            _acl_grant(parent, uid, 1, False, journal)
        if resolved.is_file():
            _acl_grant(resolved, uid, 4, False, journal)
            continue
        # 不复制或 chown 原模型；仅可写缓存目录创建/继承默认 ACL。
        if writable:
            resolved.mkdir(parents=True, exist_ok=True)
        for directory, names, files in os.walk(resolved, followlinks=False):
            directory = Path(directory)
            _acl_grant(directory, uid, 7 if writable else 5, False, journal)
            if writable:
                _acl_grant(directory, uid, 7, True, journal)
            for name in names + files:
                child = directory / name
                if child.is_symlink():
                    raise RuntimeError(f"Storage contains a symlink; configure its real storage path: {child}")
            for name in files:
                child = directory / name
                if not child.is_file():
                    raise RuntimeError(f"Storage contains a non-regular file: {child}")
                _acl_grant(child, uid, 6 if writable else 4, False, journal)


def migrate_legacy(root=Path("/")):
    etc = root / "etc/spacemit-ai-gateway"
    etc.mkdir(parents=True, exist_ok=True)
    legacy = root / "opt/spacemit-ai-gateway/configs/base.yaml"
    marker = etc / ".legacy-migrated"
    storage_marker = etc / ".legacy-storage-migrated"
    assets_marker = etc / ".legacy-assets-migrated"
    backup = etc / "legacy-base.yaml"
    source = legacy if legacy.is_file() else backup
    effective_backup = etc / "legacy-effective-base.yaml"
    if effective_backup.is_file():
        source = effective_backup
    legacy_install = source.is_file()
    credentials = etc / "credentials.env"
    pattern = re.compile(r"\[Service\]\nUser=([^\n]+)\nGroup=[^\n]+\nEnvironment=HOME=([^\n]+)\nUMask=0022\n")
    context_path = etc / "legacy-context.json"
    context = json.loads(context_path.read_text()) if context_path.exists() else {"home": "/root", "environment": {}}
    administrator_unit = root / "etc/systemd/system/spacemit-ai-gateway.service"
    user = context.get("user") or "root"
    administrator_uid = (int(user) if user.isdigit() else pwd.getpwnam(user).pw_uid) if administrator_unit.is_file() else 0
    administrator_storage_marker = etc / f".legacy-admin-storage-{administrator_uid}"
    dropin = root / "etc/systemd/system/spacemit-ai-gateway.service.d/runtime-user.conf"
    old_dropin = dropin if dropin.is_file() else etc / "spacemit-ai-gateway-legacy-runtime-user.conf"
    if not context_path.exists() and old_dropin.is_file():
        match = pattern.fullmatch(old_dropin.read_text())
        if match:
            context["home"] = match[2]
    frontend_command = context.get("frontend_exec_start")
    if frontend_command is not None:
        if (not isinstance(frontend_command, str) or not frontend_command.strip() or
                any(char in frontend_command for char in "\r\n\0")):
            raise RuntimeError("Invalid legacy frontend start command")
        frontend_dropin = root / "etc/systemd/system/spacemit-ai-gateway-frontend.service.d/legacy-listener.conf"
        if not frontend_dropin.exists() and not frontend_dropin.is_symlink():
            frontend_dropin.parent.mkdir(parents=True, exist_ok=True)
            private_write(frontend_dropin, "[Service]\nExecStart=\nExecStart=" + frontend_command + "\n")
    home = context["home"]
    inherited = {key: value for key, value in context.get("environment", {}).items()
                 if key.startswith("SPACEMIT_AI_GATEWAY_") and key != "SPACEMIT_AI_GATEWAY_CONFIG"}
    current = etc / "base.yaml"
    # 固定服务 UID 的旧标记不能证明完整管理员 unit 的 UID 已得到存储权限。
    if (not storage_marker.exists() or not assets_marker.exists()
            or (administrator_uid and not administrator_storage_marker.exists())) and source.is_file():
        import yaml
        preserve(legacy if legacy.is_file() else source, backup)
        default = root / "usr/share/spacemit-ai-gateway/base.yaml"
        data = yaml.safe_load(source.read_text()) or {}
        source_data = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        # 保留 SDK preset、视觉目录和错误日志的 HOME 语义；身份仍为固定非 root UID。
        inherited["HOME"] = home
        if "XDG_CACHE_HOME" in context.get("environment", {}):
            inherited["XDG_CACHE_HOME"] = context["environment"]["XDG_CACHE_HOME"]
        paths = []
        databases = {}

        def storage_paths(config, override=False):
            from spacemit_ai_gateway.app.settings import (
                LlmStorageConfig, EmbedStorageConfig, RerankStorageConfig, VlmStorageConfig,
            )
            # 只补应用实际使用的 storage 缺省叶子，不合并其他域字段或管理员模型配置。
            for name, storage_type in (("llm", LlmStorageConfig), ("embed", EmbedStorageConfig),
                                       ("rerank", RerankStorageConfig), ("vlm", VlmStorageConfig)):
                domain = config.setdefault(name, {})
                if isinstance(domain, dict):
                    storage = domain.setdefault("storage", {})
                    if isinstance(storage, dict):
                        for field, value in storage_type().model_dump().items():
                            storage.setdefault(field, value)
            for name in ("asr", "tts", "vad"):
                domain = config.get(name, {})
                value = domain.get("model_dir") if isinstance(domain, dict) else None
                if isinstance(value, str) and value:
                    absolute = materialize(value, home, context.get("working_directory", "/opt/spacemit-ai-gateway"))
                    if override and absolute != value:
                        inherited.setdefault(f"SPACEMIT_AI_GATEWAY_{name.upper()}__MODEL_DIR", absolute)
                    domain["model_dir"] = absolute
                    paths.append((absolute, True))
            for domain_name, domain in config.items():
                if not isinstance(domain, dict) or not isinstance(domain.get("storage"), dict):
                    continue
                for field in ("base_dir", "models_dir", "db_path"):
                    value = domain["storage"].get(field)
                    if isinstance(value, str):
                        absolute = materialize(value, home, context.get("working_directory", "/opt/spacemit-ai-gateway"))
                        if not Path(absolute).is_absolute():
                            absolute = str(Path(context.get("working_directory", "/opt/spacemit-ai-gateway")) / absolute)
                        if override and absolute != value:
                            inherited.setdefault(f"SPACEMIT_AI_GATEWAY_{domain_name.upper()}__STORAGE__{field.upper()}", absolute)
                        domain["storage"][field] = absolute
                        if field == "db_path":
                            databases[domain_name.upper()] = absolute
                        paths.append((str(Path(absolute).parent) if field == "db_path" else absolute, True))

        storage_paths(data)
        # managed ASR 是升级后新增的配置段；旧 YAML 里没有它，缺省路径必须补齐，
        # 否则升级后开启 managed 会去旧 HOME 缓存找模型。管理员已写的值保持不变。
        if default.is_file():
            managed_default = (yaml.safe_load(default.read_text()) or {}).get("asr", {}).get("managed")
            if isinstance(managed_default, dict):
                asr = data.setdefault("asr", {})
                if isinstance(asr, dict):
                    managed = asr.setdefault("managed", {})
                    if isinstance(managed, dict):
                        for key, value in managed_default.items():
                            if key == "qwen3" and isinstance(value, dict):
                                qwen = managed.setdefault("qwen3", {})
                                if isinstance(qwen, dict):
                                    for name, item in value.items():
                                        qwen.setdefault(name, item)
                            else:
                                managed.setdefault(key, value)
        rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        replace_config = (not current.exists() or
                          (default.is_file() and current.read_bytes() == default.read_bytes()) or
                          current.read_text() in (source_data, rendered))
        if not replace_config:
            # 管理员 YAML 字节保持不动；通过低优先级 credentials 物化依赖 HOME 的路径。
            paths.clear()
            databases.clear()
            storage_paths(yaml.safe_load(current.read_text()) or {}, override=True)
        pending = {}
        effective_environments = {}
        for name in ("credentials.env", "environment"):
            environment = etc / name
            values = environment_values(environment)
            lines = environment.read_text().splitlines(keepends=True) if environment.exists() else []
            for index, line in enumerate(lines):
                key = line.partition("=")[0].strip()
                if (storage_field(key) or model_directory_field(key) or key == "XDG_CACHE_HOME") and key in values:
                    value = materialize(values[key], home, context.get("working_directory", "/opt/spacemit-ai-gateway"))
                    lines[index] = env_line(key, value)
                    values[key] = value
                    if key != "XDG_CACHE_HOME":
                        paths.append((str(Path(value).parent) if key.endswith("DB_PATH") else value, True))
            effective_environments.update(values)
            if environment.exists() and "".join(lines) != environment.read_text():
                preserve(environment, etc / f"legacy-{name}")
                pending[environment] = "".join(lines)
        for key in list(inherited):
            if storage_field(key) or model_directory_field(key) or key == "XDG_CACHE_HOME":
                inherited[key] = materialize(inherited[key], home, context.get("working_directory", "/opt/spacemit-ai-gateway"))
                value = inherited[key]
                if key != "XDG_CACHE_HOME":
                    paths.append((str(Path(value).parent) if key.endswith("DB_PATH") else value, True))
        # 与服务 EnvironmentFile 顺序一致，只读实际生效的 DB，避免扫描已被覆盖的旧库。
        effective = {**inherited, **effective_environments}
        for key, value in effective.items():
            if storage_field(key) and key.upper().endswith("__DB_PATH"):
                domain = key.removeprefix("SPACEMIT_AI_GATEWAY_").split("__")[0].upper()
                databases[domain] = materialize(value, home, context.get("working_directory", "/opt/spacemit-ai-gateway"))
        paths.extend(registered_model_paths(root, databases.values()))
        effective_home = effective.get("HOME", home)
        paths.extend(legacy_default_assets(effective_home, effective.get("XDG_CACHE_HOME", "~/.cache")))
        if administrator_uid:
            grant_storage_access(root, paths, uid=administrator_uid)
        else:
            grant_storage_access(root, paths)
        if replace_config:
            private_write(current, rendered)
            # YAML 可能包含旧 API key，仅 root 和固定服务组读取。
            if root == Path("/"):
                os.chown(current, 0, grp.getgrnam("spacemit-ai-gateway").gr_gid)
            current.chmod(0o640)
        for environment, text in pending.items():
            private_write(environment, text)
        # 已有凭据的鉴权叶子不被覆盖；只补旧 unit 的环境及物化路径。
        values = environment_values(credentials)
        additions = "".join(env_line(key, value) for key, value in sorted(inherited.items()) if key not in values)
        if not credentials.exists():
            private_write(credentials, "# Preserved legacy gateway authentication; YAML remains authoritative.\n" + additions)
        elif additions:
            preserve(credentials, etc / "legacy-credentials.env")
            private_write(credentials, credentials.read_text().rstrip("\n") + "\n" + additions)
        marker.touch()
        storage_marker.touch()
        assets_marker.touch()
        if administrator_uid:
            administrator_storage_marker.touch()
    elif not source.is_file() and not credentials.exists():
        private_write(credentials, "SPACEMIT_AI_GATEWAY_AUTH__ENABLED=true\n" +
                      env_line("SPACEMIT_AI_GATEWAY_AUTH__API_KEYS", json.dumps([secrets.token_urlsafe(32)])))
    for name in ("spacemit-ai-gateway", "spacemit-ai-gateway-frontend"):
        unit = root / f"etc/systemd/system/{name}.service"
        if legacy_install and not unit.exists() and not unit.is_symlink():
            directory = root / f"etc/systemd/system/{name}.service.d"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "00-legacy-runtime.conf"
            if not path.exists() and not path.is_symlink():
                content = (
                    "[Service]\nUser=root\nGroup=\nEnvironment=\n"
                    "Environment=PATH=/opt/spacemit-ai-gateway/venv/bin:/usr/local/bin:/usr/bin:/bin\n"
                    "Environment=SPACEMIT_AI_GATEWAY_CONFIG=/etc/spacemit-ai-gateway/base.yaml\n"
                    "UMask=0022\nNoNewPrivileges=false\nProtectSystem=false\n"
                    "StateDirectory=\n"
                )
                if name == "spacemit-ai-gateway":
                    content += "WorkingDirectory=/opt/spacemit-ai-gateway\n"
                else:
                    content += (
                        "ExecStart=\nExecStart=/usr/bin/python3 "
                        "/opt/spacemit-ai-gateway/frontend/serve.py 8326 --bind ::\n"
                    )
                private_write(path, content)
        if (name == "spacemit-ai-gateway" and legacy_install and unit.is_file()
                and context.get("environment", {}).get("SPACEMIT_AI_GATEWAY_CONFIG")
                == "/opt/spacemit-ai-gateway/configs/base.yaml"):
            # 完整管理员 unit 及 EnvironmentFile 保持原样，兼容 dpkg 已移除的旧配置路径。
            if not legacy.exists() and not legacy.is_symlink():
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.symlink_to("/etc/spacemit-ai-gateway/base.yaml")
            if legacy.is_symlink() and legacy.readlink() == Path("/etc/spacemit-ai-gateway/base.yaml"):
                user = context.get("user") or "root"
                uid = int(user) if user.isdigit() else pwd.getpwnam(user).pw_uid
                if uid != 0:
                    grant_storage_access(root, [("/etc/spacemit-ai-gateway/base.yaml", False)], uid=uid,
                                         traverse=["/opt/spacemit-ai-gateway/configs"])
        path = root / f"etc/systemd/system/{name}.service.d/runtime-user.conf"
        if path.is_file() and pattern.fullmatch(path.read_text()):
            preserve(path, etc / f"{name}-legacy-runtime-user.conf")
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
        {"install": install_runtime, "migrate": migrate_legacy, "remove": remove_runtime,
         "ensure-voice-key": ensure_voice_key, "grant-voice-key": grant_voice_key_access}[sys.argv[1]]()
