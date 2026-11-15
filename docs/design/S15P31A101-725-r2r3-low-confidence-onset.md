# S15P31A101-725 R2·R3 는 접지 모름 + 예측 위치로 새로 발생시키지 않는다

관련 명세: 7.1(불확실성 처리), 7.2(R2·R3 접지 모름 정책), 7.3(확인 필요 문구), 6.4(지속 조건·지속 대기 관측 상실), 5.2(접지).
명세 개정: v1.3 수정 80. 선례: 수정 53(S15P31A101-430·-431, R1 은 확실한 위치만 새로 발생). 선택지 비교: -723 설계 문서 3절. 계약 변경 없음.

## 결정 (사용자 2026-11-15, 선택지 C)

R2(추락 위험 가장자리)·R3(고소 작업면 위)의 **새 발생**은 그 프레임에서 대상의 접지가 모름이면서 위치가 예측이면 내지 않는다.

- 접지 모름 = `height_common.standing` 의 `StandingBasis.UNKNOWN`(접지점 `ground_anchor_xyz` 없음, `z_validity` UNAVAILABLE, 확인 필요
  `Z_UNKNOWN`). 가려진 동안 유지한 접지점(`StandingBasis.HELD`)은 서 있는 면을 마지막 관측값으로 아는 상태라 해당하지 않는다.
- 위치 예측 = 트랙 `observation_state` 가 `PREDICTED`(확인 필요 `PREDICTED` 의 트랙 쪽 근거, `r1_zone_entry.track_confidence_reasons`).
- 둘 다일 때 규칙은 판정을 `holding=True` 로 낸다(`height_common.low_confidence_onset`). lifecycle 은 holding 으로 새 이벤트를 만들지 않고
  활성 이벤트만 유지한다. R2·R3 는 등급이 고정이라 활성 이벤트에서 holding 과 발생 판정의 효과(유지, 해소 확인 재시작, 확인 필요·근거·수치
  갱신)가 같다. 그래서 이미 열린 이벤트의 유지·해소·등급은 바뀌지 않는다.
- 둘 중 하나만 모르면 지금처럼 확인 필요를 붙여 발생한다. 접지점이 생기거나 관측 위치가 되면 그 프레임부터 지속 조건(3프레임 중 2)대로 발생한다.
- R1·R4~R11 은 바꾸지 않았다. R5 는 원래 PREDICTED 트랙과 접지 모름 가장자리 대상을 판정하지 않아 빈틈이 생기지 않는다.
- `rule_version` 은 그대로(R2 v1·v1001, R3 v1·v1001). 11.4 첫 운영 배포 전 규칙 보완 예외, R1 수정 53 과 같은 처리.

## 근거

17차 GPU 세션(2026-11-15, 14차 녹화 재생) 작업 4분: R2 27건이 모두 발생 때 `Z_UNKNOWN`, 21건 `PREDICTED`, 20건 `SINGLE_VIEW`. R3 는 같은
시각·같은 ID 로 함께 열렸다. 7.2 접지 모름 정책("범위가 작업면에 조금이라도 걸치면 그 위에 있을 수 있다")과 7.1("R2~R5 발생은 범위가
조금이라도 걸리면")이 겹쳐, 접지가 안 나온 새 ID 가 단상 근처를 지날 때마다 CRITICAL+HIGH 가 났다. -723 3절 표의 A~F 중 C 가 가장 좁고
(최대 21/27 차단 추정) R1 선례·"모르면 비운다"와 같은 방향이다.

## 코드·시험 위치

- `ai/src/simai_ai/risk/rules/height_common.py`: `low_confidence_onset(track, basis)`, 머리말 절.
- `ai/src/simai_ai/risk/rules/r2_edge_proximity.py`: `raising = touching and not low_confidence_onset(...)`, `holding=not raising`. 근거 코드는
  거리 사실 그대로(`edge_within_raise_distance` 가 holding 과 함께 나올 수 있다).
- `ai/src/simai_ai/risk/rules/r3_elevated_position.py`: `holding=low_confidence_onset(...)`.
- `ai/tests/risk/test_r2_r3_height.py` 끝 절: 조합 3가지 × R2·R3, HELD + PREDICTED 는 발생, R2 유지 띠 그대로, 17차 형태 1분 0건 뒤 관측 위치로
  발생, 접지점이 생기면 발생, 열린 이벤트 유지·해소, 지속 대기 관측 상실 경로, 음성 대조(조건을 끄면 열림).

## 명세에 없어 정한 것

- **지속 대기 중 예측으로 바뀔 때.** 관측 위치(접지 모름)의 발생 판정으로 지속 대기를 시작한 뒤 다음 프레임이 접지 모름 + 예측이면, 6.4
  지속 대기 관측 상실 처리대로 그 자리에서 발생한다(UNVERIFIABLE, `PREDICTED`·`pending_observation_lost`). 보류 조건은 새 대기를 시작하지
  않을 뿐 관측으로 시작한 대기의 안전 처리는 바꾸지 않는다(R1 수정 53 과 같은 동작). 시험으로 고정했다.
- **근거 코드를 더하지 않음.** 보류된 판정은 활성 이벤트가 없으면 저널에 남지 않고, 활성 이벤트에서는 효과가 발생 판정과 같아 별도 코드가
  필요 없다(R1 선례).

## 버린 대안

- B(PREDICTED 만으로 막기): 접지 모름 + 관측 위치도 막아 C 보다 넓다. 사용자 결정이 C.
- E(낮은 신뢰 발생을 CAUTION 으로): 7.3 "신뢰도가 낮아도 등급은 내려가지 않는다"와 충돌.
- 규칙 대신 lifecycle 에서 거르기: 보류 판단이 규칙 문맥(서 있는 면)에 있고, R1 선례도 규칙에서 holding 으로 냈다.

## 잔여 위험

1. **가려진 접근의 발생 지연.** 실제로 단상 위에서 가려진 채(접지 모름 + 예측) 가장자리로 다가가는 사람은 관측 위치나 접지점이 돌아올 때까지
   R2·R3 가 나지 않는다. 예측 상태는 6.1 기준 최대 500 ms(그 뒤 UNKNOWN, UNKNOWN 은 원래 판정하지 않음)라 지연 상한은 대략 "관측이 돌아올
   때까지"다. 관측으로 시작한 지속 대기는 위 처리로 바로 발생하므로 순간 가림만으로 놓치지는 않는다.
2. **접지 모름 + 관측 위치는 그대로 남는다.** 17차 27건 중 약 6건 형태는 계속 날 수 있다. -716 의 재생 입력 시각 어긋남이 접지를 못 내게 한
   주원인이면 라이브 RTSP 에서는 Z_UNKNOWN 자체가 줄 수 있다.
3. 21/27 은 발생 순간 기준 추정이다. 지속 조건 중 관측 프레임이 끼면 늦게 날 수 있다.

## 다음 라이브 세션에서 잴 것

- R2·R3 RAISED 수와 그중 발생 때 `confidence_reasons` 조합(`Z_UNKNOWN`·`PREDICTED`·`SINGLE_VIEW`) — 17차와 비교(Z_UNKNOWN+PREDICTED 발생은 0 이어야 함).
- 단상 위 실제 장면(부록 E 단상 장면): 사람이 올라선 시각 → R2·R3 RAISED 시각 지연, 가려진 채 가장자리 접근 시 지연(관측 복귀 시각과 비교).
- 단상 근처 트랙의 `observation_state` PREDICTED 비율과 접지점(`ground_anchor_xyz`) 있는 프레임 비율 — 라이브에서 Z_UNKNOWN 이 재생보다 줄었는지.

## 넘긴 일

- 접지 모름 + 관측 위치(잔여 위험 2)도 막을지는 라이브 측정 뒤 결정(선택지 B).
