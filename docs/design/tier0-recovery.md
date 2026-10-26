# Tier 0 재시작 복구 설계와 운영 절차

S15P31A101-51. 기획서 §3.2(재시작 복구·복구 순서·저널 계약), §3.10(복원 전 빈 현장 경보 목록 금지), §6.2, 부록 D-G3-09·11·13, §15.1 G3 기준이다.
코드는 `ai/src/simai_ai/recovery/`(checkpoint, projection, restore, checkpointer, health_gate)이고 시험은 `ai/tests/recovery/` 다.
배선(진입점)은 S15P31A101-171, 복원 Track 과 재관측 매칭·30초 규칙은 S15P31A101-54, Network Volume 강제 종료 실측은 S15P31A101-92 다.

## 구조

| 단계 | 내용 |
|---|---|
| 저널 | 전이 기록마다 `meta.state` 에 전이 뒤 `EventState` 전체를 남긴다(I1 로는 보내지 않음). 저널이 정본이다 |
| 투영 | `EventProjection` 이 fsync 끝난 기록을 offset 순서로 적용한다. 이벤트별 sequence 로 멱등이고, 해소 기록(tombstone)으로 해소된 이벤트의 과거 전이를 거른다 |
| 체크포인트 | `Checkpointer` 가 1초마다 투영으로 쓴다. 활성 이벤트, 해소 기록(최근 1024개 + 밀려난 것의 판정 시각 watermark), last_applied_journal_offset·msg_id, config_version, 마지막 촬영 시각, Track(꺼내기만) |
| 복구 | `recover`: 체크포인트 선택 → 그 offset 이후 저널 재적용 → 활성 이벤트를 UNVERIFIABLE 로 복원(저널에 기록) |
| Health | `RecoveryHealthGate`: 재시작 뒤 촬영 시각이 앞으로 간 새 프레임과 시계 차이 추정이 함께 확인될 때까지 UNSAFE_UNKNOWN |
| 복원 이벤트 해소 근거 | `LifecycleTracker.restore` 가 모든 복원 이벤트에 복원 보호를 건다(해소될 때까지, 체크포인트·저널에 싣지 않고 재시작마다 다시 건다). 사람 대상은 기존대로 대상 World ID 재관측으로, 구역 대상(R7)은 판정의 `Evaluation.zone_clear_evidence`(R7 이 그 구역의 정상 관측 카메라 확보를 확인)가 있는 판정에서만 VERIFIED 다. 판정이 없거나 규칙·구역이 구성에서 빠진 것은 근거가 아니다(-174). 복원하지 않은 구역 대상 이벤트도 판정이 사라진 판정에서는 같은 근거가 있어야 해소를 센다(-180, 아래 "구역 대상 이벤트가 남을 때") |
| 복원 Track | `WorldTrackManager.restore(result.tracks, result.tracks_t)`: 재관측과 맞출 후보로만(새 ID, 30초 규칙). `RestoredEventLinker(result.restored)`: 새 이벤트의 related_event_id (-54, tier0-world-track.md "재시작 뒤 복원 Track 매칭") |

체크포인트 형식(내부, `format_version` 2): `checkpoint-{세대}.json` = {format, format_version, sha256, body}. 형식 1(해소 기록 없음)은 읽지 않고 직전 세대·저널 전체로 물러선다.
저널 정리는 남은 온전한 체크포인트 세대의 가장 작은 offset 과 acked_offset 중 작은 값 이하만 한다(명세 경계보다 보수적, 직전 세대로 물러설 구간 보존).

### 해소 기록의 보장 경계

- 최근 1024개 해소 이벤트: sequence 로 정확히 거른다.
- 그보다 오래돼 밀려난 이벤트: 밀려난 해소 기록의 마지막 판정 시각 최댓값(watermark) 이하인 판정 시각을 가진, 활성도 tombstone 도 아닌 이벤트의 기록을 오래된 기록으로 보고 건너뛴다. Tier 0 판정 시계가 watermark 보다 더 뒤로 가지 않는다는 전제에 기댄다(1024개 해소가 쌓이는 시간보다 큰 시계 역행이면 새 이벤트를 잘못 거를 수 있다).

## 시작 거부와 조치 (운영 절차)

`recover` 는 활성 이벤트를 추측하지 않는다. 아래 경우 Tier 0 는 시작하지 않고 노트북에는 "관제 중단"이 표시된다. **어느 경우든 저널을 지워 정상 복구로 삼지 않는다.** 저널을 지우면 활성 이벤트와 EC2 로 보내지 못한 전이(outbox)를 잃는다.

공통 첫 조치: 같은 Network Volume 의 `journal/`(세그먼트, `acked_offset.json`, `evidence_index.json`), `checkpoints/` 를 통째로 보관 위치(날짜·gateway_id 를 붙인 디렉터리)에 복사해 원본을 보존하고, 오류 메시지·로그와 함께 Lead 에 알린다.

| 오류 | 원인 | 조치 |
|---|---|---|
| `LegacyJournalError` "meta.state 없는 이전 형식 저널" | S15P31A101-51 이전 코드가 쓴 저널. 상태를 되살릴 수 없다 | 배포 전 시험 데이터면 `journal/`·`checkpoints/` 디렉터리를 보관 위치로 옮기고 새로 시작한다. 운영 데이터면 옮기지 말고 보존한 채 Lead 에 알린다. 운영 배포 전이라 업그레이드 bridge 는 두지 않는다(Lead 결정) |
| `RecoveryError` "저널이 체크포인트 … 와 맞지 않는다" | 저널이 fsync 된 기록을 잃었거나(체크포인트가 앞섬) 다른 저널(같은 offset 의 msg_id 가 다름). 마운트 경로·볼륨 착오가 흔한 원인 | 볼륨·경로 설정을 먼저 확인한다. 같은 Tier 0 의 저널이 맞는데 기록이 없어졌으면 저장소 장애로 보고 보존 후 Lead 판단 |
| `RecoveryIncomplete` | 쓸 체크포인트가 없고(모두 손상·형식 불일치) 저널 앞부분이 정리돼 활성 이벤트를 다 알 수 없다 | 보존 후 Lead 판단. EC2 의 이벤트 기록(최종 상태)과 대조해 수동으로 판단한다 |
| `RecoveryError` "저널을 다시 적용하지 못했다" | 저널 기록의 meta.state 손상, payload 와 불일치, 다른 Tier 0·LIVE 아닌 기록 | 보존 후 Lead 판단(저널 손상) |

시작하지만 알려야 하는 것:

- `RecoveryResult.rejected` 가 비어 있지 않음: 버린 체크포인트(손상·부분 쓰기·형식 버전·다른 gateway·정리된 구간)와 이유. 로그·health 에 남기고, 파일은 다음 체크포인트 저장 때 정리된다(필요하면 그 전에 보관 위치로 복사).
- `RecoveryResult.source == JOURNAL`: 체크포인트 없이 저널 전체로 복구했다. config_version 을 체크포인트에서 얻지 못하므로 BundleArchive 복구 결과만으로 판정한다.
- `RecoveryResult.stats` 의 gaps·unknown_updates·duplicates·stale: 저널 이상. 로그로 알린다.

## 구역 대상 이벤트가 남을 때 (운영 절차, S15P31A101-180)

구역 대상(R7) 이벤트는 R7 이 그 구역의 정상 관측 카메라 확보를 확인한 판정(`Evaluation.zone_clear_evidence`)이 해소 유지 시간(3 s) 동안 이어져야 해소된다(명세 6.4). 구성 묶음 전환으로 R7 규칙이나 그 구역이 빠지면 근거가 나오지 않으므로 그 구역의 활성 R7 이벤트는 해소되지 않고 UNVERIFIABLE 로 남는다. 재시작해도 복원 보호로 같은 상태다. 근거를 만들 수 없으면 관리자가 닫는다(아래 "운영자 종료", -200).

| 경우 | 조치 |
|---|---|
| 구역 삭제·R7 제거 전 | 관제 화면에서 그 구역의 활성 R7 이벤트가 없는지 먼저 확인한다. 있으면 카메라를 복구해 해소된 뒤(정상 관측 카메라 2대 이상, 3 s) 새 구성 묶음을 배포한다 |
| 이미 뺀 뒤 이벤트가 남음 | 구역(같은 zone_id)과 R7 이 들어 있는 구성 묶음을 새 버전으로 다시 활성화한다(이전 버전을 덮어쓰지 않는다). 카메라가 정상이면 첫 판정에서 VERIFIED, 3 s 뒤 해소된다. 해소를 확인한 다음 원래 의도한 구성 묶음을 다시 배포한다 |
| 그 구역을 볼 카메라를 복구할 수 없음 | 근거를 만들 수 없으므로 이벤트는 남는다. 저널·체크포인트를 고쳐 지우지 않는다. 확인(ACK)으로 담당자를 기록하고, 관리자 종료(`event_close`)로 닫는다(아래) |

## 운영자 종료 (S15P31A101-200)

명세 6.4 "운영자 종료". 코드: `LifecycleTracker.close`·`CloseRequest`·`CloseReject`(risk/lifecycle.py), `EventJournal.has_close_id`·`is_judgment_transition`(eventlog/journal.py),
`JudgmentLoop.close`(services/tier0/judge.py), `wire_tier0_handlers(on_event_close=…)`(i1_dispatch.py), `build_i1_link(on_event_close=…)`(i1_publisher.py, 결과를 신뢰 대기열로),
`Tier0Service._build_link`(`on_event_close=self.judge.close`), outbox `operator_resolution`(eventlog/outbox.py), G3 `restored_resolution_violations`(deploy/scripts/crash_recovery_g3.py).
계약(v5.0.0): I1 `event_close`·`event_close_result` 1.0.0, I1 `risk_event_transition` 2.0.0·I2 `risk_event` 2.0.0 `resolution`(공통 `operator_resolution`).
시험: tests/risk/test_operator_close.py, tests/recovery/test_operator_close_restart.py, tests/eventlog/test_outbox.py, tests/services/tier0/test_tier0_judge_r7_restore.py·test_i1_units.py·test_tier0_operator_close_e2e.py, tests/deploy/test_crash_recovery_g3.py.

| 항목 | 정한 것 | 근거·버린 대안 |
|---|---|---|
| 대상 | 구역 대상(world_ids 없음)만. 사람 대상은 TARGET_NOT_ZONE | 사람 대상은 대상 World ID 재관측으로만 해소한다(3.2). 사람을 놓친 채 닫으면 위험을 잃는다 |
| 등급 | CAUTION·INFO 만. HIGH·CRITICAL 은 SEVERITY_NOT_ALLOWED | 명세에 등급 기준이 없어 안전 쪽(거부). 지금 구역 대상 규칙은 R7(CAUTION)뿐이다. 별도 확인(이중 승인)은 버림: 쓰는 규칙이 없다 |
| 지금 조건 | 마지막 판정에 그 이벤트의 판정이 있으면 CONDITION_PRESENT | 판정이 계속 나는 이벤트를 닫으면 다음 판정에 바로 새 이벤트가 난다(경보 반복). 복원 직후 판정 전은 판정 없음으로 본다(종료 가능) |
| 누가 | 관제 화면 관리자(CONTROL_ROOM·ADMIN)만, 사유(1~500자) 필수 | 반장 PWA '조치 완료'는 해소가 아니다(8.4). 근거 없는 해소가 되지 않게 누가·왜를 필수로 |
| 기록 | 저널: RESOLVED 전이, 변경 사유 OPERATOR_CLOSED, payload `close`(close_id·채널·역할·계정·요청·기록 시각·사유). 검증 상태 그대로, lifecycle 은 evidence_ref 를 싣지 않는다(저널이 직전 판정 근거를 알면 meta 로 붙이고 모르면 null). I1: outbox 가 `close` 를 `resolution`(kind OPERATOR_CLOSED, recorded_at 제외)으로 바꾸고 `close` 는 뺀다. 값이 비면 지어내지 않고 전송 보류 | VERIFIED 로 바꾸면 관측 해소로 오인된다(규칙 7). 저널은 종료를 판정이 아닌 전이로 보고 근거를 지어내지 않는다 |
| 재발생 | 재개하지 않고 새 이벤트 | 재개(6.4)는 관측 해소 뒤 깜빡임을 막는 규칙이다. 종료한 이벤트를 다시 열면 종료 기록과 어긋난다(EC2·G3 가 닫힌 이벤트의 전이를 다시 받음) |
| 멱등 | close_id 를 저널이 색인(기록 대기 중 포함, 재시작 뒤 남은 세그먼트로 다시 만듦) → DUPLICATE | ack_id 와 같은 경계: 정리된 세그먼트의 close_id 는 재시작 뒤 모른다(그때는 이미 닫혀 NOT_ACTIVE). 거부는 기록하지 않아 같은 close_id 를 다시 받으면 다시 판단한다 |
| 응답 | `event_close_result` ACCEPTED(event_sequence)·DUPLICATE·REJECTED(사유). 종료 전이를 저널에 제출하지 못하면(쓰기 대기열 포화·쓰기 정지) 추적기는 상태를 바꾸기 전이라 이벤트가 그대로 활성이고 REJECTED `JOURNAL_UNAVAILABLE`(1.1.0, 같은 close_id 로 재시도 가능)로 답한다(리뷰 F1: 제출 못 한 종료를 수락하면 활성만 사라지고 근거가 영구 누락) | EC2 는 응답이 아니라 저널 전이로 상태를 바꾼다(생명주기 정본은 Tier 0). ACCEPTED 는 저널 fsync 전에 답한다(확인과 같음): fsync 전 강제 종료면 재시작 복원으로 다시 활성(안전 쪽) |
| G3 | EC2 로 나간 전이의 `resolution`(kind OPERATOR_CLOSED·close_id·actor_id·reason)이 있는 구역 대상 해소는 근거 있는 해소. 값이 비었거나 사람 대상이면 D-G3-09 위반. `resolution` 없는 해소는 lifecycle `changes` 와 관계없이 관측 해소로 검사 | -198 의 '근거 있는 해소만 정상' 유지 |
| 계약 버전 | `risk_event_transition`·`risk_event` 2.0.0(메이저): `resolution` 이 있으면 RESOLVED 의 VERIFIED(전이는 evidence_ref 도) 필수를 푼다. 1.x 문서는 `resolution` 없이 1.x 규칙 | 조건 완화라 1.x 수신 측이 RESOLVED 를 관측 해소로 읽는다(Lead 승인 2026-10-20, -197 리뷰 기준). world_state 2.0.0 처럼 schema_version 으로 분기 |

**Lead 결정(2026-10-20).** 위 계약 버전(메이저)과 새 I1 메시지·안전 쪽 값(HIGH·CRITICAL 거부, 관제 화면 ADMIN 만, 사유 1~500자, 판정 중 거부)을 승인했다.
서버(EC2): `event_close` 송신·`event_close_result` 표시, 전이의 `resolution` 을 `risk_event` 에 옮겨 관측 해소와 구분해 보여 주기는 server 티켓이다.

## 운영자 상향 (S15P31A101-272)

명세 5.4 "운영자 확인 상향", 12.3. 운영자 종료와 같은 흐름이다: `EscalationRequest.from_contract` → `JudgmentLoop.escalate`(escalation_id 중복 → 추적기 판단 →
저널 제출 → 현장 경보·클립) → `event_escalate_result`. 코드: `LifecycleTracker.escalate`·`EscalationRequest`·`EscalateReject`(risk/lifecycle.py),
`EventJournal.has_escalation_id`·`is_judgment_transition`(eventlog/journal.py), `JudgmentLoop.escalate`(services/tier0/judge.py), `wire_tier0_handlers(on_event_escalate=…)`,
`build_i1_link(on_event_escalate=…)`, `Tier0Service._build_link`. 계약(-206): I1 `event_escalate`·`event_escalate_result` 1.0.0, `risk_event_transition` 2.1.0 `escalation`.
시험: tests/risk/test_operator_escalate.py, tests/recovery/test_operator_escalate_restart.py, tests/eventlog/test_outbox.py, tests/services/tier0/test_tier0_operator_escalate.py·
test_tier0_operator_escalate_e2e.py·test_i1_units.py·test_event_clips.py.

| 항목 | 정한 것 | 근거·버린 대안 |
|---|---|---|
| 판단 순서 | DUPLICATE(같은 escalation_id 기록·대기 중) → NOT_ACTIVE → NOT_UPWARD(target ≤ 지금 등급) → JOURNAL_UNAVAILABLE(제출 실패) → ACCEPTED | 계약 설명 그대로. 거부는 기록하지 않아 같은 escalation_id 를 다시 받으면 그때 상태로 다시 판단한다 |
| 기록 | 같은 event_id 의 UPDATED, 변경 사유 SEVERITY·ESCALATED, payload `escalation`(받은 값 그대로), resumed false. 판정 값(검증 상태·수치·근거)은 그대로, lifecycle 은 evidence_ref 를 싣지 않는다(저널이 직전 판정 근거를 meta 로, 모르면 null) | 판정이 아닌 전이(12.3 evidence_ref 문장). outbox 는 `escalation` 을 그대로 보내고 전이 schema_version 을 2.1.0 으로 올렸다(EC2 는 메이저만 본다) |
| 재경보·클립 | 등급이 오르면 규칙 상향과 같이 `_local_alert(force)` 로 다시 울리고 경보 단계(site_alarm_sequence)를 연다. 전이는 SEVERITY 를 달아 클립 경로(같은 열쇠의 상향, CRITICAL 보존 표시)를 그대로 탄다 | 3.10 상향 재경보. 별도 경로를 두지 않았다 |
| 이후 규칙 판정 | 올린 등급은 해소될 때까지 바닥값(`EventState.escalation_floor`): 규칙 판정이 그보다 낮으면 바닥값을 쓰고, 높으면 따른다. 재개(RESUMED)하면 비운다 | 명세에 규칙 판정과의 관계가 없다(5.4 하향 불가만). 바닥값이 없으면 다음 판정이 바로 되돌려 승인이 무의미하다. 버린 안: 규칙이 그대로 덮어씀(음성 대조 시험) |
| 복구 | 바닥값은 상태 기록(to_record)에 있을 때만 싣는다(이전 체크포인트·저널 형식 그대로). escalation_id 는 저널 재생으로 색인 | 체크포인트가 상향 전이든 후이든 같은 상태로 돌아온다(시험) |
| 멱등 경계 | close_id 와 같다: 정리된 세그먼트의 escalation_id 는 재시작 뒤 모른다(그때 같은 등급이면 NOT_UPWARD 로 답해 기록하지 않는다) | |

## 실행 중 경고

| 신호 | 의미 | 조치 |
|---|---|---|
| `Checkpointer.stalled()` (기본 5초 동안 성공 없음), `last_error` | 체크포인트를 못 쓴다(볼륨 용량·권한·fsync 지연). 이전 세대는 그대로라 판정은 계속되지만 재시작 때 재적용 구간이 길어지고 저널 정리가 멈춘다 | Network Volume 용량·쓰기 지연을 확인한다. 용량이 차면 저널 정리도 멈추므로(정리 하한이 오르지 않음) 볼륨을 늘린다. 저널·체크포인트를 지워 공간을 만들지 않는다 |
| `JournalWriter.stalled()`·`failed` | 저널 쓰기 정지·실패(eventlog) | eventlog 운영 절차를 따른다 |
| 저널 크기 | 전이 기록에 상태 전체를 함께 남겨 이전 형식보다 커졌다(RAISED 표본 약 1.7배, 확인 이력이 길면 더 큼). 세그먼트 64 MiB, 보관 24시간 | 볼륨 용량 감시에 저널 디렉터리를 포함한다 |

## 남은 일

- 배선(-171, 완료): 시작 때 `recover` → `Checkpointer(…, compact=SnapshotOutbox.compact).start()`, 판정 루프의 `update_inputs`, health 발행 경로의 `RecoveryHealthGate.apply`(카메라 시각 상태는 새로 만든다), 복원 전 active_site_alerts 송신 금지, 위 경고를 로그·표시 파일(`state/tier0.live`)로 알림(health 계약 필드는 없음), checkpoint config_version 과 BundleArchive 복구 버전 비교. 구조는 docs/design/tier0-service.md.
- 명세: §3.2 는 "MVP 는 World Track 을 체크포인트하지 않는다"이다. -54 는 Jira 대로 복원 Track 을 재관측 매칭 후보로 쓰되(같은 ID 로 잇지 않음, 위치 정본 아님) 판정에는 넣지 않는다. 저장 범위는 명세 정리 때 Lead 가 정한다(-172).
- 배선(-54 → -178, 완료): `services/tier0/restore_link.RestoreLink` 가 인지를 만든 직후(첫 update 전, 판정 스레드 시작 전) `manager.restore(result.tracks, result.tracks_t)` 를 부르고 `RestoredEventLinker(result.restored)` 를 만든다. 판정 루프는 판정 lock 안에서 같은 tick 의 ID 교정 뒤에 `related=linker.for_frame(frame, manager.restore_status)` 를 `tracker.apply` 에 넘긴다(입력 정지 tick 은 마지막 실제 World 의 frame). `restore_status` 가 정해지는 tick 에 로그를 남기고 DISCARDED 면 이유를 WARNING 으로 남긴다(표시 파일 `status.restore_link` 에도). 체크포인트 Track 을 restore 가 거부하면 쓰지 않고 빈 후보로 시작한다. `for_frame` 예외는 연결 없음으로 낮춘다. 첫 인지 tick 전 체크포인트 재료는 복구 결과(config_version·Track·Track 시각)로 채운다(재시작 두 번 연속). 순서·시험은 tier0-service.md "순서 불변식".
- 위 시작 거부 절차는 §18 에 옮길 후보다.
