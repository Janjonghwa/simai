"""nvidia-smi 대역. 노트북(GPU 없음)에서 GPU 정보 기록과 VRAM 확인을 시험한다.

첫 인자가 파일 경로(--로 시작하지 않음)면 그 파일의 숫자를 사용 중 메모리(MB)로 읽는다(시험 도중 바꿔 VRAM 이 비는 순간을 만든다).
--query-gpu 는 요청한 필드 순서대로 답한다(S15P31A101-205). 기본은 H100 80GB, 환경 변수로 바꾼다:
FAKE_SMI_NAME, FAKE_SMI_TOTAL_MB, FAKE_SMI_CC(compute_cap), FAKE_SMI_DRIVER, FAKE_SMI_CUDA, FAKE_SMI_FAIL=1(GPU 없음, 종료 9).
"""

import os
import sys

args = sys.argv[1:]
used = "1024"
if args and not args[0].startswith("-"):
    used = open(args.pop(0), encoding="utf-8").read().strip()
env = os.environ.get
if env("FAKE_SMI_FAIL"):
    print("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.")
    sys.exit(9)
driver = env("FAKE_SMI_DRIVER", "550.54.15")
fields = {"name": env("FAKE_SMI_NAME", "NVIDIA H100 80GB HBM3"), "driver_version": driver,
          "memory.total": env("FAKE_SMI_TOTAL_MB", "81559"), "memory.used": used, "compute_cap": env("FAKE_SMI_CC", "9.0")}
query = next((a.split("=", 1)[1] for a in args if a.startswith("--query-gpu=")), None)
if query is not None:
    print(", ".join(fields.get(f.strip(), "[N/A]") for f in query.split(",")))
else:
    print(f"| NVIDIA-SMI {driver}    Driver Version: {driver}    CUDA Version: {env('FAKE_SMI_CUDA', '12.4')} |")
