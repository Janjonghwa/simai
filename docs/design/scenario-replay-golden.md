# 시나리오 재생 정답 비교 (S15P31A101-41, AI 부분)

SIM 장면을 합성 녹화로 만들어 파일 모드 Tier 0(S15P31A101-215)에 넣고, 나온 `risk_event`·`world_state` JSONL 을 시나리오
설계에서 손으로 정한 기대 이벤트 파일과 비교한다. 같은 시나리오를 두 번 돌려 출력이 같은지도 본다. 티켓의 세 번째 항목(가짜 Tier 0
송신기 → EC2 → 뷰어 Docker Compose 통합 시험)은 서버·front 몫이라 여기서 하지 않고, 그 시험이 쓸 형식만 정한다(아래 'EC2 통합 시험').

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 21.3 시나리오 재생 | 녹화·시뮬레이터 입력을 파일 모드 Tier 0 에 넣어 기대 이벤트(정답 파일)와 비교. 시계는 주입식으로 고정. CI(시뮬레이터, CPU) + RunPod(실제 모델) | CPU·SIM 부분. 실제 모델은 `gpu` 표시 자리만 |
| 21.3 머리말 | Risk Engine·Tier 0 는 시계를 주입받는다. 실제 시각에 기대는 시험을 만들지 않는다 | 재생 시계(ReplayClock, 최대 속도). 실제 시각으로 바꾸면 실패하는 반례 시험 |
| 부록 D | 시험 ID 정본, 계층 SC | 시나리오마다 D-ID |
| 7.2·6.4 | R1·R2·R3·R5·R7 판정, 발생 지속 조건, 해소 유지, 10초 재개 | 기대 시각 창의 근거 |
| 6.1 | 시간 모델(T_gap 200 ms 관측 유효, camera_stall 500 ms) | 늦은 카메라 시나리오의 기대 |
| AGENTS 규칙 5·7 | REPLAY 는 알림 채널로 안 감, 정답 데이터 지표는 제품 성능 아님 | 공통 불변식(local_alert 0, 저널 0), SIM 상한 표시 |

## 코드·시험 위치

| 위치 | 내용 |
|---|---|
| `ai/tests/scenarios/golden.py` | 장면 → 합성 녹화 → 파일 모드 Tier 0 → 비교 → 결정성. CI 대체 스크립트(종료 코드 0 통과·1 차이·2 사용법/파일 오류) |
| `ai/tests/scenarios/cases/<이름>/input.json` | 시나리오 입력: D-ID, SIM 덮어쓰기(사람·단상·길이), 구역·작업면, 장애 입력 |
| `ai/tests/scenarios/cases/<이름>/expected.json` | 기대 이벤트(손으로 정한 값). 각 창의 근거는 파일 안 `_why`·`_design` |
| `ai/tests/scenarios/test_scenario_golden.py` | 시나리오별 pytest(두 번 실행), 음성 대조·반례, 스크립트 종료 코드, `gpu` 자리 |
| `ai/tests/services/tier0/tier0_site.py` | 구성 묶음 도구에 `work_surfaces` 인자 추가(R2·R3 시나리오용, 기본값은 이전과 같음) |

실행(`ai/` 에서. Git Bash·cp949 콘솔에서는 `PYTHONUTF8=1` 을 앞에 붙인다):

```sh
uv run python tests/scenarios/golden.py                       # 전체(느린 것 포함), 시나리오마다 두 번 실행
uv run python tests/scenarios/golden.py r7_camera_disconnect  # 고른 것만
uv run python tests/scenarios/golden.py --out <빈 디렉터리>    # 재생 산출물을 남긴다
uv run pytest tests/scenarios -q                              # 빠른 시나리오(장면 20 s 이하) + 음성 대조
SIMAI_SCENARIO_SLOW=1 uv run pytest tests/scenarios -q        # 느린 시나리오도
```

실패하면 차이 줄(무엇이 기대와 다른지)과 실제 이벤트 요약(규칙·구역·등급·이정표 시각)을 찍는다.

## 흐름

```
cases/<이름>/input.json
  └─ sim.run (tier0_site 교실: 9 x 7.5 m, 모서리 카메라 4대, 시드 1) → observations/·calibration.json(PROVISIONAL, SIM)
  └─ 합성 녹화(gateway/recorder 형식, recording_session.write_session): 카메라별 H.264(프레임 번호를 화소로 새김, 256x144,
     사람 그림 없음), 헤더 = SIM 촬영 시각, 수신 = +30 ms(+카메라별 지연). 끊김 구간은 프레임이 없고 뒤는 새 epoch 세그먼트
  └─ 시작 구성 묶음(tier0_site.archive_bundle): 구역(기본 z-r1 HIGH)·규칙(R1·R2·R3·R5 교실 프로파일·R7)·SIM 보정·작업면
  └─ Tier0Service(input.mode file, speed max, decoder cpu, msg_id 순번 주입) + 검출기 대역(그림 번호 → SIM keypoint,
     detection_dropouts 구간은 검출 없음) + Local Tracker 대역(검출 순서 = 트랙 ID, 한 사람 장면)
  └─ out/world_state.jsonl, out/risk_event.jsonl → compare(expected) + 두 번째 실행과 바이트 비교(event_id 만 정규화)
```

## 시나리오

시각은 모두 장면 시작(SIM `start_time_ms` = 1760342400000) 기준 ms 다.

| 시나리오 | D-ID | 입력 | 기대 이벤트 |
|---|---|---|---|
| `r1_enter_exit` | D-G6-01, D-G6-03 | 1.2 m/s 로 R1 구역에 들어가 3 s 서 있다가 1.7 m/s 로 나감 | R5 CAUTION RAISED [0, 1167](실제 진입 2167 보다 1 s 이상 앞섬) → RESOLVED [2900, 4800]. R1 HIGH RAISED [1400, 3067] VERIFIED → RESOLVED [8300, 9300]. 그 밖 이벤트 없음 |
| `r1_resume_then_new` (slow, 31 s) | D-G6-08 | 들어감 → 나감 → 4 s 뒤 다시 들어감 → 나감 → 12 s 뒤 다시 들어감 | 이벤트 A: RAISED [1400, 3067] → RESOLVED [7300, 8300] → RESUMED [9800, 11312](같은 event_id, resume_count 1) → RESOLVED [15300, 16300]. 이벤트 B(새 event_id): RAISED [27800, 29312]. R5 는 보지 않음 |
| `r1_observation_loss` | D-G3-02 | 구역 안에 선 사람이 4~7 s 동안 모든 카메라에서 검출되지 않음 | 규칙 정답: 이벤트 A RAISED [1400, 3067], [4000, 5500] 에 UNVERIFIABLE·확인 필요, 해소는 사람이 나간 뒤 창 [12300, 13300] 에만(없어도 됨). 현재 정책(known_limitations): 다시 보인 사람은 늦은 재연결 조건을 채우지 않아 새 World ID 의 이벤트 B(RAISED [7000, 7900] → RESOLVED [12300, 13300]), A 는 해소 없음 |
| `r7_camera_disconnect` | D-G3-01, D-G3-08 | cam2·3·4 가 4~7 s 끊겼다 새 epoch 로 재접속, 구역 밖에 한 사람 | R7 CAUTION(구역 대상, World ID 없음) RAISED [4000, 5000](healthy_cameras ≤ 1) → RESOLVED [8500, 9300]. 결합 오류 0 |
| `r1_late_and_reconnecting_camera` | D-G3-08, D-G6-01 | r1_enter_exit 걸음 + cam3 120 ms 늦게 도착 + cam4 1.5~2.0 s 끊김·재접속 | R1 이 r1_enter_exit 와 같은 창. 결합 오류 0 |
| `r1_lagging_camera_unverifiable` | D-G0-10 | r1_enter_exit 걸음 + cam3 만 400 ms 늦게 도착 | 규칙 정답: R1 RAISED [1400, 3067], 해소는 나간 뒤 창 [8300, 9300] 에만(없어도 됨). 현재 한계(known_limitations, Lead 추적): 확인 필요로 발생하고 해소 없음, [6000, 10500] 에 UNVERIFIABLE |
| `r2_r3_podium_edge` | D-G2-05 | 0.4 m 단상(등록 작업면) 옆 바닥을 걷다(가장자리 0.6 m) 단상에 올라 가운데에 서고, 12 s 에 뒤 가장자리로 감. 구역 없음 | 바닥 걷기에는 없음. R2 CRITICAL RAISED [4200, 5300] → RESOLVED [6900, 8000] → RESUMED [12500, 13800]. R3 HIGH RAISED [4200, 5300] 뒤 해소 없음. 단상 위 줄에 Z_UNKNOWN·PREDICTED 가 없는 줄(height_diff_m 0.35~0.45)이 있음. R5 는 보지 않음 |

모든 시나리오의 두 실행 각각에 공통 불변식을 본다(두 번째 실행도 기대 비교까지 따로 한다. stats 는 출력 바이트에 없어 결정성 비교가 대신하지
못한다): 재생이 끝까지 돎(실패·입력 오류·디코딩 실패 0, 정상 정지), 그림 ↔ 헤더 결합 오류(misbound) 0, 판정 출처 REPLAY, local_alert 0,
저널 0(REPLAY 는 저널에 넣지 않음), 모든 줄 계약 통과, world_state 의 input_provenance.frames.source = FILE, t_latest_global 이 거꾸로 가지 않음,
그리고 모든 이벤트(보지 않는 규칙 포함)의 **전이 일관성**: RAISED·event_sequence 1·resume_count 0 으로 시작, event_sequence 가 줄마다 1 씩,
updated_at 이 거꾸로 가지 않음, RAISED 는 한 번, 해소 뒤에는 재개(resume_count + 1 인 UPDATED)만, 해소 없이 resume_count 가 바뀌지 않음,
resolved_at 은 해소 줄에만(= updated_at), 대상·출처(rule_id·rule_version·zone_id·world_ids·raised_at·gateway_id·source)가 바뀌지 않음,
대상 World ID 가 world_state 트랙에 있음. ID 교정·병합처럼 대상이 합법적으로 바뀌는 시나리오는 아직 없고, 생기면 기대 파일에 따로 선언한다.

고른 것과 뺀 것:
- G6 에서 SC 계층이고 CPU·SIM 으로 재현되는 D-G6-01·03·08 을 골랐다. D-G6-07(순간 검출)·D-G6-08 일부는 RV 계층(공통 벡터)이 정본이라
  여기서는 재개 경계만 본다. D-G6-02(R4 검출률)·D-G6-06·09(측정·기록)는 정답 비교 대상이 아니다. D-G6-04·05(VOLUME_3D)는 구역 형식을
  더 정해야 해서 다음으로 넘긴다.
- G3 에서 녹화·검출 입력으로 만들 수 있는 D-G3-01·02·08 을 골랐다. 강제 종료·재동기화·EC2 단절(D-G3-05·07·09·11~19)은 장애 주입(FI)
  계층으로 이미 각자 시험이 있다. '보정 없음'은 시작 거절(종료 코드 2)이라 -215 시험(`test_replay_refuses_to_start_without_usable_calibration`)이 본다.
- D-G0-10 은 FI 계층(측정·기록)이다. 이 시나리오는 frame_age_ms 영향을 재지 않고, 늦은 카메라가 있을 때의 규칙 정답(발생, 사람이 구역에
  있는 동안 해소 금지)만 본다. 지금 동작(해소 확인 불가)은 명세 정답이 아니라 가용성 한계라 known_limitations 로 분리했다(아래 잔여 위험).

## 기대 파일 형식 (format_version 1)

```json
{
  "scenario_id": "r1_enter_exit",          디렉터리 이름과 같다
  "d_ids": ["D-G6-01"],                     input.json 과 같다(부록 D 에 있어야 한다)
  "format_version": 1,
  "exhaustive": true,                       기본 true: 기대에 없는 이벤트가 나오면 실패
  "ignore_rules": ["R5"],                   이 규칙의 이벤트는 보지 않는다(다른 시나리오가 본다)
  "world_state": {"min_lines": 100, "max_tracks": 1},
  "events": [                               실제 이벤트와 순서대로 짝짓는다(규칙·구역이 같은 첫 이벤트)
    {"name": "R1 진입", "rule_id": "R1", "zone_id": "z-r1",
     "severity": "HIGH", "world_id_count": 1,       이벤트의 모든 줄에서 같아야 한다(등급을 내리지 않음, 대상이 바뀌지 않음)
     "transitions": [                                이정표 순서가 정확히 같아야 한다
       {"kind": "RAISED", "at_ms": [1400, 3067], "verification": "VERIFIED"},
       {"kind": "RESOLVED", "at_ms": [8300, 9300]}],
     "updates": [                                    그 구간에 그 값을 가진 줄이 하나 이상
       {"at_ms": [4000, 5500], "verification": "UNVERIFIABLE", "needs_check": true}]},
    {"name": "늦은 카메라가 있는 진입", "rule_id": "R1",
     "transitions": [{"kind": "RAISED", "at_ms": [1400, 3067]}],
     "optional_tail": [{"kind": "RESOLVED", "at_ms": [8300, 9300]}]}   transitions 뒤에 이 앞부분이 더 와도 된다
  ],
  "known_limitations": [                    지금 구현의 한계로 나는 동작(명세 정답 아님). 맞지 않으면 실패가 아니라 '!' 알림
    {"id": "slow-camera-fusion-time", "tracked_by": "S15P31A101-219",
     "events": [{"same_as": "늦은 카메라가 있는 진입", "rule_id": "R1",   events 의 그 이벤트에 덧붙여 본다
                 "transitions": [{"kind": "RAISED", "at_ms": [1400, 3067], "needs_check": true}]}]}
  ]
}
```

- **규칙 정답과 현재 한계를 나눈다.** `events` 는 명세·시나리오 설계에서 반드시 나와야 할 것과 나오면 안 되는 것이고, 어기면 실패(1)다.
  `known_limitations` 는 지금 구현의 한계로 나는 동작을 적어 둔 것이다. 맞지 않으면(한계가 고쳐짐) 실패가 아니라 `!` 알림으로 기대 파일 갱신
  대상임을 알린다(pytest 는 통과, 스크립트는 종료 코드에 넣지 않음). `same_as` 가 없는 한계 이벤트는 남은 실제 이벤트와 짝지어지고, 짝지은
  것은 예상 밖 이벤트로 세지 않는다.

- 이정표(kind): `RAISED`, `RESUMED`(같은 event_id 에서 resume_count 가 오른 UPDATED), `RESOLVED`. 수치만 바뀐 UPDATED 는 이정표가 아니다.
- 이정표 시각은 risk_event payload 의 `updated_at`(전이의 관측 시각) − 장면 시작이다. `at_ms` 는 늘 [하한, 상한] 구간이다.
- 이정표 밖 줄(수치만 바뀐 UPDATED 등)은 기대에 적지 않지만 위 '전이 일관성' 불변식이 모든 줄을 본다.
- 조건 필드: `verification`, `needs_check`, `resume_count`, `severity`, `metrics`({이름: [하한, 상한]}), `reasons_absent`(confidence_reasons 에
  없어야 할 이유).
- 밑줄로 시작하는 키(`_why`, `_design`, `_tolerance`)는 근거 메모다. 그 밖의 모르는 키는 오류(종료 코드 2)다. 오타 키가 조용히 무시돼
  조건이 비는 것을 막는다.
- 결정성: 두 실행의 `world_state.jsonl` + `risk_event.jsonl` 을 이어 붙여 바이트 비교한다. event_id·related_event_id(UUIDv7, 무작위 비트)만
  처음 나온 순서 번호로 바꾼다. msg_id 는 순번을 주입하므로 그대로 비교한다.

## 허용 오차(시각 창) 근거

명세에 시나리오 시각 허용 오차 수치는 없다. 창은 시나리오 기하와 규칙 상수에서 손으로 정했다(현재 출력을 찍지 않았다).

| 항목 | 값 | 근거 |
|---|---|---|
| 발생 하한 | 범위가 구역에 처음 닿을 수 있는 때 | 범위 = 점유 반경 0.3 m + 발끝이 골반보다 앞서는 거리(걸을 때 0.4 m 까지로 봄) + 2σ(0.2 m 까지로 봄). 중심이 구역에서 0.9 m 일 때. R2·R3 은 범위(0.3 m + 2σ)가 단상 footprint 에 걸칠 수 있는 때 |
| 발생 상한 | 중심이 경계를 넘는 때 + 900 ms | D-G6-01 목표(p95 900 ms 이하)를 시나리오 한 건의 상한으로 쓴다(발생 지속 조건 포함) |
| R5 발생 상한 | 실제 진입 − 1000 ms | D-G6-03(실제 진입보다 1초 이상 앞섬) |
| 해소 | 범위가 해소 간격 밖으로 나가는 때(이른 쪽: 반경만, 늦은 쪽: 발끝·2σ 포함) + 해소 유지 1500 ms(시나리오 설정) + 판정 주기·발생 지속 여유 | R1 해소 간격 0.3 m, R2 해소 거리 1.3 m(7.2) |
| R7 발생 상한 | 끊김 + 1000 ms | D-G3-01(1초 이내) |
| 재개·새 이벤트 경계 | 해소 뒤 10 s(resume_window_ms) 안과 밖이 창끼리 겹치지 않게 장면을 짰다 | 6.4. 해소 창 상한 16.3 s 와 새 발생 창 하한 27.8 s 사이가 11.5 s |

해소 유지 시간을 1500 ms 로 둔 것은 -215·-171 시험과 같은 값으로 장면을 짧게 하려는 것이다(운영 기본 3000 ms). 규칙 논리는 같다.

## 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 입력 경로 | SIM keypoint → 합성 H.264 녹화 → 파일 모드 Tier 0 | SIM 모드 Tier 0 는 실제 시각 기준(anchor_wall_ms·실시간 흐름)이고 JSONL 출력이 없다. 파일 모드는 녹화 시각 시계(최대 속도)라 매번 같고 LIVE 와 같은 디코딩·최신성 경로를 지난다 |
| 위치 | `ai/tests/scenarios/` | 검출기·추적기·영상 대역이 시험 도구(tests/services/tier0, tests/io)에 있다. src·services 에 시험 대역을 넣지 않는다 |
| 검출 누락 | 검출기 대역이 구간·카메라에서 검출을 내지 않음 | 녹화 프레임은 그대로 와서 카메라 연결·감시(R7)는 정상이고 사람만 안 보인다(가림과 같은 모양) |
| 카메라 끊김 | 녹화 세그먼트 공백 + 새 epoch | -215 재생이 세그먼트 끝을 연결 끝(I0Ingest.connection_lost)으로 넘긴다(LIVE 재접속과 같은 경로) |
| 느린 시나리오 | 장면 20 s 초과는 marker `slow`, `SIMAI_SCENARIO_SLOW=1` 일 때만 pytest | 전체 pytest 시간을 늘리지 않는다. 스크립트는 늘 전체를 돈다 |
| GPU | marker `gpu` 시험 자리만(skip) | 합성 영상에는 사람 그림이 없어 실제 모델 입력이 될 수 없다 |
| 결정성 정본 | 저장한 스냅숏 없이 같은 실행 안에서 두 번 돌려 비교 | 지시서: 찍은 값은 결정성 비교용으로만. 찍은 파일을 저장하면 정답처럼 쓰일 위험이 있다 |

## 시험

| 시험 | 보는 것 |
|---|---|
| `test_scenario_matches_expected_and_is_deterministic[...]` | 시나리오별 기대 비교 + 두 번 실행 동일 |
| `test_every_case_names_appendix_d_ids_and_hand_written_windows` | D-ID 가 부록 D 에 있고, 전이마다 구간 창이 있다 |
| `test_negative_control_changed_expected_event_fails` | 음성 대조: 등급·시각 창·전이·이벤트 하나를 바꾸면 실패 |
| `test_negative_control_changed_rule_threshold_fails` | 음성 대조: 해소 유지 1.5 → 4 s 로 바꿔 다시 돌리면 해소가 창에 없다 |
| `test_negative_control_injected_auto_resolve_is_caught` | 반례(모르면 안전 경로): 관측 상실 중 해소 줄을 끼워 넣으면 실패 |
| `test_counterexample_real_wall_clock_breaks_the_comparison` | 반례: 재생 시계를 실제 시각으로 바꾸면 비교 실패 |
| `test_counterexample_misbound_frames_break_the_invariant` | 반례(D-G3-08): 그림과 헤더를 한 장씩 어긋나게 하면 misbound 불변식 실패 |
| `test_determinism_check_is_not_vacuous` | msg_id 를 무작위로 내면 결정성 비교가 실패(정규화가 차이를 덮지 않음) |
| `test_negative_control_illegal_non_milestone_transitions_fail` | 리뷰 F1 반례: 해소 뒤 재개 없는 UPDATED, event_sequence 반복, 대상 World ID 바꿔치기(계약은 통과)가 전이 일관성으로 실패 |
| `test_second_run_invariants_are_checked_too` | 리뷰 F2 반례: 두 번째 실행만 local_alert·저널·misbound 가 있어도 실패 |
| `test_known_limitation_is_separate_from_rule_truth` | 리뷰 F3: 늦은 카메라 한계가 고쳐져 나간 뒤 해소되면 통과 + 갱신 알림, 구역 안에 있을 때 해소되면 실패 |
| `test_typo_in_expected_file_is_an_error_not_a_silent_pass` | 기대 파일 키 오타는 오류(2) |
| `test_script_exit_codes` | 스크립트 종료 코드 0/1/2, `--out` 산출물, 비어 있지 않은 `--out` 거절 |

## 버린 대안

- **SIM 모드 Tier 0 로 돌리기.** 시계가 실제 시각(anchor_wall_ms, 실시간 배속)이라 결과가 매번 같지 않고 JSONL 출력이 없다.
- **WorldTrackProvider SIM(`world_tracks.jsonl`)을 Risk Engine 에 바로 넣기.** 공통 벡터(RV)와 겹치고, Tier 0 의 디코딩·최신성·추적·접지·
  감시(R7) 경로를 지나지 않아 21.3 '파일 모드 Tier 0' 가 아니다.
- **현재 출력을 찍어 정답으로 저장하기.** 버그도 정답이 된다. 기대 파일은 구간과 이정표로 손으로 적고, 결정성은 두 번 실행으로 본다.
- **모든 UPDATED 줄을 기대에 적기.** 수치 갱신 간격(metrics_update_interval_ms) 같은 구현 세부에 묶여 정답이 쉽게 깨진다. 이정표와 구간 조건만 적는다.
- **여러 사람 장면.** Local Tracker 대역이 검출 순서를 트랙 ID 로 쓰므로 가림으로 검출 수가 바뀌면 ID 가 섞인다. 실제 추적기(TrackTrack)
  없이 만든 다인 장면은 대역 결함을 시험하게 된다. 한 사람 장면만 둔다.

## 잔여 위험

- SIM 상한이다. 검출기·추적기 대역이라 검출 잡음·ID 전환·실제 영상 디코딩 부하가 없다(AGENTS 규칙 7, 제품 성능으로 쓰지 않음).
- 시각 창은 손으로 정한 구간이라 규칙 상수(점유 반경·해소 간격·발생 지속 조건)를 바꾸면 창을 다시 따져야 한다. 그때 기대 파일의
  `_why` 를 같이 고친다.
- **늦은 카메라 하나가 전체를 PREDICTED 로 만든다**(`r1_lagging_camera_unverifiable`, 현재 한계 `slow-camera-fusion-time`, S15P31A101-219). perception.py 가 융합 시각을 camera_stall_ms(500) 안 카메라의 최신 촬영 시각 최솟값으로 골라, 400 ms 늦은 카메라 하나에
  융합이 묶이고 T_gap(t_latest_global − 마지막 유효 관측)이 관측 유효 간격 200 ms 를 넘는다. 6.1 상 PREDICTED·해소 금지는 보수적 동작이지만
  최신 카메라 3대가 있어도 느린 1대에 묶이는 것은 명세 정답이 아니라 가용성 한계다. 기대 파일은 이를 규칙 정답에서 빼고 known_limitations 로만
  적었다(고치면 '!' 알림).
- **D-G3-03 충족(S15P31A101-218).** SIM 보정은 PROVISIONAL 이라 Tier 0 가 활성 보정 상태를 판정 문맥으로 넘기고(world_state 계약은 그대로)
  R2·R3 의 모든 줄에 `PROVISIONAL_CALIB` 가 붙는다(명세 4.4·부록 D-G3-03 100%). `r2_r3_podium_edge` 기대는 이벤트 키 `reasons_every_line`
  (그 이벤트의 모든 줄 confidence_reasons 에 있어야 할 이유)로 이를 보고, 전이 줄은 needs_check true 다.
- 관측 상실 뒤 다시 보인 사람이 새 World ID 가 되는 것은 얼굴 재식별 금지(규칙 8) 때문이 아니라, World Track 늦은 재연결 조건(서로 다른 카메라
  2대의 로컬 트랙 이력, S15P31A101-40)을 추적기 대역으로는 채우지 않기 때문이다. 그래서 그 부분도 known_limitations(현재 정책)로 두었다.
- 재생 산출물은 커밋하지 않는다. 다시 만들면 event_id 만 다르다.

## EC2 통합 시험(백엔드 몫)이 쓰는 방법

- 산출물: `golden.py --out <디렉터리>` → `<디렉터리>/<시나리오>/run-1/out/world_state.jsonl`, `risk_event.jsonl`. 각 줄은 계약 봉투 그대로다
  (`contracts/messages/world_state`, `contracts/messages/risk_event` 2.1.0). world_state 의 msg_id 는 순번 UUID, sent_at 은 녹화 시각이다.
- world_state 줄은 I1 world_state 와 같은 메시지라 가짜 Tier 0 송신기가 그대로(필요하면 sent_at·capture 시각만 지금 시각으로 옮겨) 보낼 수 있다.
- risk_event 줄은 출처 REPLAY 의 뷰어 projection 이다. LIVE 알림 경로(risk_event_transition·notification) 시험 입력으로 출처를 LIVE 로 바꿔
  보내지 않는다(AGENTS 규칙 5). 뷰어 재생·리플레이 표시 시험의 입력과 기대로 쓴다.
- 기대: 같은 `expected.json` 을 쓴다. 시각은 장면 시작(1760342400000) 기준 상대 ms 라 송신 시각을 옮겨도 그대로 쓸 수 있고, 뷰어가 받은
  이벤트를 이정표(RAISED·RESUMED·RESOLVED)·규칙·구역·등급으로 줄여 같은 비교를 하면 된다.
- 산출물은 다시 만들 수 있고(같은 입력이면 event_id 외 같음) 커밋하지 않는다. 통합 시험 CI 는 이 스크립트를 먼저 돌려 산출물을 만든다
  (av·numpy 가 있는 ai dev 환경).

## 남은 일

- 티켓 세 번째 항목: 가짜 Tier 0 송신기 → EC2 → 뷰어 Docker Compose 통합 시험(서버·front 준비 뒤, 백엔드).
- RunPod 실제 모델 시나리오 재생(`gpu` 자리): 동의받은 시나리오 녹화(사람 있음)와 손으로 적은 기대 파일이 필요하다(-183 큐).
- D-G6-04·05(VOLUME_3D 구역), R5 가장자리 접근 예측의 기대, 여러 사람 장면(실제 추적기 필요).
- 명세 정리(-217): 21.3 시나리오 재생의 허용 오차 기준(이 문서의 표)과 R2 시나리오 D-ID(부록 D G6 에 R2 항목이 없어 D-G2-05 로 가리킴).
