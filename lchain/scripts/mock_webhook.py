"""模拟企微发送端（crash_test 使用）：监听 127.0.0.1:8099，把收到的每一次投递追加到 /tmp/wh.jsonl。"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with open("/tmp/wh.jsonl", "a") as f: f.write(body.decode() + "\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", 8099), H).serve_forever()
