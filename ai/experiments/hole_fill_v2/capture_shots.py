"""Headless Chrome/Edge(WebGL 하드웨어 가속)로 8개 시점 4칸 비교 스크린샷을 캡처한다.

캡처 시점 (8곳, 1900x1000):
- 카메라 시점 4곳: `cam_A`, `cam_B`, `cam_C`, `cam_D`
- 자유 시점 4곳: `top`, `obl1`, `obl2`, `obl3`

출력 (`C:/Users/SSAFY/simai-local/hole-fill-v2/shots/`):
- `compare_{view}.jpg` (4칸: ① 12차 원본 | ② 방안 a 자연 톤 | ③ 방안 b LaMa | ④ 방안 c 고전)
- `tone_vs_grid_{view}.jpg` (2칸: 방안 a 자연 톤 | 방안 a-grid 1m 격자, top/obl1)
- `compare_obl1_pbr_standard.jpg` (순수 MeshStandardMaterial PBR 조명 검증 캡처)
"""

from __future__ import annotations

import argparse
import base64
import functools
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_OUT_DIR = Path("C:/Users/SSAFY/simai-local/hole-fill-v2")
DEFAULT_VDT_DIR = Path("C:/Users/SSAFY/simai-local/vdt")

BROWSER_CANDIDATES = [
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
]


def find_browser() -> Path:
    for p in BROWSER_CANDIDATES:
        if p.is_file():
            return p
    raise FileNotFoundError("Chrome 또는 Edge 실행 파일을 찾을 수 없다.")


def run_capture(
    data_dir: Path,
    vdt_dir: Path,
    timeout_s: float = 150.0,
) -> dict[str, Any]:
    here = Path(__file__).resolve().parent
    data_dir = data_dir.resolve()
    vdt_dir = vdt_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    # index.html, viewer.js, cameras.json 을 data_dir 에 동기화해 단일 HTTP 루트에서 서빙
    for fname in ("index.html", "viewer.js"):
        src = here / fname
        if src.is_file():
            shutil.copy2(src, data_dir / fname)

    cam_dst = data_dir / "cameras.json"
    if not cam_dst.is_file() and (vdt_dir / "cameras.json").is_file():
        shutil.copy2(vdt_dir / "cameras.json", cam_dst)

    shots_dir = data_dir / "shots"
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
                print(
                    f"[capture] 저장 완료: {out_path.name} ({len(raw_bytes) / 1024:.1f} KB)",
                    flush=True,
                )
                self._send_json({"ok": True})
                return

            if self.path == "/__done":
                result_holder.update(payload)
                metrics_path = shots_dir / "shots_manifest.json"
                metrics_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"[capture] 전체 캡처 완료: {metrics_path}", flush=True)
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

    handler_cls = functools.partial(Handler, directory=str(data_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()

    browser = find_browser()
    url = f"http://127.0.0.1:{port}/index.html?autoshots=1"
    print(f"[capture] 브라우저 실행: {browser.name} -> {url}", flush=True)

    with tempfile.TemporaryDirectory(
        prefix="hole_fill_v2_chrome_", ignore_cleanup_errors=True
    ) as profile_dir:
        cmd = [
            str(browser),
            "--headless=new",
            f"--user-data-dir={profile_dir}",
            "--window-size=1900,1000",
            "--force-device-scale-factor=1",
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
    parser = argparse.ArgumentParser(
        description="GLB 빈 곳 채우기 2차(S15P31A101-532) 8시점 4칸 비교 캡처"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("SIMAI_HOLE_FILL_V2_OUT", str(DEFAULT_OUT_DIR))),
    )
    parser.add_argument(
        "--vdt-dir",
        type=Path,
        default=Path(os.environ.get("SIMAI_VDT_DIR", str(DEFAULT_VDT_DIR))),
    )
    args = parser.parse_args()
    res = run_capture(args.data_dir, args.vdt_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
