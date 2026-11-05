# EC2 PPE 상태 수신 (S15P31A101-516)

명세 5.5(PPE 워커)·12.3(I1 `ppe_state`: "EC2 는 버전·멱등·시각·만료·`identity_revision` 을 검사해 추가 전용으로 저장하고 화면용 현재 상태를 따로 둔다. 과거·만료된 메시지는 현재 상태를 덮지 않는다")·11장(`ppe_state` hypertable, 압축·보관은 `world_track_state` 와 같음). 계약은 `contracts/messages/ppe_state` 1.0.0(-491)이고, 보내는 쪽은 [`ppe-service.md`](ppe-service.md)(-507: PPE outbox → Tier 0 I1)다.

이 이슈 전에는 EC2 에 저장기가 없어 `I1MessageRouter` 가 `unknown_type` 으로 세고 버렸다.

- 코드: `server/.../domain/ppe/` — `PpeStateIngestor`(검사·이력·현재 상태), `PpeStateChecks`(스키마로 못 거는 계약 시험)
- 표: V43 `ppe_state`(hypertable, 이력), `ppe_current_state`(일반, 사람별 현재 상태)
- 시험: `PpeStateChecksTest`(DB 없음), `PpeStateIntegrationTest`(실제 I1 연결, 시험 DB 필요)

## 흐름

1. 계약 스키마 전체로 검사한다(`ContractSchemas`, `messages/ppe_state`). WORN·NOT_WORN 인데 같은 상태 근거가 없으면 여기서 걸린다.
2. 스키마가 못 거는 계약 시험을 본다(`PpeStateChecks`). 한 메시지에 같은 `world_id` 가 두 번 있으면 안 된다. 항목마다 `expires_at_ms ≥ last_evidence_at_ms` 여야 하고, `last_evidence_at_ms`·근거 촬영 시각은 `capture_time_ms` 이하여야 한다. DB 가 받지 못하는 시각·정수도 여기서 거른다. 어기면 `invalid` 로 세고 아무 행도 남기지 않는다.
3. `people` 이 비면(판정할 사람 없음, 프로세스는 살아 있음) 쓸 행이 없으므로 `stored` 로 세고 끝낸다.
4. 멱등 키 `(source_id, stream_epoch, state_sequence)` 로 advisory lock 을 잡고 이미 받은 메시지인지 본다. 받았으면 `duplicate` 다.
5. 사람마다 `ppe_state` 에 한 행을 추가한다(근거·입력 출처는 jsonb 그대로).
6. 받는 순간 이미 만료된 상태가 아니면 `ppe_current_state` 를 더 새 상태일 때만 덮는다(아래).

## 정한 것과 근거

| 항목 | 값 | 근거 |
|---|---|---|
| 이력 한 행 | 메시지의 `people` 항목 하나 | 11장 `ppe_state` 정의가 "World ID·`identity_revision` 별" 상태다. 시간 열은 그 사람의 `capture_time_ms`(촬영 시각, 수신 시각 아님) |
| 이력 PK | `(observation_at, source_id, stream_epoch, state_sequence, world_id)` | hypertable 의 유일 키에는 시간 열이 들어가야 한다. 멱등 키 + `world_id` 로 사람 행을 가른다 |
| 재전송 거르기 | 멱등 키 색인 + 촬영 시각 하루 범위 조회 | 압축 청크까지 훑지 않게 범위를 건다. 재연결 재전송은 분 단위다 |
| 현재 상태 키 | `(source_id, world_id)` | 지금은 Tier 0 하나(`tier0-01`)지만, 이력과 같은 키 공간을 쓴다 |
| 현재 상태를 덮는 조건 | `(identity_revision, 촬영 시각, stream_epoch, state_sequence)` 가 저장된 것보다 클 때만(PostgreSQL 행 비교) | 12.3 "과거 메시지는 현재 상태를 덮지 않는다". `identity_revision` 이 낮은 상태는 이전 사람의 근거라 먼저 본다(계약 `person_state.identity_revision` 설명). PPE 가 재시작하면 순번은 0 부터지만 촬영 시각이 더 새로우므로 덮는다 |
| 만료(받는 순간) | 촬영 시각 + 10초 < EC2 수신 시각이면 이력에만 두고 현재 상태는 덮지 않는다 | 12.3 "만료된 메시지는 덮지 않는다". 10초는 계약 `expires_at_ms` 초기값(마지막 근거 + 10초, 5.5)과 같다 |
| 압축·보관 | 1일 청크, 7일 뒤 압축, 상시 삭제 없음 | 11.5 "`world_track_state` 와 같은 압축·보관 정책". 압축 설정에 PK 열을 모두 넣는다(`system_metric` 과 같은 방식) |
| 빈 `people` | `stored` 로 센다 | 처리는 했고 바뀐 것이 없다. `duplicate` 로 세면 재전송과 섞인다 |

읽는 쪽(후속 화면 전달)이 지킬 것: `ppe_current_state` 의 `expires_at` 이 지났으면 UNKNOWN 으로 보고, 현재 `world_state` 의 `identity_revision` 과 다르면 그 사람에게 붙이지 않는다(계약 설명). 이 표는 지난 상태를 지우지 않는다.

## 버린 대안

- 현재 상태를 메모리에만 둔다: EC2 가 재시작하면 다음 메시지(0.5~1초)까지 비지만, 화면 전달을 붙일 때 REST 로 처음 상태를 줄 곳이 없다. 표 하나라 비용이 작다.
- 이력을 메시지 한 행(`people` jsonb)으로 둔다: 11장이 사람별 상태를 정의하고, 리플레이·G9 집계가 World ID 로 거른다.
- 멱등을 `msg_id` 로 본다: 계약이 멱등 키를 `(source_id, stream_epoch, state_sequence)` 로 정했다. outbox 재전송은 `msg_id` 가 같지만, 계약 키가 정본이다.
- 만료를 `expires_at_ms` 로만 본다: UNKNOWN 항목은 `expires_at_ms` 가 null 이라 만료를 판단할 수 없다. 촬영 시각으로 보면 상태와 관계없이 같은 기준이다.

## 잔여 위험

- 만료 판단은 Tier 0 시계(촬영 시각)와 EC2 시계를 같은 UTC 로 본다. EC2 시계가 10초 넘게 앞서면 현재 상태가 갱신되지 않는다(이력은 남음). 지금은 수신 시각 보정 값을 쓰지 않는다.
- 통합 시험(`PpeStateIntegrationTest`)은 시험 DB(TimescaleDB)가 있어야 돈다. 2026-11-05 노트북에서 CI 와 같은 이미지(`timescale/timescaledb-ha:pg17-all`, 같은 digest)의 빈 DB 로 V1 부터 적용해 `./gradlew build` 전체(시험 555건)가 통과했다.
- 시험은 촬영 시각을 시험 직전에 정한다. 필드 초기화로 정하면 첫 시험이 Spring 컨텍스트 기동(약 13초)을 기다리는 동안 만료 기준(10초)을 넘겨 현재 상태가 갱신되지 않는다(처음 실패한 원인).

## 넘긴 일

- 관제 화면 전달 토픽(I2)과 뷰어 표시(13.3): 토픽은 명세 19.1 미결이다. 정하면 저장 뒤 이벤트(`WorldStateStored` 처럼)를 내서 토픽으로 싣는다.
- R9 판정: Tier 0 Risk Engine 이 한다(7.2). G9·G6-R9 뒤.
- 리플레이에 PPE 상태를 싣는지: 명세 8.5 에 없다.
