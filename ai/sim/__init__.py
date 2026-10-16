"""결정적 가상 교실 시뮬레이터 (명세 5.1 입력 추상화, 21.3 시나리오 재생).

시드를 고정한 가상 교실에서 사람 궤적(관절 3D)과 가상 카메라 4대를 만들고,
카메라별 2D keypoint·촬영 시각, 정답 World 좌표, 가상 보정값(calibration 계약)을 파일로 낸다.
표준 라이브러리만 쓴다. simai_ai 는 이 패키지를 import 하지 않는다.

실행: ai/ 에서 `uv run python -m sim.run --scenario basic --out sim/runs/basic`
"""

__version__ = "0.1.0"
