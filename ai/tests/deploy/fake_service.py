"""simai-bootstrap 감시 프로세스 시험용 가짜 Tier 0·vLLM·워커 (S15P31A101-246). GPU·모델 없이 기동·종료 순서와 그룹 정리를 본다.

    python fake_service.py tier0|vllm|worker [받은 인자...]

시험(test_simai_supervisor.py)과 이미지 점검(deploy/scripts/check_image.sh 통합 이미지)이 같이 쓴다. 표준 라이브러리만 쓴다.
환경 변수:
  FAKE_EVENTS                     사건 기록 파일. 줄마다 '<역할> <사건> <pid> <unix 초>'(추가만)
  SIMAI_ROOT                      tier0: state/tier0.live 에 준비된 표시 파일(gpu_priority phase=running·reserve OK·pid·written_at)을 0.5초마다 쓴다
  FAKE_TIER0_READY_AFTER_S        tier0: 이 시간 뒤에 표시 파일을 쓰기 시작한다(엔진 준비 대역, 기본 0). 시작할 때 'marker' 사건을 남긴다
  FAKE_<역할>_EXIT_AFTER_S        그 시간 뒤 스스로 끝난다(종료 코드 FAKE_<역할>_EXIT_CODE, 기본 1). 손자는 남긴다(그룹 정리 확인)
  FAKE_VLLM_HEALTH_AFTER_S        vllm: 이 시간 뒤에 /health 가 200 (기본 0.5)
start 줄에는 HF 토큰이 환경에 있었는지(token|clean)와 OMP_NUM_THREADS 를 남긴다. vllm 은 손자 프로세스 pid 를 'grandchild <pid>' 로 남긴다.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROLE = sys.argv[1]
T0 = time.time()
stop = threading.Event()


def ev(what: str) -> None:
    with open(os.environ["FAKE_EVENTS"], "a", encoding="utf-8") as f:
        f.write(f"{ROLE} {what} {os.getpid()} {time.time():.3f}\n")


def on_term(*_a) -> None:
    ev("term")
    stop.set()


signal.signal(signal.SIGTERM, on_term)
token = "token" if {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"} & set(os.environ) else "clean"
ev(f"start {token} omp={os.environ.get('OMP_NUM_THREADS', '-')} argv={json.dumps(sys.argv[2:])}")
print(f"{ROLE} says hello", flush=True)  # 감시 프로세스의 [역할] 접두·로그 파일 확인용

marker = None
if ROLE == "tier0":
    marker = Path(os.environ["SIMAI_ROOT"]) / "state" / "tier0.live"
    marker.parent.mkdir(parents=True, exist_ok=True)

    def beat() -> None:
        if stop.wait(float(os.environ.get("FAKE_TIER0_READY_AFTER_S") or 0)):  # 진입점의 엔진 준비(재빌드 약 550초) 대역
            return
        ev("marker")
        while not stop.is_set():
            tmp = marker.with_suffix(".tmp")
            gp = {"phase": "running", "pid": os.getpid(), "written_at": time.time_ns() // 1_000_000, "reserve": {"state": "OK"}}
            tmp.write_text(json.dumps({"gpu_priority": gp}), encoding="utf-8")
            os.replace(tmp, marker)
            stop.wait(0.5)

    threading.Thread(target=beat, daemon=True).start()
elif ROLE == "vllm":
    port = int(sys.argv[sys.argv.index("--port") + 1])
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])  # vLLM EngineCore 대역
    ev(f"grandchild {kid.pid}")
    ready_at = T0 + float(os.environ.get("FAKE_VLLM_HEALTH_AFTER_S") or 0.5)

    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 이름
            ok = self.path == "/health" and time.time() >= ready_at
            self.send_response(200 if ok else 503)
            self.end_headers()

        def log_message(self, *_a) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", port), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

exit_after = os.environ.get(f"FAKE_{ROLE.upper()}_EXIT_AFTER_S")
while not stop.wait(0.05):
    if exit_after and time.time() - T0 >= float(exit_after):
        ev("crash")
        os._exit(int(os.environ.get(f"FAKE_{ROLE.upper()}_EXIT_CODE") or 1))  # 손자를 남기고 끝난다
if marker is not None:
    marker.unlink(missing_ok=True)
ev("exit")
sys.exit(0)
