# R1 보정 상태별 넓은 불확실성 (S15P31A101-233)

명세 4.4 보정 상태 표의 PROVISIONAL 행 "R1은 넓은 불확실성으로 동작"을 구현한다. -218 이 판정 문맥 `RuleContext.calibration_state` 와
R2·R3·R4 의 `PROVISIONAL_CALIB` 를 넣었고, R1 은 그때까지 보정 상태를 읽지 않았다.

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 4.4 보정 상태 | PROVISIONAL: R1 넓은 불확실성, R2·R3·R4 확인 필요. DRIFTED 는 PROVISIONAL 과 같게, UNCALIBRATED 판정은 방어적으로. PLANNED 해당 없음 | 넓히는 상태와 제외(CALIBRATED·None) |
| 4.4 CALIBRATED 기준 | 카메라 쌍별 위치 불일치 최대 15 cm, 머리 높이 오차 최대 15 cm, 바닥 평면 차이 3 cm, 스케일 근거 | 보정 여유 수치의 근거 |
| 4.3 스케일 근거 | 실측 정물이 없으면 스케일 정확도 2~5% | 수치 근거(교차 확인) |
| 7.1 불확실성 범위 | 수평 공분산 k=2 타원 + 점유 반경 0.3 m. 발생은 범위가 조금이라도 걸리면, 해소는 범위까지 완전히 벗어나 유지되면 | 넓힘 방식 |
| 7.1 확인 필요 | `PROVISIONAL_CALIB` 는 R2·R3·R4 에만(계약 `confidence_reason` 설명도 같음) | R1 에 붙이지 않는 이유 |
| 7.2 R1 | 0.3 m 바깥에서 3초 유지로 해소 | 해소 기준은 그대로 |

## 정의

보정이 CALIBRATED 가 아니면(`PROVISIONAL`·`DRIFTED`·`UNCALIBRATED`) R1 은 불확실성 범위를 **보정 여유 0.3 m** 만큼 넓힌다.

- 수평: 점유 반경에 더한다(범위 = k=2 타원 ⊕ 반경 0.3 + 0.3 m). 접지점 XY 범위도 같다.
- 높이(VOLUME_3D): 점유 높이의 아래 끝(서 있는 면 Z − k·σ)과 위 끝(관절·head_z + k·σ)을 0.3 m 씩 더 넓힌다. 높이를 모르면
  넓힘과 관계없이 지금처럼 `Z_UNKNOWN` 경로다(넓힘이 확인 필요를 지우지 않는다).
- 발생(겹침)·유지 띠(해소 간격 0.3 m)·해소 유지 3초는 그대로다. 그래서 발생은 0.3 m 더 바깥에서, 해소는 넓힌 범위가 0.3 m
  더 벗어나야 시작된다. 확률 수치는 쓰지 않는다(등급 그대로).
- CALIBRATED 와 보정 상태 없음(None, PLANNED 계획 위치)은 넓히지 않고 결과가 전과 값까지 같다(시험으로 확인).
- 넓힌 판정의 근거 코드(`reasons` 끝)에 `calibration_provisional` / `calibration_drifted` / `calibration_uncalibrated`, 수치에
  `calibration_margin_m`(0.3)을 붙인다. `rule_metrics` 는 이름이 자유라 계약 변경이 없다.

코드: `ai/src/simai_ai/risk/rules/r1_zone_entry.py`. 보정 여유와 넓힘 상태는 R1 규칙 인스턴스 설정(`calibration_margin_m`, `calibration_widened_states`, 기본값 `R1_CALIBRATION_MARGIN_M`·`R1_WIDENED_STATES`)이라 `rule_catalog` 가 수집해 규칙 값 시드(`server/src/main/resources/db/migration/R__rule_versions.sql`, R1 v1 의 `calibration_margin`·`calibration_widened_states`)에 적는다. 시드는 생성기(`python -m simai_ai.risk.rule_catalog --write`)로 다시 만들었고 최신성 시험(`test_committed_seed_is_up_to_date`)이 이 값을 본다.

## 수치 근거 (0.3 m)

명세에 수치가 없어 정했다. **0.3 m 는 측정된 보정 오차 σ 가 아니라 설계 가정에서 나온 초기 안전 여유다.** PROVISIONAL·DRIFTED 오차가 0.3 m 안이라는 보장 상한도, 통계적으로 검증된 95% 범위도 아니다.

1. CALIBRATED 기준(4.4)은 설치 직후 독립 검산에서 카메라 쌍별 위치 불일치와 머리 높이 오차를 각각 **최대 15 cm** 까지 허용한다.
   PROVISIONAL 은 이 기준을 통과하지 못했거나 아직 재지 않은 보정이고, DRIFTED 는 통과했던 보정이 틀어진 것이라 오차가 15 cm 를
   넘을 수 있다는 것만 안다. 이 15 cm 는 소수 검증점의 최대 오차 한도이지 표준편차 측정값이 아니지만, 이를 보정 오차의 σ_cal 로 **가정**하고 7.1 과 같은 k=2 를 곱해 **0.3 m** 로 둔다.
2. 교차 확인: 실측 정물이 없으면 스케일 정확도 2~5%(4.3). 교실(9 x 7.5 m)에서 World 원점·카메라에서 사람까지 3~6 m 면 스케일
   오차만으로 6~30 cm 다(3~6 m 는 교실 크기에서 정한 거리 가정이다). 0.3 m 는 이 범위의 위쪽 끝이다.
   - **G1·G2 재검토 기준.** 개발 교실 실측(G1·G2)에서 PROVISIONAL 보정으로 잰 위치 오차(카메라 쌍별 불일치·머리 높이 오차·절대 위치)의
     p95 를 구해, 0.3 m 보다 크면 여유를 그 p95 로 늘리고, 0.15 m 보다 작으면 줄이는 것을 검토한다. 바꾼 값은 규칙 값이라 첫 운영 배포
     뒤에는 새 R1 버전으로 둔다(11.4-1).
3. SIM 상한(-214 `extrinsics-from-recording.md`): 교란 2°·0.15 m 에서 번들 조정 뒤 카메라 높이 오차 1.3~1.7 cm 지만 판정은
   PROVISIONAL(SIM·스케일 MODEL_ESTIMATE)이다. 정답 데이터 결과라 상한이며(규칙 7) 실제 PROVISIONAL 오차의 근거로 쓰지 않는다.
4. 고르지 않은 것: 공분산에 σ_cal 을 제곱합으로 더하는 방식(아래 '버린 대안').

## R1 에 PROVISIONAL_CALIB 를 붙이지 않는 이유

명세 4.4·7.1 과 계약 `confidence_reason` 설명이 모두 "R2·R3·R4 에만"이다. R1 은 보정 오차를 범위에 넣어 판정 자체를 보수적으로
바꾸므로 "확인 필요"로 넘기지 않는다. R2·R3·R4 는 높이 기준(서 있는 층, 머리 높이 변화) 판정이라 수평 범위를 넓히는 것으로 보정
오차를 담지 못해 확인 필요를 붙인다. R1 에도 붙이려면 계약 의미 변경(메이저)과 Lead 승인이 필요하다. 대신 근거 코드와 수치로
보정 상태가 드러나게 했다.

## 명세 변경 (v1.3 수정 25)

- 4.4 표: UNCALIBRATED 행(방어적으로 R1 넓은 불확실성), DRIFTED 행(R1 넓은 불확실성)을 더했다. 표 아래 문단에 R1 은 같은 상태에서
  범위를 넓히고 등급은 바꾸지 않으며 PLANNED 는 넓히지 않는다는 문장을 더했다.
- 7.1: "R1 넓은 불확실성" 문단(대상 상태, 0.3 m, 수평·높이, 근거, 확인 필요 없음).
- 요구사항 갱신 목록에 FR-66.
- 11.1·11.4-1: 첫 운영 배포 전 규칙 보완 예외(Lead 승인 2026-10-22). AGENTS.md 규칙 6 에 그 예외 구절.

## 시험

| 위치 | 내용 |
|---|---|
| `ai/tests/risk/test_r1_calibration_margin.py` | 경계값(0.70 m 정확히 닿음 발생, 0.701 유지, 1.0 m 밖 없음), CALIBRATED·None 음성 대조(기본 판정과 같음, 여러 판정 경로), VOLUME_3D 위·아래 끝, Z 모름 유지, UNKNOWN 트랙 미판정, PREDICTED, DRIFTED·UNCALIBRATED, 생명주기(발생 +300 vs +500 ms, 해소 +4900 vs +4800 ms), 상태 전환 반례 2개 |
| `ai/tests/risk/vectors/r1_boundary_provisional_calib.json` | 공통 벡터 1.5: PROVISIONAL 경계 진입·퇴장 생명주기 정답 |
| `ai/tests/risk/test_r1_calibration_margin.py`(고정 기대) | CALIBRATED·None 결과를 손으로 적은 고정 Finding 값과 비교(공통 경로가 함께 바뀌는 회귀), 카탈로그에 보정 여유·넓힘 상태, 생성자 인자 검증 |
| `ai/tests/risk/test_provisional_calib.py` | R1 을 '보정 상태 무관' 목록에서 뺐다(R5·R7 만) |
| `ai/tests/scenarios/*` | SIM 보정이 PROVISIONAL 이라 R1 발생 약 0.2 s 이름·해소 약 0.1~0.2 s 늦음. 발생 하한을 넓힌 범위 기준으로 다시 정했다(`scenario-replay-golden.md`). 넓힘을 끄면 실패하는 음성 대조 |

## 버린 대안

- **σ 에 σ_cal 제곱합.** 보정 오차는 사람마다·프레임마다 독립인 잡음이 아니라 같은 방향으로 치우친 계통 오차라, 제곱합은 σ 가 클 때
  여유를 거의 더하지 않는다(σ 0.3 m 면 k=2 로 +0.07 m). 경계 근처 진입을 이르게 잡으려는 목적에 맞지 않아 더하기(Minkowski 합)로 했다.
- **해소 간격만 넓힘.** 해소만 늦어지고 발생이 이르지 않다. 명세 "넓은 불확실성"은 범위 자체다.
- **R1 v2 로 버전 올림.** 11.4-1 은 규칙 값을 바꾸면 새 버전을 둔다. Lead 결정(2026-10-22, A): 첫 실제 운영 배포 전에는 지킬 운영
  판정 기록(운영 DB·배포 데이터)이 없으므로 규칙 동작을 같은 버전 안에서 보완할 수 있고, 첫 배포부터는 동작이 바뀌면 반드시 새
  버전이다(명세 11.4-1 예외 문장). 그래서 R1 v1 을 보완하고 CALIBRATED 결과(rule_version 포함)를 그대로 둔다.
- **보정 여유를 모듈 상수로만 둠(1차 구현).** 카탈로그(`vars()` 수집)에서 빠져 규칙 값 시드에 보정 여유가 없었다(리뷰 F1). 인스턴스
  설정으로 옮겨 시드에 적었다.

## 잔여 위험

- 0.3 m 는 실측 전 설계 가정의 초기 안전 여유다. PROVISIONAL 보정의 실제 위치 오차(G1·G2 실측, 위 재검토 기준)로 다시 정한다.
- 같은 R1 v1 이벤트라도 보정 상태에 따라 범위가 다르다. 이벤트의 `metrics.calibration_margin_m`·근거 코드로 구분한다.
- R5(진입 예측)는 R1 과 같은 범위를 쓰지만 넓히지 않았다(명세 4.4 는 R1 만). R5 가 R1 에 맡기는 시점(관측 범위가 구역에 걸림)은
  넓히지 않은 범위 기준이라, PROVISIONAL 에서는 R1 발생과 R5 활성이 조금 더 겹칠 수 있다. R6 holding 도 넓히지 않는다(관측 상실 중
  해소는 lifecycle 이 막는다).
- 넓히면 PROVISIONAL 동안 경계 근처 오경보가 늘 수 있다(안전 쪽 선택). G6 오경보 측정은 CALIBRATED 기준으로 한다.

## 넘긴 일

- 첫 운영 배포 MR 이 배포 시점을 개정 이력에 기록한다(그 뒤로는 R1 값·동작 변경이 새 버전, 11.4-1).
- R5 를 같은 여유로 넓힐지: 명세 결정 필요.
