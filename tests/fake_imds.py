"""A fake EC2 metadata service on 169.254.169.254:80 (the IMDSv2 token, and the instance id with or without it): what
tests/test_gvisor_imds.py needs where there is no EC2 instance. The `gvisor` job in ci.yml runs it in a network namespace."""
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN, INSTANCE_ID = "fake-token", "i-0123456789abcdef0"


class H(BaseHTTPRequestHandler):
    def send(self, body):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_PUT(self):
        self.send(TOKEN) if self.path == "/latest/api/token" else self.send_error(404)

    def do_GET(self):
        self.send(INSTANCE_ID) if self.path == "/latest/meta-data/instance-id" else self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("169.254.169.254", 80), H).serve_forever()
