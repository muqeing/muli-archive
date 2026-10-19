"""Serve a truthful startup state while all original checks still run."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import resource
import sys
import threading
import time


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>归档服务正在准备</title><style>body{font:16px system-ui;margin:10vh auto;
max-width:600px;padding:24px;line-height:1.8;background:#f5f6f8;color:#243038}
main{background:white;border-radius:12px;padding:24px}</style><main>
<h2>归档服务正在准备</h2><p>正在核对已保存的记录，完成后会自动打开归档页面。</p>
<p>此时暂不能提交新归档。已提交任务的结果以保存的任务记录为准。</p>
<p id="phase">正在核对任务和历史记录</p></main><script>
async function poll(){try{const r=await fetch('/api/review-status',{cache:'no-store'});
const s=await r.json();if(r.ok&&s.status!=='initializing'){location.reload();return}
if(s.phase)document.getElementById('phase').textContent=s.phase}catch(e){}
setTimeout(poll,2000)}setTimeout(poll,1000)</script></html>""".encode()


def diagnostic(phase, started, service='archive-startup'):
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != 'darwin':
        peak *= 1024
    print(json.dumps({'service': service, 'phase': phase,
                      'elapsed_seconds': round(time.monotonic() - started, 3),
                      'rss_peak_bytes': peak}, ensure_ascii=False), flush=True)


class StartupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        self.started = time.monotonic()
        self.phase = '正在核对任务和历史记录'
        self.application_handler = None
        self.application_close = None
        self.thread = None
        self._closed = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body, content_type):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.send_header('Connection', 'close')
                self.close_connection = True
                if code == 503:
                    self.send_header('Retry-After', '2')
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)

            def unavailable(self):
                body = json.dumps({'service': 'muli-sorter-console',
                    'status': 'initializing', 'ready': False,
                    'worker_running': False, 'phase': owner.phase,
                    'elapsed_seconds': round(time.monotonic()-owner.started, 3)},
                    ensure_ascii=False).encode()
                self.reply(503, body, 'application/json; charset=utf-8')

            def do_GET(self):
                if self.path == '/':
                    self.reply(200, PAGE, 'text/html; charset=utf-8')
                else:
                    self.unavailable()

            do_HEAD = do_GET
            do_POST = unavailable
            do_PUT = unavailable
            do_PATCH = unavailable
            do_DELETE = unavailable

        super().__init__(address, Handler)

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever,
                                       kwargs={'poll_interval': .1},
                                       name='archive-startup-http', daemon=True)
        self.thread.start()
        diagnostic('核对任务和历史记录', self.started)
        return self

    def attach(self, handler, close):
        if self.application_handler is not None:
            raise RuntimeError('startup_application_already_attached')
        self.application_handler = handler
        self.application_close = close

    def set_phase(self, phase):
        self.phase = phase
        diagnostic(phase, self.started)

    def activate(self):
        if self.application_handler is None:
            raise RuntimeError('startup_application_not_ready')
        # Existing startup requests remain read-only. Only new connections
        # receive the original fully initialized application handler.
        self.RequestHandlerClass = self.application_handler
        diagnostic('索引检查完成，启动归档服务', self.started)

    def wait(self):
        self.thread.join()

    def stop_serving(self):
        if self.thread is not None and self.thread.is_alive():
            self.shutdown()
            self.thread.join()

    def server_close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.application_close is not None:
                self.application_close()
        finally:
            super().server_close()

    def __exit__(self, *exc):
        self.stop_serving()
        self.server_close()
