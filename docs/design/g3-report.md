# G3 실패 대응 보고서 (S15P31A101-113 Tier 0 항목 · S15P31A101-228 노트북·EC2 항목)

명세 15.1 G3 행과 부록 D.4(D-G3-01~20)의 항목별 결과와 근거를 한곳에 모은다. Tier 0 항목은 S15P31A101-113 이 시험했고(2026-10-26, 노트북 PC,
GPU 없음), 노트북·EC2 항목은 S15P31A101-228 이 아래 형식으로 채운다. 결과 원본(result.json·로그)은 저장소에 두지 않는다.

## 15.1 G3 행과 부록 D.4 의 관계

| 15.1 G3 행 | 기준 | 부록 D.4 에서 보는 항목 |
|---|---|---|
| 노트북↔RunPod·EC2 단절 표시(하트비트 3회 누락) | 현장 5 s, EC2 10 s | D-G3-17(감지), D-G3-12(EC2 만 끊김을 관제 중단으로 잘못 표시하지 않음) |
| EC2 단절 중 Tier 0 판정·현장 경보 유지, 복구 후 재동기화 누락 | 유지 100%, 누락 0건 | D-G3-05·07·09·11·13·14·15·16·18·19·20 (판정 유지는 D-G3-12 의 Tier 0 몫, 누락 0건은 저널·outbox 항목) |
| 비LIVE 출처·시험 알림이 실제 알림으로 기록·발송 | 0건(DB 제약 시험) | 부록 D.4 밖(EC2 DB 제약, -228 또는 서버 시험). 판정·관측 쪽(D-G3-01~04·08·10)은 G3 의 판정 정확성 전제 |

G3 는 15.1 세 행이 모두 기준을 만족하고 부록 D.4 의 각 항목이 목표를 만족해야 통과한다. 이 문서의 판정은 부록 D.4 항목 단위다.

## 장애 주입 공통 절차

1. **상태 디렉터리.** Tier 0 상태(저널 세그먼트·acked_offset·체크포인트·클립 임시 저장)는 한 디렉터리(`--volume-dir`, Pod 은 남는 디스크)에 둔다.
   시나리오마다 `<volume-dir>/<UTC>/` 새 하위 디렉터리에서 시작한다.
2. **주입.** 정해진 지점까지 진행한 뒤 프로세스를 강제로 끝낸다(POSIX `kill -9`, Windows TerminateProcess). 정리 코드는 돌지 않는다.
   지점은 두 종류다: (a) 실제 Tier 0 프로세스(`python -m services.tier0`, SIM 입력, 로컬 wss EC2 흉내·노트북 시험 상대)에서 관측되는 사건
   (local_alert 수신 직후, alarm_stop 직후, 체크포인트 임시 파일이 보일 때), (b) 결정적 지점(`ai/tests/recovery/crash_child.py` 가 지점까지 가서 멈추면 kill).
   단절은 I1·I0 연결을 끊거나(EC2 흉내 정지) 카메라 epoch 를 바꾸는 시나리오 입력으로 넣는다.
3. **재시작.** 같은 상태 디렉터리로 다시 띄운다. 첫 health 는 재관측 전 UNSAFE_UNKNOWN 이어야 한다.
4. **정답 비교.** 종료 시점 저널을 payload 로만 접은 정답(`site_driver.journal_truth`)과 (a) 사본 recover 결과, (b) 다시 띄운 서비스의 RESTORED 전이·
   event_state_snapshot, (c) EC2 흉내가 받은 전이(같은 msg_id·오름차순·누락 없음)를 비교한다. 위반은 항목별로 세고, 시나리오가 돌지 못하면
   위반 0건으로 세지 않고 오류(판정 불가)로 남긴다.
5. **기록.** 명령·커밋·날짜·시나리오 수·항목별 위반 수·오류 수·걸린 시간. 원본은 실행 사이트 저장 위치(Pod `simai/runs/crash92/`, 노트북 사본은 커밋하지 않음).

실행 명령(노트북 드라이런, `ai/` 에서): `uv run python ../deploy/scripts/crash_recovery_g3.py --volume-dir <짧은 경로>`
(Windows 는 경로 260자 한도 때문에 짧은 경로를 쓴다). 결정적 지점만은 `uv run python -m pytest tests/recovery/test_crash_recovery.py`.

## S15P31A101-228 이 채울 형식

228 항목은 아래 열로 채운다. 결과는 PASS(목표 충족)·FAIL(위반 n건)·근거 없음·환경 필요(무엇이 필요한지) 중 하나다.

| 항목 | 측정값(목표 대비) | 결과 | 근거(시험 파일::함수 또는 절차·로그 위치) | 실행 명령 | 날짜·커밋 | 담당 |
|---|---|---|---|---|---|---|
| D-G3-05 EC2 몫(재알림·DELAYED_ACTIVE) | 동기화 중 해소 이벤트 실시간 송신 0건(목표 0건), 아직 활성인 이벤트 DELAYED_ACTIVE 표시·송신 100%, 감사용 전이 알림 대기열 0건 | PASS | server `RiskEventJournalIntegrationTest::동기화_중_해소된_이벤트는_AUDIT_ONLY_아직_활성이면_DELAYED_ACTIVE_그_뒤는_REALTIME`·`근거_없는_활성_이벤트는_알리지_않고_AUDIT_ONLY_로_둔다`·`끝나지_못한_동기화에서_만든_이벤트도_다음_스냅샷에서_분류한다`, `StompRelayIntegrationTest::동기화_중_해소된_이벤트는_보내지_않고_DELAYED_ACTIVE_와_그_뒤_실시간_전이를_보낸다`, `NotificationQueueIntegrationTest::비LIVE_해소_CAUTION_감사용_모르는_전이와_틀린_요청은_대기열에_넣지_않는다` | 서버 ①(아래 실행 명령) | 2026-11-02, dev `57aa47f` | 곽나혁 |
| D-G3-06 표시 몫(UNVERIFIED) | EC2: Tier 0 단절 중 `viewer_health.config.sync` = UNVERIFIED 100%. 관제 앱 화면: `config.sync` 를 받기만 하고(`viewer3d/data/contractAdapter.ts`) 보이는 곳이 없음 | EC2 PASS · 화면 근거 없음 | server `HealthIntegrationTest::Tier0_만_끊기면_노트북이_본_상태로_원격_단절과_관제_정지를_가른다`(`/payload/config/sync` UNVERIFIED). 화면 표시는 관제 앱 과제(남은 일) | 서버 ① | 2026-11-02, dev `57aa47f` | 곽나혁 |
| D-G3-12 노트북 몫(관제 중단 오표시) | EC2 만 끊김 → 현장 표시 원격 장애(관제 중단 아님)·현장 경보 계속 100%, EC2 한 번 실패는 장애 아님, Tier 0·EC2 동시 상실만 관제 중단 | PASS | `gateway/site_alert/tests/test_link_status.py::test_ec2_only_down_is_remote_fault_and_site_alarm_keeps_working`·`test_single_ec2_failure_is_not_a_fault`·`test_simultaneous_tier0_and_ec2_loss_shows_control_halted` | 게이트웨이 ② | 2026-11-02, dev `57aa47f` | 곽나혁 |
| D-G3-15 노트북·EC2 몫 | 노트북: 버튼 확인은 Tier 0 제어 채널로만 보내고(EC2 와 무관한 것은 경로 구조상이며 EC2 단절 상태에서 버튼을 누르는 시험은 없음) Tier 0 가 끊겼으면 재접속 때 보냄, `alarm_stop` 으로 소리 정지·표시 유지 100%. EC2: 단절 중 저널에 남은 ACKNOWLEDGED(SITE_BUTTON)가 재연결 동기화로 `event_ack`·이벤트 상세 `acks[]` 에 반영 100% | PASS | 게이트웨이 `test_transport_app.py::test_websocket_delivers_alert_and_sends_site_ack`·`test_site_ack_while_disconnected_is_sent_on_reconnect`, `test_core.py::test_alarm_stop_stops_sound_keeps_display`, `test_link_status.py::test_ec2_only_down_is_remote_fault_and_site_alarm_keeps_working`. server `EventAckIntegrationTest::Tier0_저널에_남은_현장_버튼_확인은_재연결_동기화로_EC2_에_반영된다`(이 MR 에서 더함)·`현장_버튼_확인은_전이가_오면_event_ack_로_남는다` | 게이트웨이 ②, 서버 ① | 2026-11-02, dev `57aa47f` + 이 MR | 곽나혁 |
| D-G3-16 노트북 몫(목록 맞춤) | 재접속·`local_alert`/`alarm_stop` 유실 뒤 활성 현장 경보 목록으로 소리·표시 맞춤(놓친 경보 울림, 놓친 정지·해소 반영, 낮은 sequence 로 되돌리지 않음, 목록 없는 health 로 지우지 않음) 100% | PASS | `gateway/site_alert/tests/test_resync.py`(229 cases, 예: `test_missed_local_alert_is_shown_and_sounds`·`test_missed_alarm_stop_is_applied_from_list`·`test_missed_resolved_is_cleared_from_list`·`test_health_without_list_never_clears`) | 게이트웨이 ② | 2026-11-02, dev `57aa47f` | 곽나혁 |
| D-G3-17 반쯤 열린 TCP·처리 정체 감지 | 소켓은 열린 채 health 끊김 → 3.5 s 이내 관제 중단·제어 채널 1회 재설정, `t_latest_global` 정지 → 현장 5 s 이내 관제 중단(목표 하트비트 3회 누락·현장 5 s). EC2 쪽도 처리 정체를 관제 정지로 기록 | PASS | `gateway/site_alert/tests/test_link_status.py::test_tier0_silent_with_open_socket_halts_within_5s_and_resets_channel`·`test_processing_stall_halts_within_5s_even_though_health_arrives`·`test_flapping_control_channel_never_shows_normal`, server `HealthIntegrationTest::health_는_오는데_처리가_멈추면_관제_정지로_보고_장애를_기록한다`(보조: `노트북이_끊기면_10초_안에_현장_장비_응답_없음으로_기록한다`, EC2 10 s 기준), 실제 시간 종단 `test_heartbeat_e2e.py`(가짜 시계 시험 외 1건) | 게이트웨이 ②, 서버 ① | 2026-11-02, dev `57aa47f` | 곽나혁 |
| D-G3-20 EC2 단절 구간 리플레이 "데이터 없음" | 서버: Tier 0 health 가 3 s 넘게 없던 구간을 `gaps` 로 줌(시험). 그 구간 world_state 를 만들지 않는 것(보간 0건)은 저장된 행만 돌려주는 구조(`ReplayRepository`)에서 오는 것이고 시험이 따로 단언하지는 않음. 화면: 서버가 준 기록 없는 구간에서만 "데이터 없음", 기록 사이가 비어도 스스로 만들지 않음 100% | PASS | server `ReplayIntegrationTest::Tier0_health_가_3초_넘게_없던_구간을_기록_없음으로_준다`, 관제 앱 `__tests__/features/history/replay.test.ts`("기록 없는 구간" 6건)·`Timeline.test.tsx`("데이터 없음" 글 3건) | 서버 ①, 관제 앱 ③ | 2026-11-02, dev `57aa47f` | 곽나혁 |
| 15.1 G3 셋째 행(비LIVE·시험 알림이 실제 알림으로 기록·발송) | 0건(목표 0건): 비LIVE·해소·CAUTION·감사용 전이는 대기열에 넣지 않음, DB 가 비LIVE 이벤트의 알림 행을 직접 넣어도 거부, 발송 워커가 REPLAY 를 한 건도 보내지 않음. 노트북도 비LIVE 출처는 울리지 않음 | PASS | server `NotificationQueueIntegrationTest::DB_는_비LIVE_이벤트의_알림_행을_직접_넣어도_거부한다`·`발송_워커는_REPLAY_이벤트를_한_건도_보내지_않고_LIVE_만_보낸다`·`비LIVE_해소_CAUTION_감사용_모르는_전이와_틀린_요청은_대기열에_넣지_않는다`, 게이트웨이 `test_core.py::test_non_live_source_rejected` | 서버 ①, 게이트웨이 ② | 2026-11-02, dev `57aa47f` | 곽나혁 |

실행 명령(2026-11-02, Windows 노트북, 임시 PostgreSQL `simai-ci-db`):

- ① 서버(`server/`): `SPRING_DATASOURCE_URL=jdbc:postgresql://localhost:5432/simai_test_228 ./gradlew test --tests '*EventAckIntegrationTest' --tests '*RiskEventJournalIntegrationTest' --tests '*StompRelayIntegrationTest' --tests '*HealthIntegrationTest' --tests '*ReplayIntegrationTest' --tests '*NotificationQueueIntegrationTest'` → EventAck 6·RiskEventJournal 7·StompRelay 9·Health 8·Replay 5·NotificationQueue 6, 실패 0
- ② 게이트웨이(`gateway/site_alert/`): `uv run --frozen pytest -q tests/test_link_status.py tests/test_resync.py tests/test_core.py tests/test_transport_app.py tests/test_heartbeat.py tests/test_heartbeat_e2e.py` → 333 passed(link_status 25·resync 229·core 50·transport_app 8·heartbeat 20·heartbeat_e2e 1)
- ③ 관제 앱(`front/apps/admin/`): `npx vitest run src/__tests__/features/history/replay.test.ts src/__tests__/features/history/Timeline.test.tsx` → 77 passed

참고로 저장소에 이미 이 항목을 부르는 시험이 있다(228 판정 전, 113 은 돌리지 않음): `gateway/site_alert/tests/test_link_status.py`(D-G3-12·17),
`gateway/site_alert/tests/test_resync.py`(D-G3-16), server `RiskEventJournalIntegrationTest`·`StompRelayIntegrationTest`(D-G3-05).

## D-G3-01~20 결과

계층: SC 시나리오(골든 재생), RV 회귀 벡터, IT 통합, FI 장애 주입, UT 단위(명세 21장). 날짜는 모두 2026-10-26, 기준 커밋은 dev `b9a882f` + 이 MR.
담당을 나눈 항목(05·06·12·15·16)은 Tier 0 몫(113)과 노트북·EC2 몫(228)이 **둘 다 PASS 여야 항목 PASS** 다. 한쪽만 PASS 면 항목은 미완이다.

| 항목 | 목표 | 담당 | 계층 | 근거 | 결과 |
|---|---|---|---|---|---|
| D-G3-01 카메라 분리 → 구역 저하 표시(R7) | 1초 이내 | 113 | FI(SC 로 재현) | `ai/tests/scenarios/cases/r7_camera_disconnect`(R7 발생 창 끊김 + 1000 ms), `test_scenario_golden.py::test_scenario_matches_expected_and_is_deterministic[r7_camera_disconnect]`, `docs/design/scenario-replay-golden.md` | PASS |
| D-G3-02 구역 내 관측 상실 자동 해소(R6) | 0건 | 113 | SC | `cases/r1_observation_loss`(4~7 s 전 카메라 미검출 중 UNVERIFIABLE·해소 없음), `tests/tracking/test_world_track_reconnect.py` | PASS |
| D-G3-03 PROVISIONAL 시 높이 규칙 확인 필요 | 100% | 113 | RV | 벡터 `r2_r3_podium_provisional_calib`·`r4_fall_floor_provisional_*`, `tests/risk/test_provisional_calib.py`, `cases/r2_r3_podium_edge`(모든 줄 `PROVISIONAL_CALIB`) | PASS |
| D-G3-04 Tier 0 와 EC2 컨테이너 Risk Engine 동일성 | 불일치 0건 | 113 | RV | 같은 환경: 공통 벡터 57/57(`python -m simai_ai.risk.vectors tests/risk/vectors`), `tests/risk/test_vectors.py`, `tests/deploy/test_risk_plan_roundtrip.py`, `tests/services/risk_engine`. 두 이미지 비교 `deploy/scripts/risk_engine_g304.sh` 는 이미지가 없어 돌리지 않음(CI 단계도 아직 없음, `Jenkinsfile`) | 환경 필요(두 이미지 비교 전까지 G3 통과로 세지 않음. 같은 환경 코드 동일성은 통과) |
| D-G3-05 재동기화 중 해소 이벤트 재알림·DELAYED_ACTIVE 누락 | 0건 | 113(Tier 0 스냅샷 내용) · 228(EC2 재알림) | FI | Tier 0: `tests/services/tier0/test_event_snapshot.py`, `test_i1_resync_wss.py::test_ec2_outage_keeps_judging_and_site_alerts_then_resyncs_without_gap` | PASS(Tier 0 몫·EC2 몫 모두, 228 칸 참고) |
| D-G3-06 오프라인 중 구성 묶음 유지·UNVERIFIED, 복구 후 원자적 전환 | 100% | 113(Tier 0 ConfigSync) · 228(표시) | IT | `tests/config/test_sync.py`(부록 D-G3-06 시험 포함) | 미완: Tier 0 몫 PASS · EC2 viewer_health PASS · 관제 앱 화면 표시 근거 없음(228 칸) |
| D-G3-07 Tier 0 Pod 교체 중 미전송 이벤트 유실 | 0건 | 113 | FI | 같은 상태 디렉터리 재시작 기준: `tests/eventlog/test_crash.py`·`test_outbox.py`(acked_offset 이후 같은 msg_id 재전송). 실제 Pod 교체(다른 인스턴스·디스크 재부착)는 미시험 | GPU 세션 필요(아래 큐 항목안) |
| D-G3-08 I0 재접속·역순·누락 시 프레임-시각 오결합 | 0건 | 113 | FI(SC 로 재현) | `cases/r7_camera_disconnect`·`r1_late_and_reconnecting_camera`(결합 오류 0), 반례 `test_scenario_golden.py::test_counterexample_misbound_frames_break_the_invariant` | PASS |
| D-G3-09 재시작: 자동 해소·재관측 전 UNSAFE_UNKNOWN·related_event_id 누락 | 0건 | 113 | FI | `crash_recovery_g3.py` 서비스 시나리오(첫 health·복원 이벤트 해소 검사), `tests/recovery`(health_gate·restore), `tests/services/tier0/test_tier0_restore_link.py`, `test_tier0_judge_r7_restore.py`, 운영자 종료·상향 재시작 시험 | PASS(노트북) · 실제 저장 위치 실측은 GPU 세션 필요 |
| D-G3-10 ID 교정 후 진행 중 위험 자동 해소 | 0건 | 113 | SC | `tests/tracking/test_world_track_reconnect.py`(D-G3-10 병합), `test_world_track_split.py`(D-G3-10 분할) | PASS |
| D-G3-11 체크포인트 기록 중·전이 직후 강제 종료 복구 불일치 | 0건 | 113 | FI | `crash_recovery_g3.py`(서비스 체크포인트 중 3회, 결정적 지점 5개 + seed 4), `tests/recovery/test_crash_recovery.py`(운영자 상향 직후 지점 추가, -113) | PASS(노트북) · 실제 저장 위치 실측은 GPU 세션 필요 |
| D-G3-12 EC2 만 끊겼을 때 관제 중단 오표시·현장 경보 정지 | 0건 | 113(Tier 0 판정·현장 경보 유지) · 228(노트북 표시) | FI | Tier 0: `test_i1_resync_wss.py::test_ec2_outage_keeps_judging_and_site_alerts_then_resyncs_without_gap`(EC2 단절 중 판정·현장 경보 목록 계속) | PASS(Tier 0 몫·노트북 몫 모두, 228 칸 참고) |
| D-G3-13 ACK 후 outbox 정리 직후 강제 종료 시 해소 이벤트 부활 | 0건 | 113 | FI | `crash_recovery_g3.py` harness_after_compact, `tests/eventlog/test_crash.py`, `tests/recovery/test_compaction.py` | PASS(노트북) |
| D-G3-14 저널 기록 직후·전송 직전 강제 종료 시 미전달 | 0건 | 113 | FI | `crash_recovery_g3.py`(서비스 시나리오 EC2 전달 확인), `tests/eventlog/test_crash.py`(기록 직후·전송 직전) | PASS(노트북) |
| D-G3-15 EC2 단절 중 현장 확인 버튼 소리 정지, 복구 후 EC2 반영 | 100% | 113(Tier 0 기록·재전송) · 228(노트북 버튼·EC2 반영 화면) | FI | Tier 0: `test_i1_resync_wss.py`(site_ack 저널 기록 → 복구 후 EC2 수신) | PASS(Tier 0 몫·노트북·EC2 몫 모두, 228 칸 참고) |
| D-G3-16 노트북 재접속·신호 유실 후 현장 소리·표시가 활성 경보 목록과 일치 | 100% | 113(Tier 0 목록 송신) · 228(노트북 맞춤) | FI | Tier 0: `tests/services/tier0/test_site_alert_sync.py`(재접속 즉시 목록, 1 Hz, 송신 시점 상태) | PASS(Tier 0 몫·노트북 몫 모두, 228 칸 참고) |
| D-G3-17 반쯤 열린 TCP·처리 정체 감지(하트비트 3회 누락) | 100% | 228 | FI | `gateway/site_alert/tests/test_link_status.py`, server `HealthIntegrationTest`(228 칸) | PASS |
| D-G3-18 현장 경보는 fsync 를 기다리지 않고 EC2 전송은 fsync 뒤 | 100% | 113 | UT, FI | `tests/eventlog/test_writer.py`(D-G3-18), `test_outbox.py`(fsync 끝난 기록만), `crash_recovery_g3.py`(EC2 로 나간 전이가 저널에 없음 0건) | PASS |
| D-G3-19 EC2 단절 중 CRITICAL·HIGH 클립 로컬 저장, 복구 후 업로드 | 100% | 113 | FI | `tests/services/tier0/test_event_clips.py::test_ec2_outage_spools_to_network_volume_and_uploads_after_recovery`·`test_spooled_clips_are_restored_after_restart`·`test_critical_after_restart_holds_the_spooled_high_clip` 외 | PASS(노트북, 가짜 서명·전송) · 실제 EC2 업로드는 서명 API 연결 뒤 |
| D-G3-20 EC2 단절 구간 리플레이 "데이터 없음" | 100% | 228 | IT | server `ReplayIntegrationTest`, 관제 앱 `replay.test.ts`·`Timeline.test.tsx`(228 칸) | PASS |

## 실행 기록 (2026-10-26, Windows 노트북, GPU 없음)

| 명령(`ai/` 에서, `uv run python -m pytest … -m "not gpu"`) | 항목 | 결과 | 시간 |
|---|---|---|---|
| `../deploy/scripts/crash_recovery_g3.py --volume-dir <짧은 경로>`(운영자 상향 지점 추가 뒤) | 09·11·13·14·18 | PASS, 시나리오 15, 오류 0, 항목 위반 모두 0건 | 40 s |
| 같은 명령(추가 전, 긴 경로) | 같음 | PASS, 시나리오 14, 오류 0 | 43 s |
| `tests/scenarios` + 느린 시나리오(`SIMAI_SCENARIO_SLOW=1 -m slow`) | 01·02·03·08 | 23 passed(+ 느린 1 passed), GPU 1 deselected | 63 s + 38 s |
| `tests/risk/test_provisional_calib.py tests/risk/test_vectors.py` | 03 | 145 passed | 1 s |
| `tests/risk/test_vectors.py tests/deploy/test_risk_plan_roundtrip.py tests/services/risk_engine`, `python -m simai_ai.risk.vectors tests/risk/vectors` | 04 | 177 passed, 벡터 57/57 | 5 s |
| `tests/services/tier0/test_event_snapshot.py` | 05 | 26 passed | 7 s |
| `tests/services/tier0/test_i1_resync_wss.py` | 05·12·15 | 2 passed | 3 s |
| `tests/config/test_sync.py` | 06 | 26 passed | 9 s |
| `tests/eventlog/test_crash.py test_journal.py test_outbox.py` | 07·11·13·14·18 | 87 passed | 4 s |
| `tests/recovery` | 09·11·13 | 85 passed(지점 추가 뒤 `test_crash_recovery.py` 9 passed) | 11 s |
| `tests/services/tier0/test_tier0_judge_r7_restore.py tests/risk/test_restored_zone_event.py`, `test_tier0_restore_link.py` | 09 | 21 passed, 7 passed | 1 s, 45 s |
| `tests/tracking/test_world_track_reconnect.py test_world_track_split.py` | 10 | 59 passed | 10 s |
| `tests/services/tier0/test_site_alert_sync.py` | 16 | 21 passed | 0.2 s |
| `tests/eventlog/test_writer.py` | 18 | 5 passed | 0.2 s |
| `tests/services/tier0/test_event_clips.py` | 19 | 53 passed | 1 s |
| 운영자 종료·상향 재시작·판정·E2E(-200·-272) | 09·11 | 15 passed | 16 s |
| `tests/deploy/test_crash_recovery_g3.py`(지점 개수 기대값 4→5 고친 뒤) | 09·11 하네스 | 15 passed | 75 s |

관찰: 긴 경로(스크래치 폴더, 260자 경고)에서 crash_recovery_g3 를 네 번 중 한 번 돌렸을 때 서비스 시나리오가 "R1 local_alert 를 기다리다 시간 초과"(60 s)로
돌지 못했다(ERROR, 위반으로 세지 않음). 같은 코드로 짧은 경로·긴 경로 모두 다시 PASS 였다. 재현 조건은 확정하지 못했다(아래 남은 일).

## GPU 세션이 필요한 항목

- **D-G3-07 Pod 교체(3차 세션 큐 항목안, 큐 파일은 박종화 소관이라 여기서 고치지 않는다).**
  1. 인스턴스 A 에서 Tier 0 를 남는 디스크의 상태 디렉터리로 띄우고, EC2 흉내(또는 시험 EC2)의 I1 을 끊은 채 SIM 입력으로 전이를 만든다(outbox 에 acked_offset 이후 기록이 쌓임).
  2. A 를 `kill -9` 로 끝내고 디스크를 떼어 인스턴스 B 에 붙이거나(블록 스토리지), 상태 디렉터리를 B 로 옮긴다(사이트별).
  3. B 에서 같은 상태 디렉터리로 Tier 0 를 띄우고 EC2 를 다시 연결한다.
  4. 판정: A 에서 저널에 기록된 전이가 모두 B 에서 EC2 로 전달(같은 msg_id, 오름차순, event_sequence 빈칸 없음), 그 뒤 event_state_snapshot, 첫 health UNSAFE_UNKNOWN,
     A 의 활성 이벤트 자동 해소 0건. 위반 0건이면 PASS. 디스크를 옮기지 못한 경우(새 디스크)는 유실이므로 절차상 금지 사항으로 적는다.
  5. 필요한 것: `crash_recovery_g3.py` 서비스 시나리오를 "끝낸 뒤 다른 호스트에서 재시작"으로 나눠 부를 수 있는 옵션(또는 새 스크립트 `deploy/scripts/pod_swap_g307.py`),
     사이트의 디스크 재부착 절차(deploy/README).
- **D-G3-09·11·13·14 실제 저장 위치 실측(명세 19.1, S15P31A101-92 e 항목):** 시연 사이트 블록 스토리지에서 `crash_recovery_g3.py --volume-dir <블록 스토리지>/simai/runs/crash92`.
  저널 fsync 지연·원자적 rename·잠금 해제 지연을 노트북 결과와 비교한다(1차 세션에서 엘리스 블록 스토리지 세션으로 옮긴 항목, `runpod-environment.md` 큐 e).
- **D-G3-04 두 이미지 비교:** GPU 는 필요 없지만 Tier 0 통합 이미지(CUDA)가 있어야 한다. 이미지를 빌드한 세션(또는 CI 단계 추가)에서 `bash deploy/scripts/risk_engine_g304.sh simai/risk-engine:<TAG> simai/tier0:<TAG>`.

## 남은 일

- 228 항목은 2026-11-02 에 채웠다(S15P31A101-228). D-G3-06 은 관제 앱이 `viewer_health.config.sync`(UNVERIFIED·STALE 등)를 화면에 보이지 않아 미완이다. 관제 앱이 구성 동기화 상태를 보이면(추창만) 그 시험을 근거로 PASS 로 바꾼다.
- 위 GPU 세션 항목과 D-G3-04 이미지 비교(CI 단계 추가 제안: `Jenkinsfile` 의 "아직 없는 단계").
- crash_recovery_g3 서비스 시나리오의 간헐 시간 초과(1/4) 원인 확인: 시동 로그상 구성 묶음 준비 뒤 R1 판정까지 오지 않았다. 부하·경로 길이와의 관계를 몇 번 더 돌려 본다.
- 15.1 G3 셋째 행(비LIVE 알림 0건)은 228 칸에 서버 DB 제약 시험 결과로 붙였다(PASS).
