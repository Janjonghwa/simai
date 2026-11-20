# 시연 워치 자동 다시 잇기와 사라진 ID 배정 갈아 끼우기

S15P31A101-826. 관련 명세: 부록 C.6(워치 알림·배정), 11.4-5(익명 World ID 배정), AGENTS 규칙 6(추가 전용)·7(관측과 추정 구분).
관련 설계: [`server-watch-match.md`](server-watch-match.md)(-340·-508·-793), [`server-watch-alert.md`](server-watch-alert.md)(-338·-510·-793).

## 배경 (2026-11-19 시연 사고 및 11-20 사용자 요구·2차 조정)

1. 16:27:53 리허설 중 `watch-01` 이 관리자 수동 배정(`watch_binding.method='MANUAL'`)으로 `w-mwn81pid-0021` 에 묶였다.
2. `WatchMotionMatcher` 는 확정된 수동 배정을 건드리지 않으므로, 본 시연(18:11~18:14)의 유일한 사람 `w-mwnb36dh-0004` 로 바뀌지 않고 사라진 ID 에 끝까지 묶여 있었다.
3. 경보는 시연 스위치(`simai.watch.demo-device`, -793) 덕분에 워치로 갔지만, 3D 이름표는 워치 이름·아이콘 없이 「작업자 #157」로 보였다.
4. 시연 흐름에서는 배우가 로비 중앙에 서 있는 상태에서 **작업 시작 전에 이미 워치가 이어져 이름표가 떠 있어야** 한다(사용자 요구 2026-11-20).
5. 다만 **시간 경과만으로 배정을 풀면 안 된다**(사용자 지적 2026-11-20 2차): Re-ID(20차 2/2)가 현장을 1분 넘게 나갔다 다시 들어온 사람에게 원래 World ID 를 되살려 주므로, 시간으로 풀면 돌아온 사람의 수동 배정과 표시 이름이 사라진다. 따라서 배정은 시간이 지나도 풀지 않고, 배정된 World ID 가 **지금 보이지 않는 동안(>10초)** 새 후보(시연 1명 `DEMO_SINGLE` 또는 걸음 대조)가 나타났을 때만 기존 배정을 `REPLACED` 로 닫고 갈아 끼운다.

## 변경 내용

- 코드:
  - `server/.../domain/watch/WatchBindings.java`: `Binding.demoSingle()`·`provisional()`, `MOTION_OWNED` 에 `DEMO_SINGLE` 포함, `bindByMotion(..., Long replaceableBindingId)`(사라진 ID 의 확정 수동 배정 1건만 `REPLACED` 교체 허용, 동시 충돌 시 롤백), `bindDemoSingle(deviceId, worldId, replaceableBindingId)` 추가
  - `server/.../domain/watch/WatchMotionMatcher.java`:
    - 시간 경과에 따른 자동 배정 해제 제거(수동·대조·임시 모두 Re-ID 재입장까지 유지)
    - `isTrackVisible(worldId, now)`(`TRACK_GONE_MS = 10_000`) 판정 추가: 배정된 World ID 가 지금 보이지 않을 때만 새 후보(걸음 대조 또는 시연 1명 잇기 `DEMO_SINGLE`)가 그 워치를 `REPLACED` 로 갈아 끼울 수 있음(확정 수동 포함)
    - `bindDemoSingleIfEligible(now)`: 작업 시작 여부와 무관하게 대조 주기마다 확인, `simai.watch.demo-device` 켜짐 ∧ LIVE 추적 1명 ∧ 5초 초과 ∧ 대상 트랙 배정 없음 ∧ 시연 워치가 미배정이거나 기존 배정 ID 가 보이지 않음 → `DEMO_SINGLE`(임시 성격, 반대 근거 `< 0` 으로 `MATCH_LOST` 해제, 걸음 일치 시 `MOTION_MATCH` 승격)
  - `server/.../domain/watch/WatchBindingNotifier.java`: I4 `watch_binding_notice` 1.0.0 계약 호환을 위해 `DEMO_SINGLE` 은 `MOTION_MATCH` 로 송출
  - `server/.../domain/watch/WatchProperties.java`: `demoDevice` 설명 갱신
- 마이그레이션: `server/src/main/resources/db/migration/V86__watch_binding_demo_rebind.sql` — `watch_binding_method_check` 에 `DEMO_SINGLE` 추가 및 COMMENT 갱신(`unbind_reason` 은 기존 `REPLACED` 사용으로 제약 변경 없음)
- 시험: `server/src/test/java/com/ssafy/simai/domain/watch/WatchDemoRebindIntegrationTest.java`

## 명세에 없어 정한 값과 근거

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 시간 경과 해제 없음 | `watch_binding` 은 미관측 시간만으로 닫지 않음(수동·대조·임시 모두) | Re-ID 가 1분 넘게 현장을 벗어났다 돌아온 사람에게 원래 World ID 를 되살린다. 갈아 끼워지기 전에 같은 ID 로 돌아오면 수동 배정·표시 이름이 그대로 유지된다 |
| 갈아 끼우기 허용 기준 | 배정된 World ID 가 최근 `TRACK_GONE_MS`(10초) 안에 관측되지 않음(`isTrackVisible == false`) | 기존 `WatchMotionMatcher` 의 사라짐 기준(10초)을 그대로 써 잠깐 가려짐(10초 이내) 동안은 갈아 끼우지 않고, 확실히 화면에서 사라진 옛 ID 에 묶인 워치(확정 수동 포함)만 새 후보가 이어받게 한다 |
| 갈아 끼우기 사유·원자성 | 기존 `unbind_reason = 'REPLACED'`, 읽은 `binding_id` 한 행만 닫고 같은 트랜잭션에서 새 행 추가(유일 색인 충돌 시 롤백) | AGENTS 규칙 6(추가 전용)을 지키며 새 `unbind_reason` 값을 늘리지 않는다. `binding_id` 를 지정해 같은 순간 관리자가 새로 만든 수동 배정을 덮어쓰지 않고, 한 트랜잭션이라 워치에 중간 `UNBOUND` 알림 없이 새 `BOUND` 만 한 번 간다 |
| 시연 1명 자동 배정 방식 값 | 새 `method = 'DEMO_SINGLE'`, `score = 0` | DB·운영 조회에서 실제 걸음 상관(`MOTION_MATCH`)과 시연 단일 인원 추정(`DEMO_SINGLE`)을 구분한다(관측·추정 구분). 임시 성격(유지 문턱 `0`)을 `MOTION_MATCH`(`KEEP_SCORE = 0.6`)와 가를 수 있다 |
| I4 배정 알림(`watch_binding_notice`) 매핑 | `DEMO_SINGLE` → `MOTION_MATCH` 로 송출 | 계약 `watch_binding_notice` 1.0.0 의 `method` 는 닫힌 enum(`MOTION_MATCH`·`WAVE`·`MANUAL`)이다. 계약을 올리지 않고 기존 워치 앱과 100% 호환한다. 프론트(`watchStatus.ts`)도 `MANUAL` 외를 모두 `자동`으로 표시한다 |
| 시연 1명 조건 | `simai.watch.demo-device` 켜짐 ∧ 최근 3초(`TRACK_STALE_MS`) 관측 트랙이 **정확히 1개** ∧ `lastSeen - firstSeen > 5_000` ∧ ID 교정 직후 아님 ∧ 걸음 대조 점수 음수 아님 ∧ 대상 ID 배정 없음 ∧ (시연 워치 미배정 ∨ 시연 워치 기존 배정 ID 가 >10초 미관측) | 작업 시작 전·후 무관하게 대조 주기(기본 2초)마다 확인한다. 2명 이상이면 걸음 대조에 맡긴다. 반대 근거(`< 0`)로 `MATCH_LOST` 풀린 직후 다시 붙는 진동(무한 루프)을 막는다 |
| 임시 성격 유지·승격 | 반대 근거(`< 0`)면 `MATCH_LOST`, 경쟁 짝이 뽑히면 `REPLACED`, 같은 짝이 대조 문턱(`S >= 1.2`)을 넘으면 `bindByMotion` 으로 `MOTION_MATCH` 승격(`DEMO_SINGLE` 은 `REPLACED`) | 확정 수동처럼 굳히지 않고, 걸음이 맞으면 진짜 대조 배정으로 자연스럽게 넘어간다. 같은 World ID 라 워치 배정 알림은 반복 발송되지 않는다 |
| 경보 중복 방지 | 기존 `WatchAlertService.alert` 유지(`!devices.containsValue(demo)` + `send` 의 DB 중복 거름) | `DEMO_SINGLE` 로 묶이면 일반 배정 경로로 한 번만 발송되고, 시연 스위치 예비 분기는 건너뛴다 |

## 버린 대안

1. **60초 초과 미관측 배정 자동 해제(`STALE`·`STALE_AT_START`, 1차안)**: 작업 시작 전·후로 60초 안 보인 배정을 먼저 풀면, 1분 넘게 현장을 나갔다 Re-ID 로 원래 World ID 를 되찾아 돌아온 작업자의 수동 배정과 이름까지 사라진다. 시간으로 풀지 않고 사라진 동안 새 후보가 나타날 때만 `REPLACED` 로 갈아 끼우는 쪽이 Re-ID 재입장 보존과 11-19 사고 방지를 둘 다 만족한다.
2. **`DEMO_SINGLE` 대신 `MOTION_MATCH` 재사용**: `MOTION_MATCH` 는 유지 문턱이 `0.6` 이라, 서 있다가 약한 걷기(+0.2) 한 번만 들어와도 `MATCH_LOST` 로 풀려 깜빡인다. 또한 DB 이력에서 실제 걸음 대조와 시연 단일 인원 배정을 구별할 수 없다.
3. **작업 시작(`POST /api/work-units/{id}/start`) 시 별도 해제 훅**: 작업 시작 전 로비에 서 있는 배우에게 이미 이름표가 떠 있어야 하므로(11-20 사용자 요구), 작업 시작과 무관하게 상시 대조 주기에서 갈아 끼우도록 단일화했다.

## 남은 위험

- 시연 스위치가 켜진 상태에서 워치 미착용자 1명이 혼자 5초 넘게 화면에 서 있고 시연 워치의 기존 배정 ID 도 10초 넘게 안 보이면 시연 워치가 그 사람에게 임시(`DEMO_SINGLE`)로 갈아 끼워진다(시연 스위치 자체가 1인 시연 전용이며, 워치 착용자가 걷기 시작하거나 2명 이상이 되면 반대 근거·걸음 대조가 정리한다). 본 운영에서는 `simai.watch.demo-device` 를 비워 둔다.
- 수동 배정된 작업자 A 가 10초 넘게 현장을 비운 사이 작업자 B 가 A 의 워치를 차고 걷기 시작하면 걸음 대조가 A 의 수동 배정을 B 로 갈아 끼운다(의도된 동작이나, 워치 없이도 A 와 우연히 3창 이상 같은 리듬으로 걷는 일은 `BIND_SCORE = 1.2`·`MARGIN = 0.4` 문턱으로 낮춘다).
- 대조 주기(`simai.watch.match-interval`, 기본 2초)마다 확인하므로 5초 조건 충족 뒤 최대 2초의 지연이 있을 수 있다.
