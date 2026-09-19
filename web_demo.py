"""Local web app + real Daytona scheduling endpoint: python web_demo.py."""
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from harness import run_harness
from harness.runtime import DaytonaScheduleExecutionRuntime

ROOT = Path(__file__).resolve().parent
RUNTIME = DaytonaScheduleExecutionRuntime()
LOCK = threading.Lock()


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
        self.wfile.write(data)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        target = (ROOT / path.lstrip('/')).resolve()
        allowed = path in ('/', '/index.html') or (
            target.is_relative_to(ROOT / 'js') and target.suffix == '.js'
        ) or (target.is_relative_to(ROOT / 'css') and target.suffix == '.css')
        if not allowed:
            return self._json(404, {'error': 'NOT_FOUND'})
        super().do_GET()

    def do_HEAD(self):
        self._json(405, {'error': 'METHOD_NOT_ALLOWED'})

    def do_POST(self):
        if self.path != '/api/demo/schedule':
            return self._json(404, {'error': 'NOT_FOUND'})
        # This local demo accepts only its own browser origin.
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://127.0.0.1:5191', 'http://localhost:5191'):
            return self._json(403, {'error': 'ORIGIN_NOT_ALLOWED'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                return self._json(400, {'error': 'INVALID_BODY'})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('JSON object required')
        except (ValueError, UnicodeError):
            return self._json(400, {'error': 'INVALID_JSON'})
        if not LOCK.acquire(blocking=False):
            return self._json(503, {'error': 'BUSY', 'message': '앞선 추천을 처리 중입니다. 잠시 후 다시 시도해 주세요.'})
        try:
            result = run_harness(payload, runtime=RUNTIME)
            self._json(200, result)
        except ValueError as exc:
            self._json(400, {'error': 'VALIDATION_ERROR', 'message': str(exc)})
        except Exception:
            self._json(503, {'error': 'DAYTONA_UNAVAILABLE', 'message': 'Daytona 실행 실패. 서버의 DAYTONA_API_KEY 설정과 네트워크를 확인해 주세요.'})
        finally:
            LOCK.release()


if __name__ == '__main__':
    print('Web demo: http://127.0.0.1:5191/#/live', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 5191), Handler).serve_forever()
