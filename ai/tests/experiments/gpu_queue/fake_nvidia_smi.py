"""nvidia-smi 대역. 노트북(GPU 없음)에서 GPU 정보 기록과 VRAM 확인을 시험한다."""

import sys

if any(a.startswith("--query-gpu") for a in sys.argv[1:]):
    print("NVIDIA H100 80GB HBM3, 550.54.15, 81559, 1024")
else:
    print("| NVIDIA-SMI 550.54.15    Driver Version: 550.54.15    CUDA Version: 12.4 |")
