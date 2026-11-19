# S15P31A101-807 사이드카 처리 부하 접수

명세 3.2·6.6. 구현은 `ai/src/simai_ai/gpu/admission.py`와 `ai/services/worker/__main__.py`, 시험은 `ai/tests/gpu`, `ai/tests/ppe`, `ai/tests/services/worker`에 있다. I1 계약과 Tier 0 판정은 바꾸지 않는다.

검출 150 ms·판정 200 ms·연속 10초는 아바타 기존 예산을 공유한다. `SIMAI_ADMISSION_DETECT_BUDGET_MS`, `SIMAI_ADMISSION_JUDGE_BUDGET_MS`, `SIMAI_ADMISSION_HOLD_S`로 조정하며 잘못된 값은 경고 후 기본값이다. 기존 600 ms 즉시 차단과 정상 10초 재개를 유지한다. 아바타 재개 뒤 즉시 재차단 보호는 10초이며 `SIMAI_AVATAR_REOPEN_GUARD_S`로 조정한다. 반복적인 10초 켜짐·꺼짐을 줄이기 위한 초기값이다.

워커는 실행 중 작업을 중단하지 않고 접수만 늦춘다. PPE는 처리 부하 차단·감속을 적용하지 않는다. 미착용 확정에 연속 6칸이 필요하고 빈 칸은 1개만 허용하므로 감속도 R9 발생을 막을 수 있다(Lead 결정 2026-11-19). GPU 실측에서 Tier 0 p95, 워커 접수 반복과 PPE 검출률을 확인하고 임계값을 조정해야 한다.
