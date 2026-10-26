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
| D-G3-05 재동기화 중 해소 이벤트 재알림·DELAYED_ACTIVE 누락 | 0건 | 113(Tier 0 스냅샷 내용) · 228(EC2 재알림) | FI | Tier 0: `tests/services/tier0/test_event_snapshot.py`, `test_i1_resync_wss.py::test_ec2_outage_keeps_judging_and_site_alerts_then_resyncs_without_gap` | Tier 0 몫 PASS · EC2 몫 곽나혁 제출 대기 |
| D-G3-06 오프라인 중 구성 묶음 유지·UNVERIFIED, 복구 후 원자적 전환 | 100% | 113(Tier 0 ConfigSync) · 228(표시) | IT | `tests/config/test_sync.py`(부록 D-G3-06 시험 포함) | Tier 0 몫 PASS · 표시 곽나혁 제출 대기 |
| D-G3-07 Tier 0 Pod 교체 중 미전송 이벤트 유실 | 0건 | 113 | FI | 같은 상태 디렉터리 재시작 기준: `tests/eventlog/test_crash.py`·`test_outbox.py`(acked_offset 이후 같은 msg_id 재전송). 실제 Pod 교체(다른 인스턴스·디스크 재부착)는 미시험 | GPU 세션 필요(아래 큐 항목안) |
| D-G3-08 I0 재접속·역순·누락 시 프레임-시각 오결합 | 0건 | 113 | FI(SC 로 재현) | `cases/r7_camera_disconnect`·`r1_late_and_reconnecting_camera`(결합 오류 0), 반례 `test_scenario_golden.py::test_counterexample_misbound_frames_break_the_invariant` | PASS |
| D-G3-09 재시작: 자동 해소·재관측 전 UNSAFE_UNKNOWN·related_event_id 누락 | 0건 | 113 | FI | `crash_recovery_g3.py` 서비스 시나리오(첫 health·복원 이벤트 해소 검사), `tests/recovery`(health_gate·restore), `tests/services/tier0/test_tier0_restore_link.py`, `test_tier0_judge_r7_restore.py`, 운영자 종료·상향 재시작 시험 | PASS(노트북) · 실제 저장 위치 실측은 GPU 세션 필요 |
| D-G3-10 ID 교정 후 진행 중 위험 자동 해소 | 0건 | 113 | SC | `tests/tracking/test_world_track_reconnect.py`(D-G3-10 병합), `test_world_track_split.py`(D-G3-10 분할) | PASS |
| D-G3-11 체크포인트 기록 중·전이 직후 강제 종료 복구 불일치 | 0건 | 113 | FI | `crash_recovery_g3.py`(서비스 체크포인트 중 3회, 결정적 지점 5개 + seed 4), `tests/recovery/test_crash_recovery.py`(운영자 상향 직후 지점 추가, -113) | PASS(노트북) · 실제 저장 위치 실측은 GPU 세션 필요 |
| D-G3-12 EC2 만 끊겼을 때 관제 중단 오표시·현장 경보 정지 | 0건 | 113(Tier 0 판정·현장 경보 유지) · 228(노트북 표시) | FI | Tier 0: `test_i1_resync_wss.py::test_ec2_outage_keeps_judging_and_site_alerts_then_resyncs_without_gap`(EC2 단절 중 판정·현장 경보 목록 계속) | Tier 0 몫 PASS · 노트북 몫 곽나혁 제출 대기 |
| D-G3-13 ACK 후 outbox 정리 직후 강제 종료 시 해소 이벤트 부활 | 0건 | 113 | FI | `crash_recovery_g3.py` harness_after_compact, `tests/eventlog/test_crash.py`, `tests/recovery/test_compaction.py` | PASS(노트북) |
| D-G3-14 저널 기록 직후·전송 직전 강제 종료 시 미전달 | 0건 | 113 | FI | `crash_recovery_g3.py`(서비스 시나리오 EC2 전달 확인), `tests/eventlog/test_crash.py`(기록 직후·전송 직전) | PASS(노트북) |
| D-G3-15 EC2 단절 중 현장 확인 버튼 소리 정지, 복구 후 EC2 반영 | 100% | 113(Tier 0 기록·재전송) · 228(노트북 버튼·EC2 반영 화면) | FI | Tier 0: `test_i1_resync_wss.py`(site_ack 저널 기록 → 복구 후 EC2 수신) | Tier 0 몫 PASS · 나머지 곽나혁 제출 대기 |
| D-G3-16 노트북 재접속·신호 유실 후 현장 소리·표시가 활성 경보 목록과 일치 | 100% | 113(Tier 0 목록 송신) · 228(노트북 맞춤) | FI | Tier 0: `tests/services/tier0/test_site_alert_sync.py`(재접속 즉시 목록, 1 Hz, 송신 시점 상태) | Tier 0 몫 PASS · 노트북 몫 곽나혁 제출 대기 |
| D-G3-17 반쯤 열린 TCP·처리 정체 감지(하트비트 3회 누락) | 100% | 228 | FI | — | 곽나혁 제출 대기 |
| D-G3-18 현장 경보는 fsync 를 기다리지 않고 EC2 전송은 fsync 뒤 | 100% | 113 | UT, FI | `tests/eventlog/test_writer.py`(D-G3-18), `test_outbox.py`(fsync 끝난 기록만), `crash_recovery_g3.py`(EC2 로 나간 전이가 저널에 없음 0건) | PASS |
| D-G3-19 EC2 단절 중 CRITICAL·HIGH 클립 로컬 저장, 복구 후 업로드 | 100% | 113 | FI | `tests/services/tier0/test_event_clips.py::test_ec2_outage_spools_to_network_volume_and_uploads_after_recovery`·`test_spooled_clips_are_restored_after_restart`·`test_critical_after_restart_holds_the_spooled_high_clip` 외 | PASS(노트북, 가짜 서명·전송) · 실제 EC2 업로드는 서명 API 연결 뒤 |
| D-G3-20 EC2 단절 구간 리플레이 "데이터 없음" | 100% | 228 | IT | — | 곽나혁 제출 대기 |

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

- 228 항목(05·06·12·15·16 의 노트북·EC2 몫, 17, 20) 결과 합치기.
- 위 GPU 세션 항목과 D-G3-04 이미지 비교(CI 단계 추가 제안: `Jenkinsfile` 의 "아직 없는 단계").
- crash_recovery_g3 서비스 시나리오의 간헐 시간 초과(1/4) 원인 확인: 시동 로그상 구성 묶음 준비 뒤 R1 판정까지 오지 않았다. 부하·경로 길이와의 관계를 몇 번 더 돌려 본다.
- 15.1 G3 셋째 행(비LIVE 알림 0건)은 부록 D.4 밖이라 서버 시험(DB 제약) 결과를 따로 붙인다.
