"""nvidia-smi 대역. 노트북(GPU 없음)에서 GPU 정보 기록과 VRAM 확인을 시험한다.

첫 인자가 파일 경로(--로 시작하지 않음)면 그 파일의 숫자를 사용 중 메모리(MB)로 읽는다(시험 도중 바꿔 VRAM 이 비는 순간을 만든다).
"""

import sys

args = sys.argv[1:]
used = "1024"
if args and not args[0].startswith("-"):
    used = open(args.pop(0), encoding="utf-8").read().strip()
if any(a.startswith("--query-gpu") for a in args):
    print(f"NVIDIA H100 80GB HBM3, 550.54.15, 81559, {used}")
else:
    print("| NVIDIA-SMI 550.54.15    Driver Version: 550.54.15    CUDA Version: 12.4 |")
