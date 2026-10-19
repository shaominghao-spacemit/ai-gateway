#!/usr/bin/env python3
"""测试专用 HTTP 推理进程，记录真实参数并允许启动失败/慢退出注入。"""
import argparse
import json
import os
from pathlib import Path
import signal
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--port', type=int)
args, unknown = parser.parse_known_args()
if os.environ.get('TEST_LLAMA_EXIT'):
    raise SystemExit(7)
if os.environ.get('TEST_LLAMA_IGNORE_TERM'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get('TEST_LLAMA_ARGS'):
    import sys
    Path(os.environ['TEST_LLAMA_ARGS']).write_text(json.dumps(sys.argv))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self.reply({'status': 'ok'})
        else:
            self.reply({'data': [{'id': os.environ.get('TEST_LLAMA_MODEL', 'qwen3-asr')}]})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/slots/0?action=erase':
            self.reply({'erased': True})
            return
        if os.environ.get('TEST_LLAMA_PAYLOAD'):
            Path(os.environ['TEST_LLAMA_PAYLOAD']).write_bytes(body)
        time.sleep(float(os.environ.get('TEST_LLAMA_DELAY', '0')))
        self.reply({'choices': [{'message': {'content': '  测试识别  '}}]})


HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
