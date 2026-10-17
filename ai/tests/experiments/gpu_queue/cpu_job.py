"""큐 시험용 CPU Job. GPU 를 쓰지 않는다.

  ok NAME OUT           OUT 에 NAME 을 한 줄 붙이고 $SIMAI_RUN_DIR/metrics.json 을 남긴다
  fail CODE             종료 코드 CODE 로 끝난다
  hang PIDFILE          손자 프로세스를 띄워 pid 를 PIDFILE 에 쓰고 오래 잔다(타임아웃 시험)
  escape PIDFILE        새 세션(setsid)으로 손자를 띄워 pid 를 쓰고 바로 정상 종료한다(자손 정리 시험)
  slow-if FLAG NAME OUT FLAG 파일이 있으면 FLAG.sleeping 을 쓰고 오래 자고, 없으면 ok 와 같다(재시작·LIVE 시험)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def ok(name: str, out: str) -> None:
    with open(out, "a", encoding="utf-8") as f:
        f.write(name + "\n")
    run_dir = Path(os.environ["SIMAI_RUN_DIR"])
    (run_dir / "metrics.json").write_text(json.dumps({"sample_value": 1.5, "label": name, "nested": {"x": 1}}), encoding="utf-8")


def main() -> int:
    mode, *args = sys.argv[1:]
    if mode == "ok":
        ok(*args)
        return 0
    if mode == "fail":
        print("의도한 실패", flush=True)
        return int(args[0])
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        Path(args[0]).write_text(str(child.pid), encoding="utf-8")
        time.sleep(120)
        return 0
    if mode == "escape":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
        Path(args[0]).write_text(str(child.pid), encoding="utf-8")
        return 0
    if mode == "slow-if":
        flag, name, out = args
        if Path(flag).exists():
            Path(flag + ".sleeping").write_text(str(os.getpid()), encoding="utf-8")  # 시험이 기다리는 표시
            time.sleep(120)
        ok(name, out)
        return 0
    raise SystemExit(f"모르는 모드: {mode}")


if __name__ == "__main__":
    sys.exit(main())
