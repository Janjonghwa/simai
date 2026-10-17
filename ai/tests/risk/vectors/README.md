# Risk Engine 공통 테스트 벡터

같은 입력에 같은 판정이 나오는지 Tier 0 배포 이미지와 EC2 컨테이너 이미지 양쪽에서 확인하는 입력·기대 출력 세트다(명세 3.2, 15.1 G3, 21.3).
형식과 실행기의 정본은 `ai/src/simai_ai/risk/vectors.py` 머리말이다.

```sh
# ai/ 에서
uv run python -m simai_ai.risk.vectors tests/risk/vectors   # 모두 통과하면 종료 코드 0
uv run pytest tests/risk                                   # 위 실행을 포함한 시험
```

## 형식 요약 (format_version 1.x)

| 필드 | 내용 |
|---|---|
| `vector_id`, `description` | 식별자와 확인하려는 것 |
| `origin` | `LIVE` / `PLANNED` / `REPLAY` / `WHAT_IF`. LIVE 가 아니면 알림 후보 0건을 함께 검사한다 |
| `rules` | `[{rule_id, rule_version}]`. 등록부(`default_registry`)에서 만든다 |
| `zones` | 구역 목록. 판정 시각(`clock_ms`)에 유효한 버전만 쓴다 |
| `steps[]` | `clock_ms`(고정 시계, 줄지 않음), `world_state`(I1 payload, `contracts/messages/world_state.schema.json`), 선택 `monitoring`(1.2, 아래), `expect.findings` |

- 기대 판정은 `rule_id, rule_version, severity, world_ids, zone_id, needs_check, reasons, metrics`를 모두 적는다. 순서는 상관없다. `holding`(히스테리시스 띠, 기본 false)은 선택이다(1.1).
- 사람 대상 판정은 `world_ids`에 World ID를 적는다. 사람 없는 구역·시스템 판정(R7 형태)은 `world_ids: []`, `zone_id`를 채운다. 가짜 World ID를 만들지 않는다.
- `metrics`는 절대 오차 1e-9 안에서 같으면 통과, 나머지는 정확히 같아야 한다.
- 시각은 UTC epoch ms 정수, 좌표는 World(m, +Z 상방). 모르는 값은 null 로 두고 Z=0 으로 채우지 않는다.
- 정답·시뮬레이션 입력으로 만든 벡터의 결과를 제품 성능으로 쓰지 않는다.

## 감시 상태 (format_version 1.2, 선택)

`steps[].monitoring`은 같은 판정 시각의 카메라 상태와 구역별 관측 카메라다(R7 입력, `simai_ai.risk.monitoring.MonitoringState`). world_state 에 카메라 상태가 없어서 따로 둔다.

| 필드 | 내용 |
|---|---|
| `cameras[]` | `{id, status, last_capture}`. health 메시지의 `camera_health`와 같은 이름(`contracts/messages/health.schema.json`). status 는 `OK` / `NO_SIGNAL` / `WAITING_KEYFRAME` / `SYNC_INVALID`. 다른 필드(fps 등)는 무시한다 |
| `zone_cameras` | `{zone_id: [camera_id, …]}`. 그 구역을 시야에 담는 카메라 |

- `monitoring` 키는 `format_version` 1.2 이상에서만 쓴다. 1.0·1.1 벡터에 있으면 형식 오류다.
- `monitoring`이 없으면 규칙에 None 으로 넘긴다. 출처가 LIVE·REPLAY 면 R7 은 이것을 '모름'으로 보고 모든 구역을 저하로 판정한다. PLANNED·WHAT_IF 는 카메라 개념이 없어 R7 을 판정하지 않는다(가상 카메라 장애를 보려면 `monitoring`을 넣는다).
- 목록에 없는 구역·카메라도 정상으로 채우지 않는다.

## 생명주기 시나리오 (format_version 1.1, 선택)

최상위에 `lifecycle`(`gateway_id`, `config_version`, 선택 `policy`)이 있으면 단계마다 판정을 `LifecycleTracker`에 넣고 RiskEvent 전이와 현장 경보 신호를 비교한다(명세 3.2, 6.4, 7.3, 7.4).

| 필드 | 내용 |
|---|---|
| `steps[].inject_findings` | 규칙 출력 대신 넣는 판정. 구체 규칙(R1~R7) 등록 전에 생명주기만 시험한다. `expect.findings`에는 넣지 않는다 |
| `steps[].acks` | `[{event, actor, actor_id, acked_at}]`. 판정 반영 뒤 같은 시계로 적용한다. actor 는 `ADMIN` / `FOREMAN` / `SITE_BUTTON` |
| `expect.transitions` | `[{event, event_sequence, transition, changes, rule_id, severity, needs_check, verification, world_ids, zone_id, acknowledged}]` (필수, 순서 그대로) |
| `expect.local_signals` | `[{kind, event, severity, needs_check}]`. kind 는 `LOCAL_ALERT` / `ALARM_STOP` / `ALERT_CLEAR` (필수, 순서 그대로) |

- `event`는 이벤트 별칭이다. 실행기가 처음 나온 순서대로 `e1`, `e2`, … 를 붙인다. event_id(UUIDv7)는 적지 않는다.
- 실행기는 불변 규칙도 함께 본다: LIVE 가 아니면 현장 경보 신호 0건, event_id 는 UUIDv7, gateway_id 일치, event_sequence 는 이벤트별로 1부터 1씩 증가.
- 해소 유지 시간은 관측 시각(`world_state.t_latest_global`)으로 잰다.

## 발생 지속 조건·재개·확인 필요 이유 (format_version 1.3)

- `format_version` 1.3 미만 생명주기 벡터는 지속 조건·재개 없는 설정(`LEGACY_IMMEDIATE_POLICY`, 발생 판정 한 번에 새 이벤트)으로 돌린다. 1.3 이상은 제품 기본값(3프레임 중 2프레임, R7 즉시, 재개 10초)에서 `policy` 에 적은 값만 바꾼다(명세 6.4).
- `lifecycle.policy` 에 더하는 키(1.3 이상만): `persistence` `{min_frames, window_frames}`, `persistence_by_rule` `{rule_id: {min_frames, window_frames}}`(적으면 기본값 R7 즉시를 대신한다), `resume_window_ms`(정수 또는 null = 재개 안 함).
- 프레임은 관측 시각(`t_latest_global`)이 앞으로 간 단계다. 같은 관측을 다시 판정한 단계는 지속 조건에서 세지 않는다. 다만 대기 중인 키의 안전 검사(관측 상실·lag 초과 → 발생)는 관측 시각이 멈춘 단계에서도 한다. 재개 시간은 지속 조건을 채운 단계의 관측 시각으로 잰다.
- 판정(`inject_findings`, `expect.findings`)에 `confidence_reasons`(선택, 기본 `[]`)를 적을 수 있고, `expect.transitions[]` 에는 `confidence_reasons` 가 필수다. 이유가 있으면 `needs_check` 는 true, 이유 없는 `needs_check: true` 는 원인 미상 `UNSPECIFIED` 가 붙는다(명세 7.1, 원인을 지어내지 않음).
- 재개 전이는 `transition: UPDATED`, `changes: ["RESUMED"]` 로 보이고 같은 이벤트 별칭의 `event_sequence` 가 이어진다.

## 현재 벡터

| 파일 | 확인하는 것 |
|---|---|
| `empty_scene.json` | 빈 장면, 규칙 없음 → 판정 없음 |
| `no_rules.json` | 구역 안 사람(관측·예측, 접지 미확인 포함), 규칙 없음 → 판정 없음 |
| `replay_zone_versions.json` | REPLAY, 두 단계, 구역 버전 전환 → 판정 없음 |
| `lifecycle_live_ack_resolve.json` | LIVE: HIGH 발생 → 현장 경보, 현장 확인 → 소리 정지(해소 아님), 판정 소멸 후 유효 관측 3초 → 해소·표시 종료 |
| `lifecycle_observation_lost.json` | LIVE: 관측 상실(UNKNOWN, 트랙 없음)은 1분이 지나도 해소하지 않고 UNVERIFIABLE, 재관측 후 3초 → 해소 (G3) |
| `lifecycle_stale_observed.json` | LIVE: OBSERVED 라도 대상의 새 관측이 없으면(gap 초과) 다른 영상이 진행해도 UNVERIFIABLE, 영상 전체 중단(lag 초과) 중에도 해소 안 함, 재관측 후 3초 → 해소 (6.1, G3) |
| `lifecycle_identity_revision.json` | LIVE: 해소 직전 ID 교정 → 해소하지 않고 다시 3초를 센다 (G3) |
| `lifecycle_merge.json` | LIVE: 같은 사람·같은 규칙 병합(높은 등급), 규칙별 별도 이벤트, holding 만으로는 발생 안 함, 낮은 신뢰도는 등급 유지 |
| `lifecycle_planned_no_alert.json`, `lifecycle_what_if_no_alert.json`, `lifecycle_replay_no_alert.json` | 비 LIVE: 같은 시나리오의 전이는 만들지만 현장 경보 신호 0건 (7.4, G3) |
| `r1_boundary_entry_exit.json` | LIVE R1 GROUND_FOOTPRINT(1.3 기본 정책): 경계 밖 유지 띠는 이벤트 없음, 불확실성 범위(k=2 타원 + 0.3m)가 걸린 둘째 프레임에 발생·현장 경보(3프레임 중 2), 0.3m 띠 안은 유지, 띠 밖 1.5초 뒤 띠로 돌아오는 깜빡임은 새 이벤트 없이 해소 확인 재시작, 띠 밖 3초 유지로 해소 (7.1, 7.2, 8.2) |
| `r1_boundary_entry_exit_what_if.json`, `r1_boundary_entry_exit_replay.json` | 비 LIVE R1: 같은 시나리오의 판정·전이는 같고 현장 경보 신호 0건 (7.4) |
| `r1_uncertain_person.json` | LIVE R1: 같은 위치에서 불확실성 큰 사람만 발생, 예측 위치·카메라 0대는 확인 필요(PREDICTED, SINGLE_VIEW)로 발생·경보, 신원 애매는 IDENTITY_UNCERTAIN |
| `r1_volume_z_unknown.json` | LIVE R1 VOLUME_3D: XY 안인데 서 있는 면이나 위 끝 중 하나라도 모르면 아는 끝과 관계없이 발생 + 확인 필요(Z_UNKNOWN), 높이를 알게 되면 겹침(확인 필요 해제)·밖(3초 뒤 해소) (8.2, D-G6-04) |
| `r1_volume_podium.json` | LIVE R1 VOLUME_3D: 같은 XY 의 바닥 위 사람은 판정 없음, 0.4m 단상 위는 발생. 추정(inferred) 관절 미사용, 높이 간격 0.3m 안은 유지 (8.2, D-G6-04) |
| `r1_persistence_resume.json` | LIVE R1 + 기본 생명주기 정책: 구역 안 순간 검출 한 프레임은 이벤트 0건, `[T,F,T]` 에서 발생, 해소 뒤 10초 안 재진입은 같은 이벤트 재개(RESUMED, 다시 울림), 10초 넘어 재진입은 새 이벤트 (6.4, D-G6-07·08) |
| `r1_footprint_position_or_anchor.json` | LIVE R1 GROUND_FOOTPRINT 보수적 결합: position_xy 만 안, 접지점만 안 모두 발생, 둘 다 띠 밖이면 판정 없음 (8.2) |
| `r6_observation_lost_in_zone.json` | LIVE R6: 구역 안 위험 중 PREDICTED → UNKNOWN → 트랙 없음이 1분 넘게 이어져도 자동 해소 0건(UNVERIFIABLE). PREDICTED 동안 R1 은 확인 필요로 판정하고, UNKNOWN 동안 R6 holding 은 원래 R1 이벤트를 확인 필요로 유지만 하며 새 이벤트를 만들지 않음. 재관측 뒤 다시 상실하면 해소 확인을 처음부터 (G3). R1 은 실제 등록부 규칙(`ObservationLossHold(ZoneEntryRule())`)이 판정한다 |
| `r6_observation_lost_in_zone_what_if.json` | WHAT_IF R6: 같은 시나리오, 현장 경보 신호 0건 (7.4, G3) |
| `r7_camera_disconnect.json` | LIVE R7: 카메라 분리 → 구역 감시 저하 이벤트(CAUTION, 관제 화면 안내만, 분리 후 1초 안). 현장 경보 신호 0건(7.2, 7.3), 다른 구역은 별도 이벤트, 복구 후 3초 유지로 해소 (G3) |
| `r7_sync_invalid_replay.json` | REPLAY R7: 시간 동기화 무효 → 저하, 회복 → 해소, 현장 경보 신호 0건 (7.4, G3) |
| `r7_live_without_monitoring.json` | LIVE R7: 감시 상태 없음 → '모름'으로 모든 구역 저하(monitoring_unknown, CAUTION) |
| `r7_planned_without_monitoring.json` | PLANNED R7: 카메라 개념이 없어 감시 상태가 없으면 판정 0건 |
| `lifecycle_persistence_boundary.json` | LIVE 1.3 발생 지속 조건(D-G6-07): 순간 검출 한 프레임 → 이벤트 0건, 같은 관측 재판정은 프레임으로 세지 않음, `[T,F,T]` 에서 발생, R7 은 즉시 |
| `lifecycle_resume_window.json` | LIVE 1.3 재개(D-G6-08): 확인·해소 뒤 정확히 10초에 재발생 → 같은 이벤트 재개(UPDATED RESUMED, 미확인으로 되돌리고 다시 울림), 해소 뒤 10.1초 → 새 이벤트 |
| `lifecycle_flicker_no_new_event.json` | LIVE 1.3 경계 깜빡임(G6 10초 안 재발생 새 이벤트 0건): 활성 중 들락날락은 이벤트 하나, 해소 뒤 10초 안 재발생은 모두 재개, RAISED 한 번 |
| `lifecycle_confidence_reasons.json` | LIVE 1.3 확인 필요 이유(7.1): 이유 목록 변경 → CONFIDENCE 갱신(선언 순서 정렬), 이유가 사라지면 확인 필요 해제(등급 유지), 이유 없는 needs_check → UNSPECIFIED |
| `lifecycle_pending_observation_lost.json`, `lifecycle_pending_observation_lost_what_if.json` | 1.3 지속 대기 중 관측 상실: 발생 판정 한 프레임 뒤 관측 상실 → 바로 발생(UNVERIFIABLE, PREDICTED), 1분 동안 자동 해소 0건, 관측되며 판정이 사라진 사람은 이벤트 0건. WHAT_IF 는 현장 경보 신호 0건 |
| `lifecycle_pending_stall.json`, `lifecycle_pending_stall_replay.json` | 1.3 지속 대기 중 영상 전체 정지: 같은 관측 재판정은 프레임으로 세지 않지만 lag 600ms 를 넘는 첫 판정 주기에 발생(UNVERIFIABLE, PREDICTED), 정지 1분 자동 해소 0건. REPLAY 는 현장 경보 신호 0건 |
| `lifecycle_resume_pending_lost.json` | LIVE 1.3 재개 대기 중 트랙 소실 → 같은 이벤트 재개(UNVERIFIABLE, PREDICTED, 다시 울림) |

R6 은 구역 규칙을 감싸는 방식(`ObservationLossHold`)이라 등록부의 R1 v1 이 `ObservationLossHold(ZoneEntryRule())` 이고(S15P31A101-71), R6 벡터도 실제 R1 판정을 쓴다. R1·R6 벡터는 1.3 형식(기본 생명주기 정책, 판정·전이에 `confidence_reasons`)이다. 다른 구체 규칙도 등록되면 해당 이슈에서 규칙별 벡터를 이 디렉터리에 추가한다.
