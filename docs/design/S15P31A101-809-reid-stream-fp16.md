# Re-ID CUDA 스트림과 정밀도 (S15P31A101-809)

명세 3.2·5.2의 Tier 0 우선 및 기하 게이트 뒤 보조 비용 원칙을 유지한다. I1, 위치·ID 정본과 재입장 임계값 0.65·margin 0.10은 바꾸지 않는다.

COPE CUDA 추론은 최저 우선순위(0) 전용 스트림에서 입출력 복사와 추론을 처리하고 해당 스트림만 기다린다. FP16 autocast 결과는 float32로 변환해 L2 정규화한다. CPU와 주입 모델은 기존 경로를 쓴다. CUDA 우선순위는 실행 중인 커널을 선점한다는 보장이 없으며 복사가 실제로 겹치는지도 하드웨어에 따라 다르다.

FP16은 실제 COPE·운영 영상의 정확도 및 지연 근거가 아직 없어 기본 끔이다. `SIMAI_TIER0_REID_FP16=1`로 켠다. GPU 시험은 같은 입력의 FP32 대비 `1 - cosine ≤ 0.01`을 검사한다. 이는 초기 회귀 한계이지 재입장 정확도 보장은 아니다. 불안정 주기 750 ms, 안정 주기 3000 ms, 상한 8 crops/s도 유지한다. 재입장 지연이 늘지 않는다는 근거 없이 제안값 1200 ms·5/s로 변경하지 않았다. 각각 `SIMAI_TIER0_REID_UPDATE_INTERVAL_MS`, `SIMAI_TIER0_REID_MAX_CROPS_PER_SECOND`로 조정하며 환경변수가 설정 파일보다 우선한다.

구현은 `ai/src/simai_ai/reid/`와 `ai/services/tier0/`, 시험은 `ai/tests/reid/` 및 `ai/tests/services/tier0/test_reid_settings.py`에 있다. GPU에서 실제 가중치의 FP16 차이와 재입장 정확도, YOLO 동시 실행 시 Tier 0 p95/p99 지연 및 처리량을 측정한 뒤 기본값 변경을 판단한다.
