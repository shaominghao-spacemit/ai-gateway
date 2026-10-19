"""SenseVoice 受管进程：SDK 在此进程中完整加载/退出，继承父进程的 flock FD。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

from ...app.settings import AsrConfig
from ...common.ready_state import BackendReadyState
from .adapters.sensevoice import SenseVoiceBackend, _lang_from_str
from .managed_assets import language_support


async def run():
    # SDK/C 扩展可能向 stdout 写日志，协议使用保存的原 stdout。
    output = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    backend = None
    identity = None
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                request = json.loads(line)
                op = request['op']
                if op == 'init' and backend is None:
                    cfg = AsrConfig.model_validate(request['config'])
                    backend = SenseVoiceBackend(cfg)
                    await backend.warmup()
                    if backend.state != BackendReadyState.READY or backend._mock:
                        raise RuntimeError('managed_sensevoice_requires_real_sdk')
                    engine_language = cfg.language
                    identity = {'model': 'sensevoice', 'languages': [cfg.language]}
                    reply = identity
                elif op == 'health' and backend is not None:
                    reply = identity
                elif op == 'recognize' and backend is not None:
                    selected = request['languages']
                    if language_support('sensevoice', selected):
                        raise ValueError('sensevoice_language_list_unsupported')
                    if selected[0] != engine_language:
                        setter = getattr(backend._engine, 'set_language', None)
                        if not callable(setter):
                            raise RuntimeError('sensevoice_dynamic_language_unsupported')
                        await asyncio.to_thread(setter, _lang_from_str(selected[0]))
                        engine_language = selected[0]
                    result = await backend.recognize(
                        base64.b64decode(request['audio'], validate=True), 16000,
                        selected[0], cfg.punctuation)
                    reply = {'text': result.text}
                else:
                    raise ValueError('invalid_worker_request')
            except Exception as exc:
                reply = {'error': str(exc)}
            output.write(json.dumps(reply) + '\n')
    finally:
        if backend is not None:
            await backend.shutdown()
        output.close()


if __name__ == '__main__':
    asyncio.run(run())
