import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    return int(raw)


REDIRECT_BIND_HOST = os.getenv("UI_REDIRECT_HOST", "0.0.0.0")
REDIRECT_BIND_PORT = _env_int("UI_REDIRECT_PORT", 80)
REDIRECT_TARGET_HOST = os.getenv("UI_REDIRECT_TARGET_HOST", "127.0.0.1")
REDIRECT_TARGET_PORT = _env_int("UI_REDIRECT_TARGET_PORT", 443)


class RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._redirect()

    def do_HEAD(self) -> None:
        self._redirect(send_body=False)

    def do_POST(self) -> None:
        self._redirect()

    def do_PUT(self) -> None:
        self._redirect()

    def do_PATCH(self) -> None:
        self._redirect()

    def do_DELETE(self) -> None:
        self._redirect()

    def do_OPTIONS(self) -> None:
        self._redirect()

    def _redirect(self, send_body: bool = True) -> None:
        host = REDIRECT_TARGET_HOST
        if REDIRECT_TARGET_PORT == 443:
            location = f"https://{host}{self.path}"
        else:
            location = f"https://{host}:{REDIRECT_TARGET_PORT}{self.path}"

        self.send_response(308)
        self.send_header("Location", location)
        self.send_header("Connection", "close")
        self.end_headers()
        if send_body:
            self.wfile.write(f"Redirecting to {location}\n".encode("utf-8"))

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((REDIRECT_BIND_HOST, REDIRECT_BIND_PORT), RedirectHandler)
    print(
        f"[redirect] listening on http://{REDIRECT_BIND_HOST}:{REDIRECT_BIND_PORT} "
        f"-> https://{REDIRECT_TARGET_HOST}:{REDIRECT_TARGET_PORT}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
