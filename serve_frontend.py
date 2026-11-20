"""
Windows에서 python -m http.server를 사용하면 시스템 MIME 설정에 따라 .js 파일이 'text/plain'으로 제공될 수 있습니다.
브라우저는 ES 모듈을 실행하지 못해 헤더만 보일 수 있으므로 이 스크립트를 사용하세요.
"""

from __future__ import annotations

import argparse
import mimetypes
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


class FrontendHandler(SimpleHTTPRequestHandler):
    """MIME 오류 응답이나 이전 스크립트가 캐시에 남지 않게 한다."""

    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript"}

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Price Adjust Planner frontend")
    parser.add_argument("port", nargs="?", type=int, default=5500)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    mimetypes.add_type("text/javascript", ".js", strict=True)
    handler = partial(FrontendHandler, directory=str(FRONTEND_DIR))
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        print(f"Frontend: http://{args.host}:{args.port}/")
        print("Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
