# S15P31A101-855 R5 는 높이 모름 + 예측 위치로 새로 발생시키지 않는다

관련 명세: 7.1(불확실성 처리, 확인 필요), 7.2(R5 판정, R2·R3 접지 모름 정책), 6.4(지속 조건·지속 대기 관측 상실), 6.1(PREDICTED).
선례: S15P31A101-725(R2·R3 낮은 신뢰 새 발생 보류, 수정 80), S15P31A101-430·-431(R1 확실한 진입만, 수정 53), S15P31A101-833(R5 자세 전이 억제).
계약 변경 없음. `rule_version` 그대로(R5 v1·v1001, 11.4 첫 운영 배포 전 규칙 보완 예외, -725 와 같은 처리).

## 현상 (2026-11-23 15:33 리허설)

작업 시작(15:33:31 KST) 1.4초 뒤 R5 가 났다. confidence LOW, `confidence_reasons` {Z_UNKNOWN, PREDICTED, SINGLE_VIEW},
metrics {speed_mps 0.421, time_to_entry_s 0.0, zone_distance_m 2.156}. 실제로는 구역에서 2 m 넘게 떨어져 있었고 한 카메라로만
보이며 높이를 몰랐다. 42초 뒤 정상 R5(HIGH, reasons {}, speed 0.292, time_to_entry 0.0, zone_distance 0.438)는 맞는 경보다.

## 어디서 생겼나

R5 규칙은 PREDICTED 트랙을 판정하지 않는다(속도 모름 → holding). Z_UNKNOWN 은 VOLUME_3D 구역 대상에만, 규칙이 붙이는 PREDICTED 는
가장자리 대상(HELD 접지점)에만 붙어 **한 규칙 판정에 둘이 같이 오지 않는다.** 이 조합은 lifecycle 의 '지속 대기 중 관측 상실'
발생(6.4)으로만 생긴다: 관측 프레임에서 Z_UNKNOWN 발생 판정 1개가 대기를 시작하고, 다음 프레임에 위치가 예측(또는 UNKNOWN·트랙
없음·gap/lag 초과)이 되면 lifecycle 이 마지막 발생 판정에 PREDICTED 와 `observation_lost_before_persistence` 를 더해 바로 발생시킨다.
시험 `test_rehearsal_z_unknown_then_predicted_makes_no_event` 는 이 보류를 끄면 실패한다(리허설 경로 재현 확인).

## 정한 규칙

- R5 의 **새 발생(재개 포함)** 판정이 Z_UNKNOWN 과 PREDICTED 를 함께 가지면 내지 않는다(`r5_entry_prediction.r5_low_confidence_onset`,
  조합 `R5_LOW_CONFIDENCE_ONSET_REASONS`). 지금 이 조합이 생기는 곳은 위 lifecycle 경로뿐이라 `LifecycleTracker._persist` 의 관측 상실
  발생 직전에 걸러 대기를 버린다(`R5_LOW_CONFIDENCE` 진단 로그만, 이벤트·계약 필드 아님).
- 이미 열린 R5 는 `_advance` 로 가므로 유지·해소·확인 필요 이유가 그대로다(예측 위치가 와도 holding 으로 유지, 해소되지 않음).
- 둘 중 하나만이면 지금처럼 발생한다.
  - Z_UNKNOWN 만: 관측 위치의 Z_UNKNOWN 판정이 지속 조건(3프레임 중 2)을 채우면 확인 필요로 발생.
  - PREDICTED 만: 높이를 아는 대기가 관측을 잃으면 지금처럼 PREDICTED 를 더해 바로 발생(UNVERIFIABLE).
- 보류 뒤 관측 위치가 돌아와 지속 조건을 채우면 그때 발생한다(보류는 그 대기만 버린다).
- R1~R4·R6~R11 은 바꾸지 않았다.

### -725 판정을 그대로 부르지 않은 까닭

-725 의 `height_common.low_confidence_onset(track, basis)` 는 "이번 프레임 트랙이 PREDICTED 이고 서 있는 면 UNKNOWN" 을 규칙 안에서 본다.
R5 는 PREDICTED 트랙을 규칙에서 판정하지 않으므로 규칙 안에서는 이 조건이 참이 될 일이 없고, 문제 경로(lifecycle 관측 상실)에서는
트랙이 UNKNOWN 이거나 없을 수도 있다(DB 의 PREDICTED 는 lifecycle 이 상실 종류와 무관하게 붙인다). 그래서 같은 조합을 판정의 확인 필요
이유(Z_UNKNOWN + PREDICTED)로 본다. R5 의 Z_UNKNOWN 은 VOLUME_3D 에서 아래 끝(접지점) 또는 위 끝(VALID 관절·VALID head_z)을 모를 때라
-725 의 "접지점 없음" 보다 조금 넓다(위 끝만 모르는 대기도 관측 상실로는 발생하지 않는다). R5 는 CAUTION 예측이고 관측 위치로
지속 조건을 채우면 그대로 나므로 이 정도 확장은 받아들였다.

## time_to_entry_s 0.0 과 zone_distance_m 2.156 — 버그 아님

계산은 명세대로다(7.2 R5 "관측 범위는 안 걸리지만 판정 시각으로 옮긴 범위가 걸리면 선행 0초로 발생", `predicted_touching_now`).
- `time_to_entry_s` 는 **불확실성 범위**(k=2 타원 + 점유 반경 0.3 m)의 가장자리가 구역에 닿는 시각이고, `zone_distance_m` 은 **범위
  중심**(position_xy, 접지점이 있으면 그 XY 중 가까운 쪽)에서 구역까지 거리다(명세·규칙 머리말 정의). 둘의 차이가 범위 반경이다.
- 리허설 값으로 거꾸로 풀면: staleness ≤ 500 ms 에서 옮긴 거리 ≤ 0.421 × 0.5 ≈ 0.21 m, 관측 범위는 안 걸렸으므로
  2.156 − 0.21 ≤ 2σ + 0.3 < 2.156 → 접근 방향 σ ≈ 0.82~0.93 m. 한 카메라 깊이 방향 σ 로 그럴 만한 값이다.
- Z 를 몰라도 수평 판정 기준은 바뀌지 않는다. 수평 범위는 Z 와 무관하고, 접지점이 없으면 position_xy 범위 하나만 쓴다. VOLUME_3D 의
  높이 거르기만 빠지고 Z_UNKNOWN 이 붙는다(명세대로).
- 즉 "2 m 떨어졌는데 0초" 는 한 카메라의 큰 σ 로 범위가 2 m 넘게 퍼진 결과다. 이번 보류로 그 상태의 관측 상실 발생은 막지만,
  같은 큰 σ 의 관측 프레임이 지속 조건을 채우면(Z_UNKNOWN 만) 여전히 난다(남은 위험 1).

## 코드·시험 위치

- `ai/src/simai_ai/risk/rules/r5_entry_prediction.py`: `R5_LOW_CONFIDENCE_ONSET_REASONS`, `r5_low_confidence_onset`, 머리말 절.
- `ai/src/simai_ai/risk/lifecycle.py`: `_persist` 관측 상실 발생 직전 R5 보류, 머리말·docstring.
- `ai/tests/risk/test_r5_low_confidence_onset.py`: 실제 R5 규칙·엔진·생명주기로 (a) 15:33:32 형태 → 새 발생 없음(예측·UNKNOWN 이어져도),
  (b) 15:34:13 형태 → 발생(확인 필요 없음, 같은 수치), (c) Z_UNKNOWN 만 → 발생, PREDICTED 만(정상·리허설 위치에 높이만 앎) → 발생,
  (d) 열린 R5(Z_UNKNOWN·정상)는 접지 모름 + 예측 4초에도 해소 없이 유지, 보류 뒤 관측 복귀 시 발생, 조합 함수 단위 시험.

## 버린 대안

- R5 를 `pending_loss_exempt_rules` 에 넣어 관측 상실 발생 전체를 끄기: PREDICTED 만(높이 앎)도 막혀 "한 조건만이면 지금처럼" 과
  어긋난다. 다만 명세 7.2 R5 의 "지속 조건을 세는 중에 속도를 잃으면 이벤트 없이 사라진다" 를 PREDICTED 까지 읽으면 이쪽이 명세에
  더 가깝다(아래 명세 차이).
- R5 규칙 `_judge` 에 보류를 넣기: 규칙 판정에는 두 이유가 같이 오지 않아 효과가 없는 죽은 코드다.
- Z_UNKNOWN 만으로 막기: 리허설 뒤 정상 경로(관측 위치·높이 모름)도 막아 -725 선택지 C(가장 좁게)와 다르다.
- 큰 σ 의 범위 반경 상한·선행 0초 발생에 거리 상한 두기: 7.1 "조금이라도 걸리면" 과 R5 정의를 바꾸는 일이라 이번 범위 밖.

## 명세 차이 (보고)

명세 7.2 R5 는 "속도를 모르면 판정 제외(PREDICTED 포함) … 지속 조건을 세는 중에 속도를 잃으면 이벤트 없이 사라진다" 고 적는데,
코드는 위치가 PREDICTED·UNKNOWN 이 되면 lifecycle 이 6.4 관측 상실 처리로 바로 발생시킨다(-73 의 안전 쪽 처리, 속도만 잃고 위치가
관측되는 경우에만 이벤트 없이 사라짐). 이번 티켓은 지시대로 PREDICTED 만이면 지금 동작을 유지했다. 명세 문구를 "위치는 관측되는데
속도를 잃으면" 으로 고칠지, R5 관측 상실 발생을 아예 끌지는 명세 정리 때 정한다. 7.1 표의 R2·R3 접지 모름 문장에 R5 를 더하는 것도
명세 정리 MR 로 넘긴다.

## 남은 위험

1. **관측 위치의 큰 σ 는 그대로다.** 한 카메라 + 높이 모름이 관측 프레임으로 2프레임 이어지면 2 m 떨어진 사람에게도 선행 0초 R5 가
   (확인 필요로) 난다. 리허설 1건은 관측 1프레임 뒤 예측으로 바뀐 경로였지만, 같은 장면이 조금만 길게 관측됐으면 막히지 않았다.
2. **가려지며 다가가는 사람의 R5 지연.** 높이를 모르고 한 프레임 보인 뒤 가려진 채 구역으로 가는 사람은 관측이 돌아오거나 R1 이
   날 때까지 R5 가 없다. R5 는 CAUTION 안내라 R1(위치 확실 시)이 진입을 맡는다.
3. 위 끝만 모르는 Z_UNKNOWN(접지점 있음)도 관측 상실 발생이 막힌다(-725 보다 조금 넓음).

## 다음 라이브 세션에서 잴 것

- R5 RAISED 중 `observation_lost_before_persistence` 근거 수와 그 확인 필요 조합 — Z_UNKNOWN + PREDICTED 는 0 이어야 함.
- R5 RAISED 중 `predicted_touching_now` 이면서 zone_distance_m > 1 m 인 수(남은 위험 1)와 그때 `SINGLE_VIEW`·σ.
