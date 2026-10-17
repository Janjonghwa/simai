# Tier 0 노트북–Pod 시계 차이 추정 설계

S15P31A101-160. 기획서 §3.9(노트북–Pod 시계 차이), §6.1(T_lag), §6.4(해소), §6.5(시간 동기화 이탈), FR-139, 부록 D(D-G0-12) 기준이다.
계약은 `contracts/messages/clock_sync_request·clock_sync_response.schema.json` 와 `health.schema.json` 의 `laptop_clock_offset`(S15P31A101-155)을 그대로 쓴다.
코드는 `ai/src/simai_ai/tracking/` 의 `clock_sync.py`(교환·추정), `time_model.py`(T_lag 보정), `system_health.py`(Health 반영, 처리 정체 감시), `decision_time.py`(Risk 판정 시각·해소 확인 어댑터)에 있다. 시험은 `ai/tests/tracking/test_clock_sync.py`, `test_clock_operation.py`, `test_time_model.py`, `test_system_health.py` 에 있다.

## 범위

| 맡음 | 맡지 않음 |
|---|---|
| Tier 0 쪽 교환: 요청 생성(t1), 응답 처리(t4), 표본 거르기, 추정, health 필드 값 | 제어 WebSocket 연결과 송수신 루프(Tier 0 서비스 조립, `ClockSyncChannel` 로 주입) |
| 노트북 쪽 응답 생성 순수 함수 `build_clock_sync_response` | 게이트웨이의 실제 연결·호출(S15P31A101-83/-159) |
| T_lag 보정(보정 전후 기록), System Health 반영, 처리 정체 감시 | health 메시지 전체 조립, world_state 매핑 |
| Risk 생명주기에 넘길 판정 시각(`DecisionClock`)·해소 확인 정책(`ClockAwareVerification`) | `risk/lifecycle.py` 변경(S15P31A101-156) |

노트북 응답 함수는 Tier 0 와 같은 공식·검증을 한곳에 두려고 `simai_ai.tracking` 에 둔다. 게이트웨이는 현재 `simai_ai` 를 의존하지 않으므로, 연결할 때 의존을 추가하거나 같은 동작을 옮겨 이 모듈의 시험으로 대조한다.

## 교환과 추정

`t1`(Tier 0 요청 송신), `t2`(노트북 수신), `t3`(노트북 송신), `t4`(Tier 0 수신).
`offset = ((t2 - t1) + (t3 - t4)) / 2`(노트북 - Tier 0, 노트북이 빠르면 양수), `round_trip = (t4 - t1) - (t3 - t2)`.

- 인과율만으로 참 offset 은 표본 구간 `[t3 - t4, t2 - t1]` 안에 있다. 구간 반폭 h = round_trip/2 + 1ms(t2·t3 정수 ms 양자화) + `max_drift_ppm` × 표본 나이.
- `offset_ms` 는 추정 창 안에서 round_trip 이 가장 짧은 표본의 값이다(같으면 최근 것, 계약).
- `uncertainty_ms = max(h_최소왕복, |offset_최소왕복 − offset_최근| + h_최근)`. 창 안에서 노트북 시계가 두 표본 구간이 겹칠 만큼 작게 뛰면 옛 표본으로는 알아챌 수 없지만, 참값은 언제나 가장 최근 표본의 구간 안에 있으므로 이 값은 참값을 놓치지 않는다. 대가로 불확실성은 대체로 최근 표본의 왕복/2 수준이 된다(가운데 값은 최소 왕복 표본이라 정확하다). 계약의 "round_trip_ms / 2 이상"도 지킨다.
- 새 표본 구간과 겹치지 않는 옛 표본(표류만큼 넓힌 구간)은 시계가 바뀌기 전 것이라 버린다(큰 노트북 시계 점프). 시계가 그대로면 모든 구간이 참값을 품어 서로 겹치므로 정상 표본을 잘못 버리지 않는다.
- `estimated_at` 은 쓴 표본의 t4(Tier 0 시계), `sample_count` 는 창 안의 유효 표본 수다.

**불확실성 보장의 가정.** `|추정 − 참| ≤ uncertainty_ms` 는 다음이 성립할 때만 보장한다.

1. 인과율: 상행·하행 지연이 0 이상이다(요청은 보낸 뒤 받고, 응답은 받은 뒤 보낸다).
2. 노트북은 t2 를 요청을 받자마자, t3 를 응답을 보내기 직전에 잰다(계약). 처리 시간이 `max_responder_hold_ms` 를 넘는 표본은 버린다.
3. 두 시계의 상대 표류가 `max_drift_ppm` 이하다. 표본 사이와 마지막 표본 뒤 경과 모두에 적용한다.
4. **마지막 표본 뒤에는 노트북 시계가 뛰지 않았다.** 마지막 표본보다 앞선 점프는 위 덮는 폭으로 흡수하지만, 그 뒤의 점프는 다음 표본이 오기 전까지 알 수 없다. 그 점프가 보정 lag 를 불확실성 밖 음수로 만들면 `OFFSET_INCONSISTENT` 로 잡혀 Health UNSAFE_UNKNOWN·해소 확인 금지가 되고, 그렇지 않은 크기의 점프는 다음 교환(정상 1s, 교환 지연 시 최대 `offset_hold_ms`)까지 그 크기만큼 오차가 남을 수 있다.
5. Tier 0 단조 시계는 뒤로 가지 않는다(가면 ValueError).

**Tier 0 시계(`Tier0Clock`).** t1·t4 는 단조 시계로 잰다. 첫 시각에서 (벽시계, 단조) 기준점을 잡고 UTC = 기준 벽시계 + 단조 경과로 낸다. 벽시계가 이 값에서 50ms 넘게 벗어나면(NTP step, 역행) 기준점을 다시 잡는다(`epoch` 증가). 표본은 단조 시각으로 보관하고 추정할 때마다 현재 기준점으로 UTC 를 다시 계산하므로, Tier 0 벽시계가 뛰거나 거꾸로 가도 표본을 버리지 않고 왕복은 영향을 받지 않는다.

**판정 시각(`DecisionClock`).** 기준점을 뒤로 다시 잡으면 Tier 0 UTC 가 거꾸로 간다. Risk 생명주기는 판정 시각 역행을 거부하므로, `t_risk_now` 와 Risk Engine·`LifecycleTracker` 의 시계는 `DecisionClock` 으로 준다. 직전 값보다 작으면 직전 값으로 고정하고(앞으로만), 기준점 전환을 `epoch_changes`(역행 폭 `held_ms`)로 남긴다. 고정된 동안 판정 시각은 Tier 0 UTC 보다 최대 역행 폭만큼 앞서므로 lag 는 그만큼 크게(보수적으로) 나온다. 활성 이벤트는 유지되고 해소되지 않는다.

**버리는 응답.** 모르는 `exchange_id`(중복·끊김으로 버린 요청 포함), `request_timeout_ms` 초과, 돌려준 `t1_ms` 가 보낸 값과 다름, 형식 오류·`t3 < t2`, 노트북 처리 시간 `t3 - t2 > max_responder_hold_ms`, `round_trip < 0`(교환 중 노트북 시계 점프).

## 교환이 늦거나 끊겼을 때 (Lead 결정)

| 상황 | offset | `laptop_clock_offset` | System Health |
|---|---|---|---|
| 연결은 살아 있고 교환만 늦음, 마지막 표본 나이 ≤ `offset_hold_ms`(60s) | 마지막 추정, 불확실성은 표류(100ppm × 나이)만큼 커짐 | 나이 든 추정값(`estimated_at` 그대로) | 다른 원인대로 (불확실성 > 100ms 면 DEGRADED) |
| 유지 상한 초과, 표본 없음 | None | null | UNSAFE_UNKNOWN (`CLOCK_OFFSET_UNKNOWN`) |
| 제어 채널 끊김(`link_lost()`) | None (대기 요청·표본 모두 버림) | null (계약) | UNSAFE_UNKNOWN (`CLOCK_OFFSET_UNKNOWN` + `GATEWAY_LINK_LOST`) |

0 으로 채우지 않는다. 시계 차이를 모르면 판정 시각 기준 지연을 알 수 없으므로 판정 불가를 숨기지 않고 UNSAFE_UNKNOWN 으로 올린다(노트북 '관제 중단' 경로).

**객체 상태는 막지 않는다.** 관측 상태는 gap(촬영 시각끼리, 시계 차이와 무관)으로 그대로 정한다. 시계 차이를 모르면 `TrackTimeState.time_reference = False` 이고 위치·표준편차는 `t_latest_global` 시점까지만 예측한다(현재로 옮기지 않음). 새 위험(신규 진입) 판정은 계속된다. 현재 시각이 필요한 판단 중 해소 확인은 `ClockAwareVerification` 이 막는다(UNVERIFIABLE, 해소 확정 금지). 막는 상태는 절대 시각을 믿을 수 없는 모든 경우다: 시계 차이 모름(표본 없음·유지 상한 초과·연결 끊김)과, 시계 차이는 있으나 그 판정의 촬영 시각과 맞지 않음(`OFFSET_INCONSISTENT`). 시간 모델·Health 와 같은 `estimate_lag` 로 판정해 셋이 어긋나지 않는다. 시간 대응이 복구되면 유효 관측으로 해소 유지 시간을 처음부터 다시 채운 뒤 해소한다.

**시계 세대.** 해소 유지 시간은 노트북 시계 `observation_time` 으로 잰다(`risk.lifecycle`). 노트북 시계가 앞으로 뛴 직후 첫 판정 전에 교환이 점프를 감지해 offset 을 다시 맞추면, 위 판정은 통과하지만 점프 폭만큼 유지 시간을 건너뛴다. 그래서 `Tier0ClockSync.generation`(노트북 점프 감지 횟수 + Tier 0 기준점 전환 횟수)이 바뀐 뒤 각 이벤트의 첫 해소 확인은 False 로 둔다. 생명주기가 해소 확인을 처음부터 다시 세므로 점프 뒤 관측으로 유지 시간(3s)을 다시 채운다. 새 이벤트(세대가 바뀐 뒤 처음 보는 이벤트)는 막지 않는다. 구간이 겹칠 만큼 작은 점프는 감지되지 않아 그 폭(왕복 시간 수준)만큼 유지 시간이 줄 수 있다. 뒤로 가는 점프는 유지 시간을 늘리는 쪽(보수적)이다. 시계 차이를 알면 판정 시각을 노트북 시계 상한(now + offset + uncertainty)으로 옮겨 기본 정책(`ObservedTracksPolicy`)에 넘긴다. 기본 정책의 lag 는 `now − world.t_latest_global` 인데 LIVE 에서 두 시각의 시계가 다르기 때문이다.

## T_lag 보정 (명세 6.1)

촬영 시각은 노트북 시계, `t_risk_now` 는 Tier 0 시계다. `t_latest_global` 을 `t - offset_ms`(반올림)로 Tier 0 시계로 옮겨 `T_lag` 를 계산한다. `T_gap` 은 두 시각 모두 노트북 시계라 보정하지 않는다.

`estimate_lag(t_risk_now, t_latest_global, capture_clock)` 은 보정 전후를 모두 낸다(`raw_lag_ms`, `lag_ms`, `uncertainty_ms`). 둘 다 기록해 시계 차이가 실제로 lag 를 얼마나 바꿨는지 G0 에서 볼 수 있게 한다. raw lag 는 판정에 쓰지 않는다.

| `capture_clock` | lag_ms | 비고 |
|---|---|---|
| `SAME_CLOCK`(기본) | raw 그대로, 불확실성 0 | SIM·REPLAY·FILE·같은 호스트 전용. LIVE 조립은 반드시 `Tier0ClockSync.capture_clock(링크)` 를 넘긴다. 음수면 ValueError(기존 동작) |
| `ClockOffset` | 보정 값. 음수가 불확실성 안이면 0 으로 자름 | 참 lag ≥ 0 이고 상한(lag + u)은 그대로 보수적 |
| `ClockOffset`, 보정 값 < −u | None (`OFFSET_INCONSISTENT`) | 마지막 표본 뒤 시계 점프 등. 오류로 루프를 멈추지 않는다 |
| `None` | None (`OFFSET_UNKNOWN`) | 표본 없음·유지 상한 초과·끊김 |

`evaluate_track` 은 보정한 lag 로 staleness 를 내고, 불확실성 확대를 `max_speed_mps × (staleness + lag 불확실성)` 으로 한다.

## System Health 반영 (명세 6.1, 6.5, 3.2)

| 조건 | 원인 | 상태 |
|---|---|---|
| max(보정 lag 상한 lag + u, 처리 정체 경과) > 600ms / > 2000ms | `LAG_DEGRADED` / `LAG_UNSAFE` | DEGRADED / UNSAFE_UNKNOWN |
| 시계 차이 불확실성 > `clock_uncertainty_degraded_ms` | `CLOCK_OFFSET_UNCERTAIN` | DEGRADED (6.5 "불확실성 증가") |
| 시계 차이 모름 | `CLOCK_OFFSET_UNKNOWN` | UNSAFE_UNKNOWN |
| 보정 값이 불확실성 밖으로 음수 | `CLOCK_OFFSET_INCONSISTENT` | UNSAFE_UNKNOWN |

**처리 정체 감시(`ProgressWatchdog`).** `t_latest_global` 이 마지막으로 늘어난 Tier 0 단조 시각을 기억하고 그 뒤 경과(`progress_age_ms`)를 `evaluate_health` 에 넘긴다. 촬영은 처리보다 먼저이므로 참 lag ≥ 이 경과이고, 이 하한을 lag 임계값과 비교한다. 서로 다른 벽시계의 차이(raw lag)를 쓰지 않으므로 시계 차이의 부호·크기나 시계 차이를 아는지와 무관하게 정체를 같은 시점에 잡는다(이전 판에서 raw lag 로 감시하던 방식은 노트북이 빠를 때 감지가 늦어 버렸다).

## 초기값 (결정 기록)

명세에 수치가 없는 제안값이다. G0 실측(D-G0-12) 뒤 다시 정한다.

| 값 | 초기값 | 근거 |
|---|---|---|
| `exchange_period_ms` | 1000 | health 1 Hz 와 같은 주기. 접속·재접속 직후에는 바로 한 번 보낸다(`request_due()`) |
| `request_timeout_ms` | 2000 | 하트비트 장애 판정(3회 누락, 3초)보다 짧게 |
| `window_ms` / `max_samples` | 60000 / 64 | 1분 안의 최소 왕복 표본 |
| `offset_hold_ms` | 60000 | Lead 결정. 표류 확대 100ppm × 60s = 6ms |
| `max_responder_hold_ms` | 100 | 응답은 받은 즉시 보낸다(계약). 넘으면 노트북 시계 점프나 처리 지연으로 본다 |
| `max_drift_ppm` | 100 | NTP 미동기 수정 발진기 최악 수준(보수적) |
| `Tier0Clock.jump_threshold_ms` | 50 | 이보다 큰 벽시계 변화만 step 으로 본다 |
| `clock_uncertainty_degraded_ms` | 100 | lag DEGRADED 600ms 예산의 1/6. 일본 리전 왕복 수십 ms 에서 불확실성 15~40ms 예상 |

## 설계 허용치와 시험

| 항목 | 허용치 | 시험 |
|---|---|---|
| 추정 오차 | 모든 시나리오에서 \|추정 − 참\| ≤ `uncertainty_ms` | 고정 offset, 비대칭, 지터, 표류, 큰·작은 노트북 점프, Tier 0 벽시계 점프·역행, 교환 지연(유지) |
| 대칭 경로 | 오차 ≤ 1ms | `test_fixed_offset_symmetric_path` |
| 비대칭 경로 | 오차 = (상행 − 하행)/2, 불확실성 안 | `test_asymmetric_delay_error_within_uncertainty` |
| 지터(고정 시드, 바닥 15ms + 지수 지터 평균 20ms, 60회) | 최종 오차 ≤ 5ms, 최소 왕복 표본 선택 | `test_jitter_uses_min_round_trip_sample` |
| 작은 노트북 점프(겹치는 구간, ±10·20ms) | 불확실성이 참값을 덮음 | `test_small_laptop_step_inside_overlap_stays_within_uncertainty` |
| 큰 노트북 점프 | 다음 표본 한 번으로 새 offset 을 따름 | `test_laptop_clock_jump_resets_window` |
| 교환 지연 | 60s 까지 나이 든 추정(불확실성 표류만큼 증가), 넘으면 null | `test_late_exchange_holds_aged_estimate_until_limit_then_null` |
| 보정 T_lag | \|보정 lag − 참 lag\| ≤ `uncertainty_ms` + 1ms | `test_lag_correction_end_to_end` |
| 처리 정체 | ±37s·+300ms·0 offset × 시계 차이 앎/모름 × 정체 0/0.7/2.5s 에서 같은 판정 | `test_stall_watchdog_is_independent_of_offset` |
| 운영 시나리오 | 영상 정상 + 교환 실패(유지 상한 전/후) + 기존 위험 + 신규 진입: 신규 진입은 발생·경보, 상한 후 UNSAFE_UNKNOWN·해소 0건, 교환 재개 뒤 해소 | `test_exchange_failure_before_and_after_hold_limit` |
| 해소 금지 상태별 | 불일치(응답 중단 + 노트북 +5s step)·유지 상한 초과·실제 연결 단절 각각에서 UNSAFE_UNKNOWN, 해소 0건·ALERT_CLEAR 0건, 신규 진입 경보 1건, 복구 뒤 해소 | `test_no_resolution_while_time_reference_is_untrusted` |
| 교환이 먼저 감지한 노트북 forward 점프(+5s·+60s) | 그 틱 해소·ALERT_CLEAR 0, 약 3s(29~31틱) 뒤 해소 1 | `test_laptop_forward_step_detected_by_exchange_does_not_skip_clear_hold` |
| Tier 0 벽시계 역행·전진 | 판정 시각 단조, 활성 이벤트 유지, 신규 경보, 해소 0건, 루프 지속 | `test_tier0_wall_step_keeps_decision_loop_and_events` |
| 계약 | 요청·응답·health 필드(값·null) 스키마 통과 | `test_messages_and_health_field_match_contracts` |
