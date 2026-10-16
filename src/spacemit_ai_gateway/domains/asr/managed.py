"""单客户端托管租约。资源锁跟随后端真实寿命，不以 HTTP 或令牌超时解锁。"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import hmac
import os
import secrets
import socket
import stat
import threading
from pathlib import Path
from uuid import uuid4

import psutil

from ...common.errors import DomainError
from .managed_assets import check_assets, language_support, languages
from .managed_backend import ManagedBackend

_INSTANCE_IDS: dict[int, str] = {}
_PARTICIPANTS: dict[tuple[int, str], int] = {}
_PARTICIPANT_LOCK = threading.Lock()


def register_participant(lock_path: str) -> int:
    with _PARTICIPANT_LOCK:
        directory = str(Path(lock_path).parent.resolve())
        key = (os.getpid(), directory)
        if key in _PARTICIPANTS:
            return _PARTICIPANTS[key]
        path = Path(directory) / f"participant-{os.getpid()}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o644)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError('participant_not_regular')
            os.fchmod(fd, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        _PARTICIPANTS[key] = fd  # 不随 service.shutdown 释放；进程退出时 kernel 回收。
        return fd


def cooperative_participant(pid: int, lock_path: str) -> bool:
    path = Path(lock_path).parent / f"participant-{pid}.lock"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)
    except OSError:
        return False


def lock_reason(exc: OSError) -> str:
    if isinstance(exc, BlockingIOError):
        return "engine_lock_busy"
    if isinstance(exc, PermissionError):
        return "engine_lock_permission_denied"
    return "engine_lock_unavailable"



def instance_id():
    return _INSTANCE_IDS.setdefault(os.getpid(), str(uuid4()))


def conflict(reason: str, status_code: int = 409) -> DomainError:
    return DomainError(reason, code="managed_asr_unavailable", status_code=status_code)


def engine_port_busy(port: int) -> bool:
    """端口占用是跨 UID 也能复核的引擎归属证据，比进程名可靠。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 只忽略 TIME_WAIT
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
        return False


def foreign_processes(owned_pid: int | None = None, lock_path: str = "/run/lock/bibit-asr/engine.lock",
                      engine_port: int = 8063) -> str:
    """保守拒绝未参与 flock 的旧调用者；绝不杀死或接管它们。

    引擎归属以端口占用为准：llama-server 同样承载 LLM/Embedding，按进程名拦会挡住
    既有服务。已知边界：不参与协议的旧进程若在检查之后才启动 ASR，需由部署侧约束。
    """
    if owned_pid is None and engine_port_busy(engine_port):
        return f"unmanaged_asr_engine_port:{engine_port}"
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.pid in {os.getpid(), owned_pid}:
            continue
        try:
            name = proc.info["name"] or ""
            args = proc.info["cmdline"] or []
            voice = name == "bibit-audio" or (args and Path(args[0]).name == "bibit-audio")
            gateway = (name == 'spacemit-ai-gat' or any(
                arg == "spacemit_ai_gateway.app.main:app" or Path(arg).name == 'spacemit-ai-gateway'
                for arg in args))
            if voice or gateway:
                if cooperative_participant(proc.pid, lock_path):
                    continue
                return f"unmanaged_asr_process:{proc.pid}"
            if any(arg == "spacemit_ai_gateway.domains.asr.managed_worker" for arg in args):
                return f"unmanaged_asr_process:{proc.pid}"
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            return f"process_ownership_unknown:{proc.pid}"
    return ""


def take_lock(path: str) -> int:
    # tmpfiles 创建文件。不能在进程中 unlink/recreate，也不能主动 LOCK_UN 解开子进程的锁。
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("engine_lock_not_regular_file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


class ManagedAsr:
    def __init__(self, service):
        self.service = service
        self.config = service._config
        self.backend = None
        self.owner = self.token = self.model = None
        self.delivered_token = None
        self.selected: list[str] = []
        self.fd: int | None = None
        self.closing = False
        self.failed = False
        self.pending: set[asyncio.Task] = set()
        self.operations: set[asyncio.Task] = set()
        self.release_task: asyncio.Task | None = None

    @property
    def active(self):
        return self.fd is not None

    def _tracked(self, coroutine, collection):
        task = asyncio.create_task(coroutine)
        collection.add(task)
        def finished(done):
            collection.discard(done)
            if not done.cancelled():
                done.exception()  # 断连后仍检索异常，资源由会话继续拥有。
        task.add_done_callback(finished)
        return task

    async def _preflight(self, model, selected):
        ready, reason = await asyncio.to_thread(check_assets, self.config, model)
        if not self.config.managed.enabled:
            return ready, "managed_disabled"
        if not ready:
            return False, reason
        if self.service._participant_error:
            return True, self.service._participant_error
        unsupported = language_support(model, selected)
        if unsupported:
            return True, unsupported
        if self.service._backends or self.service._legacy_asr_used:
            return True, "legacy_asr_loaded"
        foreign = await asyncio.to_thread(
            foreign_processes, self.backend.pid if self.backend else None,
            self.config.managed.lock_path, self.config.managed.qwen3.port)
        return True, foreign

    async def status(self, model: str, language: str):
        if model not in {"sensevoice", "qwen3_asr_0.6b"}:
            raise conflict("unsupported_model", 422)
        try:
            selected = languages(language)
        except ValueError as exc:
            raise conflict(str(exc), 422) from exc
        async with self.service._load_lock:
            assets, reason = await self._preflight(model, selected)
            ready = False
            state = "owned" if self.active else "free"
            if not self.active:
                try:
                    fd = take_lock(self.config.managed.lock_path)
                    os.close(fd)
                except OSError as exc:
                    state = "blocked"
                    reason = reason or lock_reason(exc)
            elif not reason:
                if self.closing:
                    reason = "releasing"
                elif self.failed:
                    reason = "inference_failed_release_required"
                elif self.model != model:
                    reason = "session_target_mismatch"
                elif self.backend is None:
                    reason = "backend_unavailable"
                else:
                    ready = await self.backend.healthy()
                    reason = "ready" if ready else "backend_unhealthy"
            if reason and reason != "ready":
                state = "blocked"
            return dict(protocol_version=1, instance_id=instance_id(), model=model,
                        languages=selected, assets_ready=assets, ready=ready,
                        owner=self.owner, resource_state=state, reason=reason or "available")

    async def acquire(self, owner: str, model: str, language: str):
        # 从 HTTP 生命周期中摘出加载任务；断连不能丢失半初始化子进程。
        task = self._tracked(self._acquire(owner, model, language), self.operations)
        try:
            result = await asyncio.shield(task)
            self.delivered_token = result['token']
            return result
        except asyncio.CancelledError:
            # 加载仍须收尾；随后只释放本次返回的精确令牌，不碰后来取得资源的会话。
            self._tracked(self._abandon(task, owner), self.operations)
            raise

    async def _abandon(self, task, owner):
        try:
            result = await task
        except Exception:
            return  # 启动失败由 _acquire 清理；清理失败时保留锁并阻塞。
        async with self.service._load_lock:
            if (self.owner != owner or self.token != result['token']
                    or self.delivered_token == result['token']):
                return
            if self.release_task is None:
                self.closing = True
                self.release_task = self._tracked(self._release(), self.operations)
            release = self.release_task
        await asyncio.shield(release)

    async def _acquire(self, owner, model, language):
        try:
            selected = languages(language)
        except ValueError as exc:
            raise conflict(str(exc), 422) from exc
        if model not in {"sensevoice", "qwen3_asr_0.6b"}:
            raise conflict("unsupported_model", 422)
        async with self.service._load_lock:
            unsupported = language_support(model, selected)
            if unsupported:
                raise conflict(unsupported, 422)
            if self.active:
                if (self.owner != owner or self.model != model
                        or self.closing or self.failed):
                    raise conflict("managed_session_already_owned")
            else:
                _, reason = await self._preflight(model, selected)
                if reason:
                    raise conflict(reason)
                try:
                    self.fd = take_lock(self.config.managed.lock_path)
                except OSError as exc:
                    raise conflict(lock_reason(exc)) from exc
                self.owner, self.model, self.selected = owner, model, selected
                self.token = secrets.token_urlsafe(32)
                self.closing = self.failed = False
                try:
                    # 在锁内再次检查不协作的旧调用者，且不走旧 load/unload/default 路径。
                    reason = await asyncio.to_thread(
                        foreign_processes, None, self.config.managed.lock_path,
                        self.config.managed.qwen3.port)
                    if reason:
                        raise conflict(reason)
                    self.backend = ManagedBackend(self.config, model, selected, self.fd)
                    await self.backend.start()
                    if not await self.backend.healthy():
                        raise conflict("backend_unhealthy", 503)
                except BaseException as exc:
                    await self._dispose()
                    if isinstance(exc, Exception) and not isinstance(exc, DomainError):
                        raise conflict(f"managed_backend_start_failed:{exc}", 503) from exc
                    raise
            token = self.token
        result = await self.status(model, language)
        if not result['ready']:
            await self.release(owner, token)
            raise conflict(result['reason'], 503)
        result['token'] = token
        return result

    def _authorize(self, owner, token):
        if not self.active or self.owner != owner or not hmac.compare_digest((self.token or '').encode(), token.encode()):
            raise conflict("invalid_managed_session", 403)

    async def recognize(self, owner, token, language, audio):
        async with self.service._load_lock:
            self._authorize(owner, token)
            try:
                selected = languages(language)
            except ValueError as exc:
                raise conflict(str(exc), 422) from exc
            unsupported = language_support(self.model, selected)
            if unsupported:
                raise conflict(unsupported, 422)
            if self.closing or self.failed or self.backend is None:
                raise conflict("session_not_ready")
            task = self._tracked(self._infer(audio, selected), self.pending)
        return await asyncio.shield(task)

    async def _infer(self, audio, selected):
        try:
            return await self.backend.recognize(audio, selected)
        except BaseException:
            # HTTP 超时不能证明 llama 已停；拒绝后续识别直到 release 终止并回收进程。
            self.failed = True
            raise

    async def release(self, owner, token):
        async with self.service._load_lock:
            self._authorize(owner, token)
            if self.release_task is None:
                self.closing = True
                self.release_task = self._tracked(self._release(), self.operations)
            task = self.release_task
        return await asyncio.shield(task)

    async def _release(self):
        if self.pending:
            await asyncio.gather(*tuple(self.pending), return_exceptions=True)
        async with self.service._load_lock:
            await self._dispose()
        return {"released": True}

    async def _dispose(self):
        # shutdown 失败时保留锁、归属和后端，以便阻塞下一次加载并允许重试释放。
        try:
            if self.backend is not None:
                await self.backend.shutdown()
        except BaseException:
            self.closing = True
            self.release_task = None
            raise
        if self.fd is not None:
            os.close(self.fd)
        self.fd = self.backend = self.owner = self.token = self.model = None
        self.selected = []
        self.delivered_token = None
        self.closing = self.failed = False
        self.release_task = None

    async def shutdown(self):
        while self.operations:
            await asyncio.gather(*tuple(self.operations), return_exceptions=True)
            await asyncio.sleep(0)
        if self.active:
            await self.release(self.owner, self.token)
