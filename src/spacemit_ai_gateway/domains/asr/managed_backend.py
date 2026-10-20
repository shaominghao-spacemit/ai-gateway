"""受管 SDK/llama 子进程。仅操作本方进程组，并将资源锁 FD 传入子进程。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import psutil

from ...common.errors import AsrBackendUnavailable
from .adapters.qwen3_asr import _pcm16_to_wav
from .managed_assets import LANGUAGES, QWEN_ENCODERS, QWEN_TEXT, model_dir


logger = logging.getLogger(__name__)


class ManagedBackend:
    def __init__(self, config, model, selected, lock_fd):
        self.config, self.model, self.selected, self.lock_fd = config, model, selected, lock_fd
        self.process = None
        self.runtime_dir = None
        self.io_lock = asyncio.Lock()
        self.client = None
        self.identity = None
        self.exchange_started = None
        self.health_task = None
        self.affinity_error = None
        self._affinity_stop = threading.Event()
        self._affinity_thread = None
        self.base_url = f"http://127.0.0.1:{config.managed.qwen3.port}"

    @property
    def pid(self):
        return self.process.pid if self.process else None

    async def start(self):
        root = model_dir(self.config, self.model)
        env = os.environ.copy()
        if self.model == 'sensevoice':
            args = [sys.executable, '-m', 'spacemit_ai_gateway.domains.asr.managed_worker']
        else:
            cfg = self.config.managed.qwen3
            self.runtime_dir = tempfile.TemporaryDirectory(prefix='gateway-asr-')
            runtime = Path(self.runtime_dir.name)
            source = json.loads((root / 'config.json').read_text())
            source['audio_model']['ep_config'] = {
                'spacemit_ep_intra_thread_num': cfg.threads,
                'spacemit_ep_inter_thread_num': 1,
                'spacemit_ep_intra_thread_affinity': cfg.ep_affinity.replace(',', ';'),
            }
            (runtime / 'config.json').write_text(json.dumps(source))
            for asset in (*QWEN_ENCODERS, QWEN_TEXT):
                (runtime / asset).symlink_to(root / asset)
            for name in tuple(env):
                if name.startswith('SPACEMIT_EP_') or name in {'LD_LIBRARY_PATH', 'SPACEMIT_PERFER_CORE_ID', 'SPACEMIT_DISABLE_TCM'}:
                    env.pop(name)
            env.update(SPACEMIT_DISABLE_TCM='1', SPACEMIT_PERFER_CORE_ID=cfg.ep_affinity.replace(';', ','))
            args = [shutil.which(cfg.server_bin) or cfg.server_bin, '-m', str(root / QWEN_TEXT),
                    '--host', '127.0.0.1', '--port', str(cfg.port), '--alias', 'qwen3-asr',
                    '-t', str(cfg.threads), '-tb', str(cfg.batch_threads), '-c', str(cfg.ctx_size),
                    '--media-backend', 'smt', '--smt-config-dir', str(runtime),
                    '--slot-save-path', str(runtime), '--slots', '--warmup', '-np', '1']
            # 内部请求不设置读取超时；外部断连后仍等真实推理完成，release 才可回执。
            self.client = httpx.AsyncClient(base_url=self.base_url, timeout=None, trust_env=False)
        self.process = await asyncio.create_subprocess_exec(
            *args, env=env, pass_fds=(self.lock_fd,), start_new_session=True,
            stdin=asyncio.subprocess.PIPE if self.model == 'sensevoice' else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if self.model == 'sensevoice' else asyncio.subprocess.DEVNULL,
        )
        if self.model == "qwen3_asr_0.6b":
            self._start_affinity_guard()
        await asyncio.wait_for(self._wait_ready(root), self.config.managed.startup_timeout)

    def _pin_a100_threads(self, target):
        # 与 Voice 一致：只收紧完全处于 A100 的线程，保留大核/混合亲和性线程。
        for task in Path(f'/proc/{self.pid}/task').iterdir():
            if not task.name.isdecimal():
                continue
            tid = int(task.name)
            try:
                current = os.sched_getaffinity(tid)
                if current and current <= set(range(8, 16)) and not current <= target:
                    os.sched_setaffinity(tid, target)
            except ProcessLookupError:
                continue  # 线程可以在枚举后自行退出。

    def _start_affinity_guard(self):
        try:
            target = {int(cpu) for cpu in self.config.managed.qwen3.ep_affinity.replace(';', ',').split(',')}
        except ValueError as exc:
            raise AsrBackendUnavailable('invalid_qwen3_ep_affinity') from exc
        if not target or not target <= set(range(8, 16)):
            raise AsrBackendUnavailable('invalid_qwen3_a100_affinity')
        self._affinity_stop.clear()
        def guard():
            while not self._affinity_stop.is_set():
                try:
                    self._pin_a100_threads(target)
                except FileNotFoundError:
                    return
                except OSError as exc:
                    self.affinity_error = 'qwen3_affinity_guard_failed'
                    logger.error('managed Qwen3 affinity guard failed: %s', exc)
                    return
                self._affinity_stop.wait(0.0001)
        self._affinity_thread = threading.Thread(target=guard, name='managed-asr-affinity', daemon=True)
        self._affinity_thread.start()

    async def _wait_ready(self, root):
        if self.model == 'sensevoice':
            cfg = self.config.model_copy(update={'backend': 'sensevoice', 'model_dir': str(root), 'language': self.selected[0]})
            reply = await self._exchange({'op': 'init', 'config': cfg.model_dump()})
            self.identity = (reply.get('model'), reply.get('languages'))
            if self.identity != (self.model, self.selected):
                raise AsrBackendUnavailable('worker_identity_mismatch')
        else:
            while not await self.healthy():
                if self.affinity_error:
                    raise AsrBackendUnavailable(self.affinity_error)
                if self.process.returncode is not None:
                    raise AsrBackendUnavailable('llama_server_exited_during_startup')
                await asyncio.sleep(0.05)

    async def _exchange(self, request):
        async with self.io_lock:
            if not self.process or self.process.returncode is not None:
                raise AsrBackendUnavailable('sensevoice_worker_exited')
            self.exchange_started = time.monotonic()
            try:
                self.process.stdin.write((json.dumps(request) + '\n').encode())
                await self.process.stdin.drain()
                reply = json.loads(await self.process.stdout.readline())
                if 'error' in reply:
                    raise AsrBackendUnavailable(reply['error'])
                return reply
            finally:
                self.exchange_started = None

    async def healthy(self):
        if not self.process or self.process.returncode is not None or self.affinity_error:
            return False
        try:
            if self.model == 'sensevoice':
                state = await asyncio.to_thread(psutil.Process(self.pid).status)
                if state in {psutil.STATUS_STOPPED, psutil.STATUS_TRACING_STOP, psutil.STATUS_ZOMBIE}:
                    return False
                if self.health_task is not None and not self.health_task.done():
                    return False
                if self.io_lock.locked():
                    # 复用启动预算作为 worker 无响应健康窗口，不取消仍持锁的推理。
                    return (self.identity == (self.model, self.selected)
                            and self.exchange_started is not None
                            and time.monotonic() - self.exchange_started < self.config.managed.startup_timeout)
                # 超时/断连只结束探针等待，已发送交换仍持锁消费回复，避免下一次识别串包。
                self.health_task = asyncio.create_task(self._exchange({'op': 'health'}))
                self.health_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                reply = await asyncio.wait_for(asyncio.shield(self.health_task), 2)
                return (reply.get('model'), reply.get('languages')) == (self.model, self.selected)
            # 端口健康不能证明是我们的子进程：核对 socket 归属及精确模型身份。
            connections = await asyncio.to_thread(psutil.Process(self.pid).net_connections, kind='tcp')
            if not any(c.status == psutil.CONN_LISTEN and c.laddr.ip == '127.0.0.1'
                       and c.laddr.port == self.config.managed.qwen3.port for c in connections):
                return False
            health, models = await asyncio.gather(
                self.client.get('/health', timeout=2), self.client.get('/v1/models', timeout=2))
            health.raise_for_status()
            models.raise_for_status()
            return (health.json().get('status') == 'ok'
                    and [m['id'] for m in models.json()['data']] == ['qwen3-asr']
                    and self.process.returncode is None)
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, psutil.Error,
                asyncio.TimeoutError, AsrBackendUnavailable):
            return False

    async def recognize(self, audio, selected):
        if self.model == 'sensevoice':
            reply = await self._exchange({'op': 'recognize', 'audio': base64.b64encode(audio).decode(), 'languages': selected})
            return reply['text']
        async with self.io_lock:
            if not await self.healthy():
                raise AsrBackendUnavailable('managed_qwen3_unhealthy')
            content = [{'type': 'input_audio', 'input_audio': {
                'data': base64.b64encode(_pcm16_to_wav(audio, 16000)).decode(), 'format': 'wav'}}]
            if selected != ['auto']:
                prompt = ','.join(LANGUAGES[x] for x in selected)
                content.append({'type': 'text', 'text': f'language {prompt}<asr_text>'})
            try:
                response = await self.client.post('/v1/chat/completions', json={
                    'model': 'qwen3-asr', 'id_slot': 0,
                    'messages': [{'role': 'user', 'content': content}],
                    'max_tokens': self.config.managed.qwen3.max_tokens, 'temperature': 0})
                response.raise_for_status()
                text = response.json()['choices'][0]['message']['content']
                if isinstance(text, list):
                    text = ''.join(item['text'] for item in text if item.get('type') == 'text')
                if not isinstance(text, str):
                    raise ValueError('invalid_qwen3_text')
                erased = await self.client.post('/slots/0?action=erase', timeout=2)
                # 某些旧 llama 版本不支持 erase；沿用 Voice 的兼容行为。
                if erased.status_code not in {200, 404, 405, 501}:
                    erased.raise_for_status()
                return text.strip()
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise AsrBackendUnavailable(f'managed_qwen3_inference_failed:{exc}') from exc

    async def shutdown(self):
        if self.process is not None:
            # TERM/KILL 指向新建的本方进程组，覆盖辅助子进程；绝不对外部端口所有者发信号。
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), self.config.managed.shutdown_timeout)
                except asyncio.TimeoutError:
                    if sig == signal.SIGKILL:
                        raise AsrBackendUnavailable('managed_child_did_not_exit')
                    continue
                # 组内其他持锁子进程也必须退出，不能只等主进程。
                try:
                    os.killpg(self.pid, 0)
                except ProcessLookupError:
                    break
            else:
                raise AsrBackendUnavailable('managed_process_group_still_alive')
            self.process = None
        if self.health_task is not None:
            self.health_task.cancel()
            await asyncio.gather(self.health_task, return_exceptions=True)
            self.health_task = None
        self._affinity_stop.set()
        if self._affinity_thread is not None:
            await asyncio.to_thread(self._affinity_thread.join)
            self._affinity_thread = None
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        if self.runtime_dir is not None:
            self.runtime_dir.cleanup()
            self.runtime_dir = None
