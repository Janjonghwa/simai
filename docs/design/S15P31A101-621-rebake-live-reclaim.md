# S15P31A101-621 맵 다시 굽기 LIVE 회수(A)와 관제 경보 복구 정합(B)

A(세팅)와 B(관제 경보 복구)를 한 이슈에서 다룬다. A 는 아래 "A." 절들, B 는 문서 끝 "B." 절이다.

## 관련 명세

- **명세 3.2 5항**(`docs/spec/03-system.md`)
  - 작업 시작(LIVE)을 누르면 백그라운드 후처리 자원을 즉시 회수한다.
  - AGENTS 작업 방식: LIVE 관제 시간에 오프라인·배치 Job 을 돌리지 않는다.
- **선행 티켓**
  - -597: 정식 굽기 회수(`bake`·`fallback` 단계).
  - -605: 보조 촬영(`supplement` 단계), SUPPLEMENT_REBAKE 워커.
  - -607: 맵 보강 촬영 업로드·다시 굽기 요청·결과 반영.
- **발견 경위**: 2026-11-11 개인 감사(dev 9e213260) G1·G2(P1).

## 결함

1. **G2: 다시 굽기가 LIVE 중에도 돈다.**
   - 회수 대상이 `bake`·`fallback` 뿐이었다. 그래서 다시 굽기의 `prepare`(180 s)·`mapanything`(1,200 s)·`supplement`(1,200 s)와, 일반 세팅 정식 굽기 안의 `supplement` 가 작업 시작 뒤에도 계속 돌았다.
   - 다시 굽기는 job_type `site_setup` 이라 워커 LIVE 접수 차단에서 빠진다(`gpu/admission.py`). 서버는 요청할 때만 IN_PROGRESS 를 봤다.
   - rebake 경로는 `_reclaim_as_no_scene` 로 감싸지지 않았다. 그래서 bake 단계에서 회수되면 `BakeReclaimed` 가 runner 에서 재시도 불가 `HANDLER_ERROR` 가 됐다.
2. **G1: 회수 세션의 다시 굽기 결과가 반영되지 않는다.**
   - -597 이 회수한 세션은 `bake_skipped='LIVE_WORK_STARTED'`, `scene_version NULL` 이고 V50 CHECK 가 이를 강제했다.
   - -607 은 이 세션도 미리보기를 기준으로 다시 굽기를 허용한다. 그런데 APPLIED 면 `UPDATE setup_session SET scene_version` 이 CHECK 에 걸린다.
   - 결과 반영은 결과 제출과 같은 트랜잭션(동기 리스너)이다. 그래서 제출 전체가 되돌아가고, 다시 굽기는 끝나지 않는다.

## 바꾼 것

| 경계 | 위치 | 내용 |
|---|---|---|
| 계약 | `contracts/messages/site_setup.schema.json` 1.12.0, `job_callback` 2.19.0, `job_heartbeat` 2.11.0 (v7.35.0) | `supplement_summary.reason` 에 `LIVE_WORK_STARTED`. 예시 `job_callback.site_setup_supplement_rebake_reclaimed` |
| 워커 | `site_setup_stages._RECLAIMABLE_STAGES` | `{bake, fallback}` → `{bake, fallback, supplement}`. 정식 굽기 안의 보조 촬영도 회수되고, 기존 감싸기가 `bake_skipped` 로 바꾼다 |
| 워커 | `site_setup_stages._REBAKE_RECLAIMABLE_STAGES`, `rebake()` | 다시 굽기에서는 `prepare`·`mapanything` 도 회수한다. `BakeReclaimed` 를 받아 원래 맵 유지 결과(`scene_version` = 기준, `artifacts.scene` 없음)로 끝낸다. 보조 촬영 단계 전이면 SKIPPED, 그 뒤(보조 촬영·굽기 중)면 FALLBACK, 사유 `LIVE_WORK_STARTED` |
| 워커 | `client.py` | `SCHEMA_VERSION` 2.11.0, `CALLBACK_SCHEMA_VERSION` 2.19.0 |
| 서버 | `JobLeaseService.tryLease` | `input.mode = 'SUPPLEMENT_REBAKE'` 인 site_setup 은 같은 현장(`input.site_id`)에 IN_PROGRESS 작업이 있으면 대여하지 않는다(QUEUED 유지) |
| 서버 | `V58__setup_session_bake_skipped_rebake.sql` | V50 CHECK 에서 `scene_version IS NULL` 조건만 뺀다. `bake_skipped` 는 회수 이력으로 그대로 남는다 |
| 프런트 | `features/setup/supplement.ts` | 사유 문구 `LIVE_WORK_STARTED` = "작업이 시작돼 다시 굽기를 멈췄습니다(원래 맵 유지)" |

일반 세팅의 READY_FOR_ZONES 전 단계(`prepare`·`mapanything`·`preview`)는 회수 대상에 넣지 않았다. 이 단계에서 작업이 시작되는 것은 서버 `startSession` 이 세팅 단계를 보지 않아서 생기는 다른 문제(감사 M5)이고, -615(세팅 완료) 흐름에서 다룬다.

## 명세에 없어 정한 것과 근거

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 회수된 다시 굽기의 결과 | SUCCEEDED + COMPLETED, 원래 맵 유지 | 다시 굽기는 기준 맵을 바꾸지 않는 추가 작업이라 실패가 아니다. 기존 FALLBACK·SKIPPED 의미(새 Scene 등록·활성화 없음)와 같다. 관리자는 작업이 끝난 뒤 다시 요청할 수 있다 |
| SKIPPED 와 FALLBACK 구분 | 보조 촬영 단계 전 = SKIPPED, 보조 촬영·굽기 중 = FALLBACK | 계약의 기존 정의(SKIPPED = 넣어 보지 않음, FALLBACK = 넣어 봤지만 못 씀)를 따른다 |
| 대기 중 다시 굽기 | 작업이 진행 중인 동안 QUEUED 로 두고, 끝나면 대여 | 요청을 버리면 관리자가 다시 눌러야 한다. 대기는 GPU 를 쓰지 않는다. 작업 대기 마감(deadline)은 원래 없다 |
| V58 CHECK | `bake_skipped IS NULL OR (bake_skipped = 'LIVE_WORK_STARTED' AND status = 'COMPLETED')` | 회수 결과에서 `scene_version` 이 비는 것은 계약(result 의 `bake_skipped` ⇔ `scene_version` null)과 `SetupService` 가 지킨다. DB 는 이력 값만 지킨다 |

## 버린 대안

- **세션 맵 포인터를 바꾸지 않고 rebake 행의 `result_scene_version` 을 최신 맵으로 읽기**
  - 덮어쓰기는 없다. 그러나 세션 조회·Scene 활성화·프런트 표시(`bake_skipped && !finalSceneVersion`)와 -615 의 "이 세션 최종 맵" 계산을 모두 바꿔야 한다.
  - 일반 세션의 다시 굽기는 이미 포인터를 옮기고 이력을 rebake 행에 남긴다. 회수 세션만 다르게 둘 이유가 없다.
- **회수된 다시 굽기를 FAILED(재시도 가능)로 두기**: 대여 재시도(`REBAKE_MAX_ATTEMPTS=2`)가 LIVE 중에 다시 대여를 시도하고, 화면에는 실패로 보인다.
- **다시 굽기를 별도 job_type 으로 나누기**: 워커 등록 유형·admission·계약 `job_type` 판이 모두 바뀐다. 대여 쿼리 조건 하나로 같은 효과를 낸다.

## 시험

- **ai**
  - `tests/services/worker/test_site_setup_supplement.py`
    - 다시 굽기 4단계 각각의 회수(원래 맵, 사유, 다음 단계 미실행, 계약 통과).
    - 취소 우선.
    - 정식 굽기 안 보조 촬영 회수.
    - prepare·mapanything 회수는 다시 굽기에서만.
  - `tests/services/worker/test_client.py` 와 `tests/contracts`: 예시 이야기 일관성.
- **서버**: `SetupSupplementIntegrationTest`
  - 회수 세션 + APPLIED 다시 굽기가 커밋되고 세션 맵이 옮겨지며 `bake_skipped` 이력이 남는다.
  - 작업 진행 중에는 대기 중 다시 굽기를 대여하지 않고, 끝나면 대여한다.
- **프런트**: 기존 `supplement.test.ts` 가 계약의 모든 사유 코드에 문구가 있는지 본다.

## 잔여 위험

1. **회수 지연**: EC2 와 통신이 끊기면 회수가 대여 유지 시간(180 s)만큼 늦어진다(-597 과 같음). 워커 쪽 로컬 LIVE 신호는 없다.
2. **이미 대여된 다시 굽기**: 대여 시점 가드는 대기 중 작업만 막는다. 이미 대여된 것은 하트비트 신호(최대 약 5 s)로 회수한다.
3. **서버 `startSession` 이 세팅 단계를 보지 않는다**(M5): READY_FOR_ZONES 전에 작업을 시작하면 일반 세팅의 prepare·mapanything 은 계속 돈다. -615 에서 다룬다.
4. **-615 와의 경계**: -615 가 "다시 굽기는 세팅 완료 전까지만"을 넣으면 회수 세션(작업 시작 = 완료 뒤)의 다시 굽기는 요청 단계에서 막힌다. V58 은 그와 무관하게 결과 반영이 실패하지 않게 한다.

## 배포 순서

순서 제약은 없다.
- EC2 는 보조 촬영 요약을 JsonNode 로 받아 사유 값을 검사하지 않는다.
- 관제 앱은 모르는 사유를 `사유 <코드>` 로 보인다.
- V58 은 CHECK 를 넓히기만 한다. 제약 이름은 V50 이 짓지 않아 환경마다 다를 수 있으므로 정의로 찾아 지운다.

---

# B. 관제 경보 복구 정합 (NC1·F01·F02)

## 관련 명세

- **명세 3.2 재동기화 규칙**: 스냅샷 뒤 동기화를 끝내고, 활성 이벤트는 DELAYED_ACTIVE 로 알리며, 경과 시간만으로 억제하지 않는다.
- **명세 12.4**: 관제 소리·점멸은 실시간 알림 대상만이다. AUDIT_ONLY 는 알림 0 이다.
- **선행**: -595(`held_by_tier0`, 「진행 중」/「지난 관제」 구분).

## 결함

1. **NC1**
   - `EventStateSnapshotIngestor` 의 분류 트랜잭션이 일시적 DB 오류로 실패하면 `finishSync()` 를 건너뛰고 연결은 열어 둔다.
   - Tier 0 는 같은 (연결 세대, 재요청 횟수)에 스냅샷을 다시 보내지 않는다(`ai/services/tier0/event_snapshot.py`). 그래서 연결이 끊길 때까지 모든 전이가 AUDIT_ONLY 로 저장되고 알림이 없다.
   - `held.apply` 는 REALTIME 전이에서만 불렸다. 그래서 그동안 새로 난 이벤트는 `held_by_tier0=false` →「지난 관제」로 분류됐고, 새로고침으로도 회복되지 않았다.
2. **F01**
   - 관제 앱은 받은 쪽에 해소 전 이벤트가 없으면 받기를 멈춘다.
   - 서버는 해소 여부와 관계없이 최신 발생순으로 줬다. 그래서 최근 해소 이벤트가 쪽을 채우면 더 오래된 활성 경보를 받지 못했다.
3. **F02**: `isAlarming` 이 `delivery` 를 보지 않았다. 그래서 REST 로 다시 받은 AUDIT_ONLY 활성 이벤트(Tier 0 가 들고 있는 것)가 울렸다.

## 바꾼 것

| 경계 | 위치 | 내용 |
|---|---|---|
| 서버 | `EventStateSnapshotIngestor.ingest` | 분류가 실패하면 연결을 닫고 예외를 다시 던진다(실패 계수 유지). 다시 붙으면 Tier 0 가 새 세대로 저널·스냅샷을 다시 보내 분류한다 |
| 서버 | `RiskEventTransitionIngestor.record` | 현재 상태를 바꾼 전이(APPLIED)는 동기화 중에도 `held.apply` 한다. 알림 발행은 그대로 REALTIME 만 한다 |
| 서버 | `EventQueryRepository.findPage` | `ORDER BY (lifecycle = 'RESOLVED'), COALESCE(raised_at, updated_at) DESC NULLS FIRST, event_id`: 해소 전 이벤트를 먼저 준다. 관제 앱의 "활성 없는 쪽에서 멈춤"이 맞는 조건이 된다 |
| 프런트 | `features/events/entries.ts isAlarming` | `delivery === 'AUDIT_ONLY'` 면 울리지 않는다. DELAYED_ACTIVE·REALTIME 은 그대로 |

## 버린 대안

- **분류 실패 때 `journal_ack.resend_from_offset` 으로 재요청**
  - 연결을 닫지 않아 가볍다. 그러나 DB 장애가 이어지면 같은 연결에서 재전송·재분류가 빠르게 되풀이된다.
  - 연결 닫기는 Tier 0 의 재접속 백오프를 그대로 쓴다.
- **관제 앱이 상한(5쪽)까지 모두 받기**: 재연결마다 요청이 늘고, 해소 이벤트가 500건을 넘으면 여전히 빠진다.
- **활성 전용 조회 파라미터 추가**: 서버·프런트 둘 다 바뀐다. 정렬 하나로 기존 프런트 조건이 맞게 된다.

## 시험

- `RiskEventJournalIntegrationTest`
  - 스냅샷 분류에 일시적 DB 오류를 한 번 넣으면 EC2 가 연결을 닫고, 다시 붙은 뒤 새 스냅샷으로 DELAYED_ACTIVE 가 된다.
  - 동기화 중 RAISED 는 들고 있음에 들어가고 RESOLVED 는 빠진다.
- `EventAckIntegrationTest`: 더 최근에 해소된 이벤트가 있어도 해소 전 이벤트가 첫 쪽에 온다.
- 위 세 시험은 고치기 전 코드에서 실패함을 확인했다.
- `events.test.ts`: AUDIT_ONLY 는 울리지 않고, DELAYED_ACTIVE·REALTIME 은 울린다.

## 잔여 위험

1. **R3(이 이슈 범위 밖)**
   - 끝나지 못한 동기화에서 기존 REALTIME 이벤트가 상향되면 delivery 가 REALTIME 으로 남는다(`COALESCE(:delivery, delivery)`).
   - 다음 스냅샷의 touched 집합은 메모리라 재시작을 넘지 못한다.
   - 이번 변경(F02)으로 이 이벤트는 울림 대상으로 남는다(그 전과 같음).
2. **상한**: 해소 전 이벤트 자체가 500건을 넘으면(지난 관제 미해소 누적) 가장 오래된 것은 받지 못한다. 「진행 중」(Tier 0 가 들고 있음)은 최근 것이라 앞쪽에 있다.
3. **정렬 비용**: 정렬이 바뀌어 인덱스 없이 전체 정렬한다. LIVE 이벤트 수천 건 규모에서는 문제없다. 수만 건이 넘으면 `(lifecycle = 'RESOLVED', raised_at)` 식 인덱스를 검토한다.
4. **재접속 주기**: 분류 실패가 이어지면(DB 장애) Tier 0 재접속 주기마다 연결이 닫힌다. 노트북 현장 경보는 이 연결과 무관하다.
