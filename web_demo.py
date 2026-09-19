"""Local web app + real Daytona scheduling endpoint: python web_demo.py."""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from harness import run_harness
from harness.models import parse_user_context
from harness.runtime import DaytonaScheduleExecutionRuntime

ROOT = Path(__file__).resolve().parent
RUNTIME = DaytonaScheduleExecutionRuntime()
LOCK = threading.Lock()
RESPONSE_TIMEOUT_SECONDS = 25
ALLOWED_ORIGINS = {'http://127.0.0.1:5191', 'http://localhost:5191'}
PUBLIC_ORIGIN = os.environ.get('HARNESS_PUBLIC_ORIGIN', '').rstrip('/')
if PUBLIC_ORIGIN:
    ALLOWED_ORIGINS.add(PUBLIC_ORIGIN)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The browser may have cancelled its request.

    def _error(self, status, code, message, details=None):
        self._json(status, {
            'requestId': getattr(self, 'request_id', None) or 'req_' + uuid.uuid4().hex,
            'error': {'code': code, 'message': message, 'details': details or {}},
        })

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        target = (ROOT / path.lstrip('/')).resolve()
        allowed = path in ('/', '/index.html') or (
            target.is_relative_to(ROOT / 'js') and target.suffix == '.js'
        ) or (target.is_relative_to(ROOT / 'css') and target.suffix == '.css')
        if not allowed:
            return self._error(404, 'NOT_FOUND', '지원하지 않는 경로입니다.')
        super().do_GET()

    def do_HEAD(self):
        self._error(405, 'METHOD_NOT_ALLOWED', '지원하지 않는 메서드입니다.')

    def do_POST(self):
        self.request_id = 'req_' + uuid.uuid4().hex
        started = time.perf_counter()
        if self.path != '/api/demo/schedule':
            return self._error(404, 'NOT_FOUND', '지원하지 않는 경로입니다.')
        # This local demo accepts only its own browser origin.
        origin = self.headers.get('Origin')
        if origin and origin not in ALLOWED_ORIGINS:
            return self._error(403, 'ORIGIN_NOT_ALLOWED', '같은 웹 앱에서 요청해 주세요.')
        if self.headers.get_content_type() != 'application/json':
            return self._error(415, 'UNSUPPORTED_MEDIA_TYPE', 'Content-Type은 application/json이어야 합니다.')
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                return self._error(400, 'VALIDATION_ERROR', '요청 본문은 1~16384 bytes여야 합니다.')
            self.connection.settimeout(15)
            payload = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
            if not isinstance(payload, dict):
                raise ValueError('JSON object required')
        except (ValueError, UnicodeError):
            return self._error(400, 'VALIDATION_ERROR', '올바른 JSON 객체가 필요합니다.')
        except TimeoutError:
            return self._error(408, 'REQUEST_TIMEOUT', '요청 본문 전송 시간이 초과되었습니다.')
        try:
            parse_user_context(payload)
        except ValueError as exc:
            return self._error(400, 'VALIDATION_ERROR', str(exc))
        if not LOCK.acquire(blocking=False):
            return self._error(503, 'BUSY', '앞선 추천을 처리 중입니다. 잠시 후 다시 시도해 주세요.')
        finished = threading.Event()
        outcome = {}

        def execute():
            try:
                outcome['result'] = run_harness(payload, runtime=RUNTIME)
            except Exception:
                outcome['failed'] = True
            finally:
                # A response timeout does not stop a remote SDK call. Keep the
                # sandbox locked until it actually ends to prevent overlapping runs.
                LOCK.release()
                finished.set()

        threading.Thread(target=execute, daemon=True).start()
        if not finished.wait(RESPONSE_TIMEOUT_SECONDS):
            return self._error(504, 'TIMEOUT', '일정 생성 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.')
        if outcome.get('failed'):
            return self._error(503, 'DAYTONA_UNAVAILABLE', 'Daytona 실행 실패. 서버 설정과 네트워크를 확인해 주세요.')
        result = outcome['result']
        result['requestId'] = self.request_id
        result['generatedAt'] = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec='seconds')
        result['source'] = 'llm' if result['meta'].get('ranking_provider') == 'nosana' else 'fallback'
        result['meta']['contractVersion'] = 'demo.v1'
        result['meta']['totalLatencyMs'] = round((time.perf_counter() - started) * 1000)
        self._json(200, result)


if __name__ == '__main__':
    print('Web demo: http://127.0.0.1:5191/#/live', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 5191), Handler).serve_forever()
