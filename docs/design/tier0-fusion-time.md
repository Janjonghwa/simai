# Tier 0 융합 시각(공통 시각 t) 설계

S15P31A101-219. 기획서 §6.1(시간 모델: T_gap·관측 상태 표·지연 프레임), §5.2(계산 순서 1단계 시간 보간, 다시점 2대 이상), §12.7(보간 창·지연 도착),
부록 D(D-G0-10) 기준이다. AGENTS 규칙 2(2대 미만에서 보인 관절은 지어내지 않음)와 7(관측과 추정 구분)을 지킨다.
코드는 `ai/src/simai_ai/tracking/time_model.py`(`choose_fusion_time`, `FusionTime`, `TimeModelConfig.fusion_max_lag_ms`)와
`ai/services/tier0/perception.py`(`Perception.choose_t`·`step`, `Perceived.lagging`, `Perception.lagging`·`lagging_ticks`), 감시 입력
`ai/services/tier0/judge.py`(`_monitoring`)·`ai/src/simai_ai/risk/monitoring.py`(`CameraMonitor.fusion_excluded`)·`risk/rules/r7_monitoring.py`에 있다.
시험은 `ai/tests/tracking/test_time_model.py`(경계), `ai/tests/services/tier0/test_tier0_fusion_time.py`(SIM 인지 반례·음성 대조),
`ai/tests/scenarios/test_scenario_golden.py`·`cases/r1_lagging_camera_unverifiable`(파일 모드 Tier 0 시나리오) 에 있다.

## 문제

World Track 은 tick 마다 공통 시각 t 하나로 카메라별 2D keypoint 를 보간해 삼각측량한다. 보간은 외삽하지 않으므로(`tier0-triangulation.md`
'시간 보간') t 는 넣을 카메라 모두에 t 이후 프레임이 있는 시각, 곧 카메라별 최신 촬영 시각의 최솟값이어야 한다. 이전 구현은 수신 중인
카메라(최신 촬영 시각이 `t_latest_global` 에서 `camera_stall_ms` 500 ms 안) 전부의 최솟값을 t 로 골랐다.

`t_latest_global` 은 명세 6.1 대로 카메라 전체의 최댓값이고 T_gap = `t_latest_global` − `t_last_valid` 이므로, 융합 시각이 `t_latest_global` 보다
d 만큼 늦으면 모든 객체의 T_gap 이 d 이상이 된다. 한 카메라만 400 ms 늦게 도착해도(500 ms 보다 짧아 수신 중) d ≈ 400 ms 라 모든 트랙이
PREDICTED(200 ms 초과)가 되고 해소 확인이 막힌다(UNVERIFIABLE). `r1_lagging_camera_unverifiable`(cam3 400 ms 지연)에서 트랙 PREDICTED 105·
OBSERVED 5 줄, R1 은 확인 필요로 발생하고 해소되지 않았다(S15P31A101-41 리뷰 '두 알려진 동작' (a)). 최신 카메라 3대가 있어도 느린 1대에
묶이는 것은 6.1 이 요구하는 보수성이 아니라 가용성 한계다.

## 대안 비교

| 대안 | 6.1(gap 정의·OBSERVED) | 외삽 금지 | 2대 미만 금지 | 결정성 | misbound 0 | 정상 운영 영향 | 판단 |
|---|---|---|---|---|---|---|---|
| (현행) 수신 중 카메라 전부의 최솟값 | 정의는 지키나 늦은 1대가 모든 객체 gap 을 키움 | 지킴 | 지킴 | 지킴 | 지킴 | 없음 | 문제 그대로 |
| **(a) 지연 상한을 넘는 카메라를 그 tick 의 t 계산에서 뺀다** (`t_latest_global` − 최신 촬영 > 150 ms. 빼고 남은 카메라가 `min_cameras` 보다 적으면 빼지 않음) | 지킴. 넣은 카메라의 지연 ≤ 150 ms 라 gap 하한이 OBSERVED 상한(200) 안 | 지킴: 뺀 카메라는 t 이후 프레임이 없어 보간이 `NO_FRAME_AFTER` 로 그 tick 에서 빠진다 | 지킴: 남은 view 로만 삼각측량(2대 미만 관절 null) | 지킴: 카메라 시각만의 함수, 이름 순 | 지킴: 프레임 결합은 바꾸지 않음 | 상한 근처 지연이면 가끔 한 tick 빠짐(아래 측정) | **선택** |
| (b) 카메라별 시각 보간: 늦은 카메라 관측을 그 촬영 시각에만 반영 | 지키려면 이미 낸 시각 t' < t 의 World 상태를 다시 계산해야 한다 | 지킴 | 지킴 | 재계산 순서에 묶임 | 지킴 | 없음 | 버림: World Track 시각은 늘기만 하고 world_state·이벤트는 추가만 한다(AGENTS 6). 과거 tick 을 고쳐 내거나, 다른 시각의 view 를 한 삼각측량에 섞어야 한다(1.5 m/s × 0.4 s = 0.6 m 어긋남 → 잘못된 3D·연결) |
| (c) 2대 이상 최신이면 진행, 나머지는 다음 tick 합류 (t = 최신 `min_cameras` 대의 최솟값) | 지킴 | 지킴 | 지킴 | 지킴 | 지킴 | 큼: 정상 위상차(프레임 주기)만으로 매 tick 2대가 빠져 view·관절이 준다 | 버림. 남은 카메라 하한(`min_cameras`)만 (a) 에 가져왔다 |

## 정한 것

`choose_fusion_time(cameras, config)`:

1. 기준 `latest` 는 `t_latest_global` 과 같다(동기화 유효 카메라의 최신 촬영 시각 최댓값). 프레임이 없으면 None(그 tick 은 인지 결과 없음).
2. 멈춘 카메라(`latest` − 최신 촬영 > `camera_stall_ms` 500)는 이전처럼 뺀다. 이 카메라는 `lagging` 에 싣지 않는다(수신 중단은 R7·health 몫).
3. 수신 중이어도 `latest` − 최신 촬영 > `fusion_max_lag_ms`(150) 인 카메라를 뺀다(`FusionTime.lagging`, 이름 순).
4. 3 을 하고 남은 카메라(prompt)가 `min_cameras`(2)보다 적을 때만 3 을 하지 않는다(이전 동작으로 되돌림). 앞선 카메라 한 대에 맞춰
   서두르면 다른 카메라가 모두 빠져 다시점이 되지 않기 때문이다. prompt 가 2대 이상이면 되돌리지 않는다: 시계가 앞선 카메라 한 대 + 그와
   150 ms 안의 정상 카메라 한 대가 남으면 나머지 정상 카메라가 빠진다(아래 잔여 위험, R7 로 드러남).
5. t = 남은 카메라 최신 촬영 시각의 최솟값. `t_latest_global` 정의(6.1)와 T_gap·관측 상태 표는 바꾸지 않는다.

| 수치 | 값 | 근거 |
|---|---|---|
| `fusion_max_lag_ms` | 150 ms (명세에 없는 제안값) | 넣은 카메라의 지연 d 는 모든 객체 T_gap 의 하한이므로 OBSERVED 상한 200 ms 보다 작아야 한다(50 ms 는 검출 한 번 누락 등에 남김). 정상 운영의 카메라 간 최신 촬영 시각 차(추론 주기 10 Hz = 100 ms + 도착 지터 약 50 ms)보다는 커서 정상 카메라를 거의 빼지 않는다. LIVE 실측 지연 분포로 다시 정한다(남은 일) |
| 남은 카메라 하한 | `min_cameras` = 2 | 명세 5.2 다시점 2대 이상, 기존 `TimeModelConfig` 값 |

**드러내기(관측과 추정 구분).** 뺀 카메라는 그 tick 관측에 들어가지 않으므로 world_state 트랙의 `cameras`("이번 상태에 기여한 카메라")에 없다.
위치는 남은 카메라의 실제 관측이고 늦은 카메라 관측으로 지어낸 값이 없다. Tier 0 안에서는 `Perceived.lagging`(그 tick 에 뺀 카메라),
`Perception.lagging`(마지막 인지 tick 의 값, 새 tick 까지 유지), `Perception.lagging_ticks`(카메라별 누계), 뺀 카메라 집합이 바뀔 때 INFO 로그로 남는다.

**감시 입력(R7, 리뷰 F1).** 수신 중이어도 융합에 쓰지 못한 카메라는 그 구역을 관측하지 못한다(명세 7.2 R7 '구역의 2대 이상 관측 조건 상실',
6.2 '그 카메라만 보던 구역의 감시 능력 저하 표시'). 판정 루프의 감시 입력(`JudgmentLoop._monitoring`)은 `Perception.lagging` 의 카메라를
`CameraMonitor(fusion_excluded=True)` 로 넘기고, R7 은 이 카메라를 정상 관측 카메라로 세지 않는다(근거 `camera_fusion_excluded:<id>`).
그래서 그 카메라를 빼면 2대 미만이 되는 구역은 R7 CAUTION(관제 안내)이 나고 유지되며, 감시 확보 근거(`clear_zones`)도 되지 않아 지연이
풀린 뒤 해소 유지 시간이 지나야 해소된다. 그 구역에 새로 들어온 사람은 다시점 관측이 없어 트랙·R1 이 없을 수 있다. 이를 외삽이나 단안 관절로
메우지 않고 R7 안내로 드러낸다. 계약은 바꾸지 않는다: R7 은 기존 risk_event 로 나가고, health 의 카메라 상태(OK·NO_SIGNAL·WAITING_KEYFRAME·
SYNC_INVALID)에는 '융합 제외'가 없어 health 에는 싣지 않았다(남은 일). `MonitoringState.from_json`(health 형식 입력)은 이 값이 없어 False 다.

## 측정 (SIM 상한, D-G0-10 측정·기록)

파일 모드 Tier 0(`tests/scenarios/golden.py`, 녹화 시각 주입 시계), 카메라 4대 25 fps, 판정 주기 100 ms. frame_age p95 는 추론 시점 − 촬영 시각.

| 시나리오 | 트랙 관측 상태 | 늦어서 뺀 tick | frame_age p95 (cam1·cam2·cam3·cam4) | 이벤트 |
|---|---|---|---|---|
| `r1_enter_exit` (지연 없음) | OBSERVED 109 | 0 | 56·69·62·71 ms | R5 533→3533, R1 2033→8633 |
| `r1_lagging_camera_unverifiable` 고치기 전 | PREDICTED 105·OBSERVED 5 (-41 리뷰) | – | – | R1 확인 필요, 해소 없음 |
| `r1_lagging_camera_unverifiable` 고친 뒤 (cam3 +400 ms) | OBSERVED 109, `cameras` 는 모두 cam1·cam2·cam4 | cam3 106/110 | 56·69·**462**·71 ms | r1_enter_exit 와 같음 |
| `r1_late_and_reconnecting_camera` (cam3 +120 ms, cam4 끊김) | OBSERVED 109 | cam3 2, cam4 3 (/110) | 56·69·183·71 ms | 기대 그대로 |

- 다른 카메라의 frame_age 는 늦은 카메라가 있어도 같다(카메라별 최신성 대기열이라 서로 묶이지 않는다). 늦은 카메라의 영향은 frame_age 가 아니라
  융합 시각이었다.
- 120 ms 지연 카메라는 위상(25 fps 40 ms)에 따라 차이가 150 ms 를 잠깐 넘는 tick 에 빠진다(2/110). 그 tick 도 남은 3대로 OBSERVED 다.
- 기대 파일은 이 값을 `camera_stats`(카메라별 [하한, 상한])로 확인한다: cam3 frame_age p95 [430, 600]·뺀 tick [90, 110], 다른 카메라 [0, 200]·0.

## 시험

| 시험 | 본 것 |
|---|---|
| `test_time_model.py::test_fusion_lag_boundary_150_kept_151_left_out` 외 5개 | 경계(150 넣음, 151 뺌), 멈춘 카메라는 lagging 아님, 남은 카메라 < 2 면 빼지 않음, 동기화 무효 카메라는 기준 아님, 입력 순서 무관, 설정 검증 |
| `test_time_model.py::test_one_clock_ahead_camera_with_normal_phase_spread_pushes_normal_cameras_out` | 리뷰 F2 반례: cam1 만 +140 ms, 정상 세 대 40 ms 위상차 → cam3·cam4 가 빠짐(되돌림은 prompt < 2 일 때만) |
| `test_r7_monitoring.py::test_camera_left_out_of_fusion_is_not_a_healthy_monitor` | 융합에서 뺀 카메라는 R7 정상 카메라가 아니다(발생, clear_zones 아님), 남은 2대가 보는 구역은 그대로 |
| `…::test_clock_offset_camera_can_push_normal_cameras_out_and_r7_shows_it` | 리뷰 F2: 위 시계 오프셋 반례에서 빠진 cam3·cam4 로만 보는 구역은 R7 저하 |
| `test_tier0_fusion_time.py::test_lagging_camera_is_left_out_and_others_stay_observed` | 400 ms 늦은 cam3 는 `cameras` 에 없고 트랙은 OBSERVED, `lagging`·`lagging_ticks` |
| `…::test_negative_control_min_fusion_time_makes_tracks_predicted` | 음성 대조: 최솟값으로 되돌리면 PREDICTED 가 과반 |
| `…::test_lagging_camera_rejoins_when_it_catches_up` | 반례(복귀·역순 도착): 지연이 풀리면 4대가 다시 기여 |
| `…::test_only_lagging_camera_and_one_other_see_the_person_no_position_is_made` | 반례(외삽 금지·2대 미만): cam1 과 늦은 cam3 만 볼 때 새 관측 없음 → UNKNOWN |
| `…::test_joint_seen_only_by_lagging_camera_and_one_other_is_null` | 반례(2대 미만 관절): cam1·cam3 만 보는 오른팔 관절 null·`UNAVAILABLE`, 늦지 않으면 삼각측량됨(빈 시험 아님) |
| `…::test_negative_control_extrapolated_*` | 음성 대조: 늦은 카메라 마지막 프레임을 t 로 끌어 쓰는 결함을 넣으면 위 두 반례 검사가 잡는다 |
| `…::test_fusion_is_deterministic` | 같은 입력 두 번 → 같은 t·lagging·트랙 |
| `test_scenario_golden.py::test_lagging_camera_scenario_needs_the_fusion_time_fix` | 음성 대조: 최솟값으로 되돌리면 시나리오가 실패(해소 없음, 뺀 tick 수 0) |
| `…::test_lagging_camera_never_contributes_and_other_frame_age_is_unaffected` | D-G0-10: cam3 는 트랙 `cameras` 에 없음, 다른 카메라 frame_age p95 는 r1_enter_exit 와 같음(±10 ms) |
| `…::test_lagging_camera_keeps_r7_up_in_a_two_camera_zone_without_inventing_r1` | 리뷰 F1 통합(파일 모드 Tier 0): cam2·cam4 가 사람을 못 보고 z-r1 을 cam1·cam3 만 덮을 때 트랙·R1 없음, R7 은 133 ms 에 나서 끝까지 해소되지 않음 |
| `…::test_r7_resolves_only_after_the_lagging_camera_catches_up` | 리뷰 F1 통합: cam3 지연이 5 s 에 풀리면 트랙은 그 뒤에만, R7 해소는 회복 + 해소 유지 1.5 s 뒤에만 |

`fusion_max_lag_ms` 를 500 으로 두면(이전 동작) 위 인지 시험 4개와 시나리오가 실패하는 것을 확인했다. 감시 입력에 `fusion_excluded` 를
싣지 않으면(이 변경 전 judge) 두 F1 통합 시험이 실패한다(R7 이 2133 ms 에 '감시 복구'로 해소, 리뷰 재현과 같음).

## 잔여 위험

- **늦은 카메라와 한 대만 보는 곳의 새 진입은 R1 이 아니라 R7 로만 드러난다.** 이전(최솟값)에는 400 ms 늦은 위치로 R1 이 늦게라도 났지만,
  지금은 그 구역이 R7 감시 저하(CAUTION, 관제 안내)로 유지되고 R1 은 지연이 풀려 다시점 관측이 생긴 뒤에야 판정된다. 외삽·단안 관절로 R1 을
  살리지 않는 것이 규칙 2·명세 5.2 에 맞다고 보았다. 4대가 겹쳐 보는 구역은 영향이 없다. R7 은 발생 지속 조건 없이 즉시라 상한 근처 지연
  (`r1_late_and_reconnecting_camera` 의 120 ms 카메라처럼 가끔 한 tick 빠짐)은 관측 카메라가 정확히 2대인 구역에서 R7 을 잠깐 냈다가 해소
  유지 뒤 해소할 수 있다(깜빡임, 안전 쪽).
- **시계 오프셋으로 정상 카메라가 빠질 수 있다.** 기준이 카메라 최댓값이라 한 대만 +140 ms 앞서도 정상 위상차와 겹치면 prompt 가 2대(앞선
  카메라 + 정상 한 대)로 남아 되돌리지 않고 나머지 정상 카메라를 뺀다(리뷰 F2 반례, `test_one_clock_ahead_camera_…`). 카메라 동기화 측정
  (`sync_valid`)이 아직 없어 내부 값은 늘 True 다(health 에는 null 로 보낸다, 기존 한계). 이 제외는 감시 입력의 `fusion_excluded` 로 R7 에
  드러나므로 빠진 카메라에 기대는 구역이 '감시 정상'으로 남지 않는다(`test_clock_offset_camera_…_r7_shows_it`). G0 에서 카메라별 시계 오프셋과
  제외율을 실패 조건에 넣는다.
- 150 ms 는 SIM(25 fps)과 설계 추론 주기(10 Hz)로 정한 잠정값이다. LIVE 카메라 지연 분포(카메라별 frame_age·최신 촬영 시각 차·제외율)를
  G0 측정(-9 공유기 측정 포함)에서 보고 다시 정한다.

## 넘긴 일

- health 에 늦어서 뺀 카메라를 싣기(계약 `camera_health` 필드 또는 상태 값 추가, I1 LIVE 계약이라 Lead 승인) — 계약 담당.
- 동기화 측정 근거를 융합 eligibility 에 연결(시계가 앞선 카메라를 기준에서 빼기) — 동기화 측정 티켓.
- LIVE 지연 분포로 `fusion_max_lag_ms` 재결정 — G0 측정(RunPod 큐).
- 명세 6.1 에 융합 시각 규칙(늦은 카메라 제외)을 적을지 — 명세 정리 MR(지금 6.1 은 `t_latest_global`·gap 정의만 있고 융합 시각은 정하지 않아
  이번 변경과 어긋나지 않는다).
