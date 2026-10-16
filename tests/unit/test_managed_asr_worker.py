"""执行实际 worker 协议循环，核对仅有固定语言能力的 SDK。"""
import base64
import io
import json
from types import SimpleNamespace

import pytest

from spacemit_ai_gateway.common.ready_state import BackendReadyState
from spacemit_ai_gateway.domains.asr import managed_worker


@pytest.mark.asyncio
@pytest.mark.parametrize('dynamic', [False, True])
async def test_fixed_language_engine_works_and_language_change_is_explicit(monkeypatch, dynamic):
    calls = []

    class Backend:
        state = BackendReadyState.READY
        _mock = False

        def __init__(self, config):
            self._engine = SimpleNamespace()
            if dynamic:
                self._engine.set_language = lambda language: calls.append(('language', language))

        async def warmup(self):
            pass

        async def recognize(self, audio, sample_rate, language, punctuation):
            calls.append(('recognize', language))
            assert audio == b'\x00\x00' and sample_rate == 16000
            return SimpleNamespace(text='recognized')

        async def shutdown(self):
            calls.append(('shutdown',))

    class Output(io.StringIO):
        def close(self):
            pass

    output = Output()
    audio = base64.b64encode(b'\x00\x00').decode()
    requests = [
        {'op': 'init', 'config': {'language': 'auto'}},
        {'op': 'recognize', 'languages': ['auto'], 'audio': audio},
        {'op': 'recognize', 'languages': ['zh'], 'audio': audio},
        {'op': 'health'},
    ]
    monkeypatch.setattr(managed_worker, 'sys', SimpleNamespace(
        stdin=io.StringIO(''.join(json.dumps(item) + '\n' for item in requests)),
        stdout=SimpleNamespace(fileno=lambda: 1), stderr=SimpleNamespace(fileno=lambda: 2)))
    monkeypatch.setattr(managed_worker.os, 'dup', lambda _: 123)
    monkeypatch.setattr(managed_worker.os, 'dup2', lambda *_: None)
    monkeypatch.setattr(managed_worker.os, 'fdopen', lambda *_args, **_kw: output)
    monkeypatch.setattr(managed_worker, 'SenseVoiceBackend', Backend)
    monkeypatch.setattr(managed_worker, '_lang_from_str', lambda language: language)
    await managed_worker.run()
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0] == replies[3] == {'model': 'sensevoice', 'languages': ['auto']}
    assert replies[1] == {'text': 'recognized'}
    if dynamic:
        assert replies[2] == {'text': 'recognized'}
        assert calls == [('recognize', 'auto'), ('language', 'zh'), ('recognize', 'zh'), ('shutdown',)]
    else:
        assert replies[2] == {'error': 'sensevoice_dynamic_language_unsupported'}
        assert calls == [('recognize', 'auto'), ('shutdown',)]
