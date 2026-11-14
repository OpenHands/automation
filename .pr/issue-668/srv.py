from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
        self.wfile.write(b'{"version": "1.62.0"}')
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1", 18668), H).serve_forever()
