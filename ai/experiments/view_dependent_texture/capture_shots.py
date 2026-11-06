"""시점 의존 텍스처(S15P31A101-527) 3단계 자동 캡처 및 브라우저 FPS 측정 스크립트.

로컬 HTTP 서버를 띄우고 헤드리스 Chrome(WebGL 하드웨어 가속)으로 `index.html?autoshots=1` 을 열어:
1. `cam_A`~`cam_D`, `top`(위에서 보기), `obl1`~`obl3`(비스듬히 3곳) 총 8개 시점의 [원래 | 시점 의존] 비교 이미지를
   `C:/Users/SSAFY/simai-local/vdt/shots/` 에 저장한다.
2. 주요 시점(`top`, `obl1`, `obl2`, `cam_A`)의 카메라별 가중치 시각화 이미지(`*_weights.jpg`)를 함께 저장한다.
3. 그림자 맵 해상도(1024, 1536, 2048)별 실제 브라우저 렌더 FPS 와 프레임 소요 시간(ms)을 측정해 `shots/metrics.json` 에 기록한다.
"""

from __future__ import annotations

import argparse
import base64
import functools
import http.server
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_VDT_DIR = Path("C:/Users/SSAFY/simai-local/vdt")
CHROME_CANDIDATES = [
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
]


def find_browser() -> Path:
    for p in CHROME_CANDIDATES:
        if p.is_file():
            return p
    raise FileNotFoundError("Chrome 또는 Edge 실행 파일을 찾을 수 없다.")


def run_capture(vdt_dir: Path, timeout_s: float = 150.0) -> dict[str, Any]:
    here = Path(__file__).resolve().parent
    for fname in ("index.html", "vdt.js"):
        src = here / fname
        if src.is_file():
            shutil.copy2(src, vdt_dir / fname)

    shots_dir = vdt_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    done_event = threading.Event()
    result_holder: dict[str, Any] = {}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))

            if self.path == "/__save_shot":
                filename = Path(payload["filename"]).name
                data_url: str = payload["dataUrl"]
                _, b64_data = data_url.split(",", 1)
                raw_bytes = base64.b64decode(b64_data)
                out_path = shots_dir / filename
                out_path.write_bytes(raw_bytes)
                print(f"[capture] 저장 완료: {out_path} ({len(raw_bytes) // 1024} KB)", flush=True)
                self._send_json({"ok": True})
                return

            if self.path == "/__done":
                result_holder.update(payload)
                metrics_path = shots_dir / "metrics.json"
                metrics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[capture] 성능 측정 저장: {metrics_path}", flush=True)
                self._send_json({"ok": True})
                done_event.set()
                return

            self.send_response(404)
            self.end_headers()

        def _send_json(self, obj: dict[str, Any]) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    handler_cls = functools.partial(Handler, directory=str(vdt_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    browser = find_browser()
    url = f"http://127.0.0.1:{port}/index.html?autoshots=1"
    print(f"[capture] 브라우저 실행: {browser.name} -> {url}", flush=True)

    with tempfile.TemporaryDirectory(prefix="vdt_chrome_", ignore_cleanup_errors=True) as profile_dir:
        cmd = [
            str(browser),
            "--headless=new",
            f"--user-data-dir={profile_dir}",
            "--window-size=1900,1000",
            "--enable-webgl",
            "--ignore-gpu-blocklist",
            "--disable-gpu-vsync",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            ok = done_event.wait(timeout=timeout_s)
            if not ok:
                raise TimeoutError(f"{timeout_s}초 안에 캡처가 끝나지 않았다.")
            time.sleep(0.3)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            server.shutdown()
            server.server_close()

    return result_holder


def main() -> None:
    parser = argparse.ArgumentParser(description="시점 의존 텍스처(VDT) 확인 이미지 캡처 및 FPS 측정")
    parser.add_argument("--vdt-dir", type=Path, default=DEFAULT_VDT_DIR)
    args = parser.parse_args()
    res = run_capture(args.vdt_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
