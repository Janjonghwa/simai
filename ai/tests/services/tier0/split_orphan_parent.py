"""판정 프로세스 대역(S15P31A101-728 시험): 검출 프로세스를 띄우고 그 pid 를 한 줄 찍은 뒤 기다린다. 시험이 이 프로세스를 죽인다."""

from __future__ import annotations

from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
for p in (HERE.parents[2], HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def main() -> None:
    from services.tier0.detect_process import DetectorProcess
    from split_fakes import scripted_spec

    process = DetectorProcess(scripted_spec(), now_ms=lambda: time.time_ns() // 1_000_000, mono_s=time.monotonic)
    process.start()
    process.start_streaming()
    print(process.status()["pid"], flush=True)
    time.sleep(120)


if __name__ == "__main__":
    main()
