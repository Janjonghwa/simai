# S15P31A101-729 PPE 표본 빈 칸 계측·추론 간격 조절과 ID 초기화 사유 표시

관련 명세: 5.5(PPE 워커·상태 전환), 6.2(PPE 상태), 12.3(I1 `ppe_state`). 선행: -567(5 Hz 입력 칸·단조 시계 500 ms), -653(ID 불확실 칸 건너뛰기), -661(순수 병합 승계), -684(World ID 불확실 계측·수정). 명세 반영: 필요 없음(아래 "명세와의 관계").

## 왜 하나 (실측)

2026-11-15 EC2 DB 읽기 전용 조회. 대상은 `ppe-policy-0.4.0` 세션 하나다(`stream_epoch` 1794554024535, 2026-11-13 07:14~08:26 UTC, World ID 68개, 메시지당 평균 3명). 이 세션은 -684 머지(같은 날 20:49 KST) 전이다.

- 안전모 UNKNOWN 은 약 96% 였다(WORN 2.6%, NOT_WORN 1.2%). ID 구간이 시작되고 15 s 가 지나도 UNKNOWN 은 92% 였다. "ID 가 짧아 다시 쌓는 시간"은 주원인이 아니었다(`ACCUMULATING` 2.4%).
- PPE 행마다 같은 시각(0.5 s 안)의 `world_track_state` 를 붙여 UNKNOWN 을 나눴다.

| 묶음 | UNKNOWN 중 | 비고 |
|---|---|---|
| Tier 0 도 못 봄(`PREDICTED`·`UNKNOWN`) | 62.2% | UNKNOWN 이 맞다(모르면 비운다) |
| 보였지만 Tier 0 `IDENTITY_UNCERTAIN` | 26.6% | -653 규칙으로 칸을 건너뛴다. -684 대상 |
| 보이고 ID 확실, PPE 근거 없음(표본 없음·검출 없음·영역) | 7.2% | 이 티켓 A |
| 보이고 ID 확실, ID 초기화 사유가 남음 | 2.5% | 이 티켓 D |
| 쌓는 중 | 1.5% | |

- PPE 메시지 간격 중앙값은 0.52 s, 상위 1% 는 0.61 s 였다. Tier 0 지연(`pipeline_lag_ms`)이 1 s 를 넘은 표본은 4,311개 중 4개였다. 접수 문이 막혀 추론이 멈춘 것은 원인이 아니다.
- `IDENTITY_MAPPING_CHANGED` 는 이 사유로 들어간 횟수가 53회인데 UNKNOWN 2,297행(35.8%)을 차지했다. 확정 전까지 사유를 고정해 두는 규칙(`_STICKY_REASONS`)이 뒤의 실제 원인을 가렸다.

## 결정

### A. 촬영 칸 채움 계측과 추론 간격 하한 조절

- **원인 가설(합성 재현, 현장 미확정)**: 카메라마다 "추론 시작 간격 500 ms 이상"과 "같은 0.5 s 촬영 칸 1회"를 함께 건다(-567). 실제로는 카메라를 차례로 돌며 추론하고, step 사이에 50 ms 를 쉰다. 이 때문에 실제 간격이 500 ms 를 조금씩 넘고, 촬영 칸이 주기적으로 빈다.
- **합성 재현**: `tests/ppe/test_service_loop.py::jitter_stream` 와 같은 모형으로 60 s 를 돌린 결과다. 조건은 카메라 4대, step 뒤 50 ms + 흔들림, 추론 25~60 ms + 흔들림이다. 표의 값은 카메라별로 빈 촬영 칸의 비율이다.

| 입력 주기 / 추론 / 흔들림 | 500 ms | 450 ms | 400 ms |
|---|---|---|---|
| 100 ms / 25 / 10 | 4.8% | 0.2% | 0.0% |
| 100 ms / 40 / 20 | 5.8% | 0.2% | 0.0% |
| 100 ms / 60 / 30 | 8.1% | 1.0% | 0.2% |
| 200 ms / 25 / 10 | 9.8% | 0.0% | 0.0% |
| 200 ms / 40 / 20 | 12.9% | 2.1% | 0.0% |
| 200 ms / 60 / 30 | 14.2% | 7.1% | 2.3% |

흔들림이 없으면 500 ms 에서도 빈 칸이 생기지 않았다. 따라서 실제 현장 효과는 계측으로 확인해야 한다.

- **계측(판정 불변)**: `PpeLoop.bin_stats`·`diagnostics()["bins"]` 와 `counts` 에 카메라별 세 값을 둔다. 60 s 마다 나오는 `PPE 상태` 로그에 실린다.
  - `inferred_bins`: 추론한 칸 수
  - `bins_missed`: 같은 epoch 안에서 직전 추론 칸과의 사이에 건너뛴 칸 수
  - `rate_gap_new_bin`: 간격 하한 때문에 새 촬영 칸을 거른 횟수(칸마다 1회)
- **간격 하한 조절**: `SIMAI_PPE_MIN_INFER_GAP_MS`(정수 ms)로 바꾼다.
  - **기본 500 그대로**다. 400~500 만 받고, 범위 밖이면 시작 실패(종료 코드 2)다. `PpeLoop` 도 400 미만이면 `ValueError` 를 낸다.
  - 칸당 1회 제한은 그대로라 촬영 시각 기준 평균은 카메라당 2 Hz 이하다.
- **현장 운영**: `bins_missed / inferred_bins` 가 5% 를 넘으면 400~450 으로 낮추고 PPE 를 다시 띄운다(`deploy/README.md` PPE 절).

### D. ID 초기화 사유를 초기화 칸까지만 표시

- 바꾸는 사유는 `IDENTITY_REVISION_CHANGED`·`IDENTITY_MAPPING_CHANGED` 둘이다. 초기화한 칸(`ItemState.reset_bin`)까지는 이 사유를 보이고, 다음 칸부터는 그 칸의 사유(`NO_OBSERVATION`·`ACCUMULATING`·`IDENTITY_UNCERTAIN` 등)로 바꾼다.
- `CONTRARY_EVIDENCE`·`CROSS_CAMERA_CONFLICT` 는 지금처럼 다시 확정될 때까지 남긴다. 반대 근거가 있었다는 사실 자체가 판단 정보이기 때문이다.
- 사유가 사라져도 초기화 횟수는 남도록 `identity_events["mapping_reset"]` 을 새로 센다. 초기화된 Track 마다 1이고, local ID 가 넘어가면 보통 2다. 차수 상승 초기화는 기존 `revision_reset` 이 센다.
- 상태·창·만료·확정 시점은 0.4.0 과 같다. 사유 코드의 의미가 바뀌므로 `policy_version` 을 `ppe-policy-0.4.0` → `ppe-policy-0.5.0` 으로 올린다(계약 `reason` 설명: 규칙 판마다 목록을 문서로 둔다). 새 사유 코드는 없고, `ppe_state` 스키마 변경도 없다.

## 바꾼 것

- `ai/services/ppe/loop.py`: `MIN_PER_CAMERA_PERIOD_MS = 400`, `MISS_SPAN_BINS = 4`, `bin_stats`·`_count_bins`·`_bin_count`, `_skip_reason` 의 새 칸 간격 거름 계측, 사람 없는 칸에서 빈 칸 기준 지우기, `diagnostics()["bins"]`.
- `ai/services/ppe/__main__.py`: `min_infer_gap_ms(env)` 와 `SIMAI_PPE_MIN_INFER_GAP_MS`. 시작 로그에 `infer_gap_ms` 를 남긴다.
- `ai/src/simai_ai/ppe/state.py`: `ItemState.reset_bin`, `_reason_open`, `_ID_RESET_REASONS`, `mapping_reset` 계측.
- `ai/src/simai_ai/ppe/config.py`: `POLICY_VERSION = "ppe-policy-0.5.0"`.
- `deploy/README.md` PPE 절, `docs/design/ppe-service.md` 주기 행.

## 정한 수치와 근거

- **하한 400 ms**: 위 표에서 400 ms 는 흔한 조건에서 빈 칸이 0~2% 다. 칸당 1회와 함께 쓰면 순간 간격은 400 ms 이상이고, 촬영 시각 기준 평균은 2 Hz 이하다. 더 낮추면 같은 칸 거름만 늘고 이득이 없다.
- **빈 칸 세기 범위 2 s(4칸)**: 이보다 긴 끊김은 사람이 화면 밖에 있었거나 접수 문이 막힌 구간일 수 있어 세지 않는다. 사람이 없던 프레임을 처리하면 기준을 지운다.
- **현장 판단 기준 5%**: WORN 확정은 유효 6칸 중 5칸 착용이라 빈 칸을 1칸까지만 견딘다. 카메라 2대가 같은 칸을 함께 놓칠 확률을 낮게 두려고 정한 운영 기준이다. 실측으로 다시 정한다.

## 명세와의 관계

- 5.5 "카메라당 최대 2 Hz": 기본값은 그대로다. 낮춘 설정도 칸당 1회로 평균 2 Hz 를 지킨다. 다만 순간 간격은 400 ms 까지 짧아진다. 운영에서 낮춘 값을 기본으로 삼게 되면, 명세 정리 MR 에서 "평균(촬영 칸당 1회) 기준"으로 문구를 고친다.
- 사유 표시 규칙은 명세에 없다(5.5·6.2 대조). 명세 변경은 필요 없다.

## 버린 대안

- **ID 가 바뀔 때 PPE 상태를 새 ID 로 넘기기**: 실측에서 ID 구간 시작 3 s 안의 UNKNOWN 은 전체의 26.9%(상한)였고, 대부분은 Tier 0 미관측·불확실과 겹쳤다. 근거가 약한 승계는 다른 사람의 착용 상태를 넘겨 R9 누락을 낼 수 있다(규칙 2).
- **local ID 주인 변경 때 초기화 완화(C안)**: 줄일 수 있는 몫은 UNKNOWN 의 약 2.5% 안이다. 대신 다른 사람 근거가 섞일 위험이 있는 안전 완화라 하지 않는다.
- **기본 간격을 바로 400 으로 바꾸기**: -567 결정과 명세 문구를 다시 해석해야 한다. 현장 계측 전이고 시연(11-18) 직전이라 기본값은 두고 설정으로만 연다.
- **사유를 아예 고정하지 않기**: 반대 근거·충돌 사유까지 풀면 깜빡임 방지 근거가 화면에서 사라진다.

## 잔여 위험

- A 의 효과는 합성 모형 기준이다. 실제 추론 시간, step 흔들림, Tier 0 공유 주기에 따라 빈 칸 비율이 다르다. 현장 계측 전에는 기본값을 바꾸지 않는다.
- 간격을 낮추면 PPE GPU 사용이 평균 2 Hz 까지 조금 늘어난다(이전 실측은 빈 칸만큼 덜 썼다). 접수 문(3.2)은 그대로라 Tier 0 지연이 늘면 멈춘다.
- D 이후 같은 장면의 UNKNOWN 사유 분포는 0.4.0 과 직접 비교할 수 없다. `policy_version` 으로 나눠 본다.
- UNKNOWN 의 주원인(Tier 0 미관측 62%, ID 불확실 27%)은 이 티켓 범위 밖이다. -684 이후 같은 쿼리로 다시 잰다.

## 시험

- `ai/tests/ppe/test_service_loop.py`
  - 500 ms·흔들림에서 빈 칸이 생기고 계측값이 실제와 같다.
  - 400 ms(시드 3개)에서 빈 칸 0, 칸당 1회, 간격 400 ms 이상이다.
  - 사람 없음·2 s 넘는 끊김은 세지 않는다.
  - 하한 미만은 거부한다. 환경변수 해석과 범위 밖 시작 실패를 확인한다.
- `ai/tests/ppe/test_association_state.py`
  - 주인 변경 초기화 뒤 빈 칸은 `NO_OBSERVATION`, 결정 칸은 `ACCUMULATING` 이고 확정 시점은 같다.
  - 차수 초기화 뒤 불확실 칸은 `IDENTITY_UNCERTAIN` 이다.
  - 반대 근거 사유는 그대로 남는다. `mapping_reset` 이 2로 센다. 정책 버전을 확인한다.
- 새 시험 중 계측·사유 시험 6건은 이전 코드에서 실패한다. `uv run --offline pytest tests/ppe tests/services/ppe`: 211 passed.

## 넘긴 일

- 현장(월) 계측: `bins_missed`·`rate_gap_new_bin` 을 보고 `SIMAI_PPE_MIN_INFER_GAP_MS` 를 켤지 판정해 이 이슈에 댓글로 남긴다.
- -684 이후 세션으로 위 UNKNOWN 분해를 다시 잰다.
- 화면 지표: UNKNOWN 비율의 분모를 Tier 0 관측 중인 사람으로 좁힌다(판정 변경 아님, 프론트·서버 몫).
