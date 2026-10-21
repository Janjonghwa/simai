"""Risk Engine 컨테이너 시험 공용: services 는 설치 대상이 아니라(ai/ 작업 디렉터리에서 실행) ai/ 를 import 경로에 넣는다."""

import sys
from pathlib import Path

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))
