# 사람 표시 이름 해석 및 3D 이름표 A안 설계 (S15P31A101-658)

실시간 관제 화면의 3D 이름표·사람 상세 패널·워치 연결 알림·이벤트 카드·2D 비상 평면에서 익명 `world_id`(`w-0012`)와 기기 ID(`watch-01`) 나열 대신 **사람이 즉시 읽을 수 있는 표시 이름**(`김철수`, `미확인 1`)과 **직관적인 워치 상태 아이콘(A안)**을 일관되게 보여 주기 위한 서버·계약·프론트엔드 설계 문서다.

---

## 1. 관련 명세 절

| 명세 절 | 관련 규칙 및 본 설계 반영 내용 |
|---|---|
| `docs/spec/05-processing.md` §5.2 (ID 교정 기록·병합·분할) | Tier 0 가 병합 시 생존 ID 에 사라진 이전 ID 를 `aliases` 로 싣고, 분할 시 갈라져 나간 자식 ID 에만 부모 ID 를 `split_from` 으로 싣는다(`world_state` 2.1.0). 서버 표시 이름 해석기는 이 계보를 그대로 따라 병합 시 당일 수동 이름·미확인 번호를 승계하고 분할 시 자식에게 새 미확인 번호를 부여한다. |
| `docs/spec/11-data.md` §11.1·§11.4·§11.5 (추가 전용 이력·데이터 최소화) | 원칙 1에 따라 모든 이름 지정·해제·미확인 번호 부여·병합 계보는 **추가 전용(append-only)** 이력 테이블(`V65`)에 쌓고 기존 행을 덮어쓰거나 삭제하지 않는다(AGENTS 규칙 6). 원칙 5(NFR-14)에 따라 `world_track_state`·`risk_event`·`work_unit` 등 위치·이벤트·작업 원본 테이블에는 사람 이름을 넣지 않고 외부 LLM 에도 절대 전송하지 않는다. |
| `docs/spec/12-interfaces.md` §12.1·§12.3·§12.4·§12.6 (계약·전달 경로) | **I1 `world_state` 는 전혀 수정하지 않으며**, `/topic/world` 도 I1 `world_state` 를 그대로 중계한다(§12.4). 관제 구독 채널인 `GET /api/health` 스냅샷과 STOMP `/topic/health`(`viewer_health`)의 열린 `payload` 최상위에 마이너 호환 필드 `person_names`·`watch_names` 를 추가한다(`viewer_health` `1.5.0`, 계약 묶음 `v7.43.0`). |
| `docs/spec/13-viewer.md` §13.1·§13.2·§13.3·§13.6 (뷰어·PPE·2D 비상 화면) | 3D 이름표는 기존 위험 심각도 글자 색(`riskStyle`)과 예측/미확인 투명도(`opacity`) 규칙을 그대로 유지한다(§13.2). PPE 미착용·모름 문구는 문자열 이어붙이기 대신 이름표 뒤쪽 칸(cell)으로 전달해 향후 PPE 아이콘 칸 확장을 열어 둔다(§13.3). 단일 이름 어댑터(`personDisplay.ts`)는 `three`·`viewer3d` 를 전혀 참조하지 않아 2D 비상 평면 독립성(§13.6)을 지킨다. |
| `docs/spec/14-security.md` (권한·개인정보) | 표시 이름 변경·초기화 REST API 는 관리자(`ADMIN`)만 호출할 수 있으며 `changed_by_user_id` 와 `created_at` 감사 이력을 남긴다. 관제 조회(`GET /api/health`, `/topic/health`)는 열람(`VIEWER`) 이상 인증 사용자에게만 열려 있다. |
| `docs/spec/appendix-c-extensions.md` §C.6 (워치 알림 확장) | 아바타 위 워치 채널 상태를 아이콘으로 표시하고 워치가 없는 경우 `"미착용"` 단정 대신 상세 패널에 `"연결된 워치 없음"` 으로 표기한다. |

---

## 2. 서버 표시 이름 해석 규칙

### 2.1 우선순위 (현장 `site_id` + `Asia/Seoul` 일자 기준)

하나의 활성 `world_id` 에 대해 서버(`PersonDisplayNames`)와 프론트 단일 어댑터(`personDisplay.ts`)는 동일한 3단계 우선순위로 표시 이름과 출처(`name_source`)를 결정한다.

1. **`WORLD_MANUAL` (관리자 당일 World ID 지정 이름)**
   - 관리자가 해당 `world_id` 에 당일(`Asia/Seoul` 기준 `site_date`) 지정한 최신 이력(`history_id` 최대 행)이 `SET` 이면 최우선 적용한다.
   - 워치 없는 작업자나 임시 방문자에게 즉시 이름을 붙일 때 쓰며, 자정(`Asia/Seoul`)이 지나 새 날짜가 되면 자동으로 만료된다.
2. **`WATCH_DEVICE` (워치 기기 표시 이름, 권장 경로)**
   - 현재 `watch_binding`(`unbound_at IS NULL`)으로 해당 `world_id` 에 배정된 워치 기기(`device_id`)가 있고, 그 워치의 최신 표시 이름 이력이 `SET` 이면 적용한다.
   - 워치 연결이 일시 끊긴 상태(`LOST`)라도 배정(`watch_binding`)이 유지되는 동안에는 워치 표시 이름을 그대로 유지한다(이름이 갑자기 `"미확인 N"` 으로 바뀌어 관제사가 혼란을 겪지 않도록 함).
   - 워치 기기 표시 이름은 날짜가 바뀌어도 현장(`site_id`) 단위로 지속된다.
3. **`UNIDENTIFIED` (`"미확인 N"`, 현장·날짜별 자동 번호)**
   - 위 두 이름이 모두 없으면 `(site_id, site_date)` 별로 `1` 부터 단조 증가하는 번호를 부여해 `"미확인 1"`, `"미확인 2"` 처럼 표시한다.
   - 같은 현장·같은 날짜 안에서는 같은 `world_id` 가 사라졌다가 다시 관측되어도 항상 같은 번호를 유지한다.

### 2.2 병합(`aliases`) 승계와 분할(`split_from`) 규칙

Tier 0 가 `world_state.tracks[]` 를 올릴 때 `PersonDisplayNames.onWorldState` 가 트랙 계보를 해석한다.

- **병합(`aliases`)**: 생존 `world_id` 가 사라진 별칭 ID 들(`aliases`)을 흡수할 때:
  1. `person_world_merge_log` 에 `(site_id, site_date, alias_world_id → survivor_world_id)` 계보를 추가 기록한다(재전송 멱등).
  2. **당일 수동 이름 승계**: 생존 ID 와 별칭 ID 들 중 당일 유효한 최신 `person_world_name_history` 행들을 비교해 **가장 최근(`history_id` 가 가장 큰) `SET` 이름**이 아직 생존 ID 의 최신 값이 아니면 생존 ID 앞으로 새 `SET` 이력 행(`INHERITED_MERGE`)을 추가한다.
  3. **미확인 번호 승계**: 생존 ID 와 별칭 ID 들에 이미 부여된 당일 미확인 번호 중 **가장 작은 번호**를 생존 ID 가 이어받도록 `person_unidentified_number_history` 에 승계 행을 추가한다(먼저 본 번호 유지).
  4. 워치 배정의 병합 이동은 기존 `WatchBindings.followMerge` 가 같은 `world_state` 수신 흐름에서 처리하므로 `WATCH_DEVICE` 이름도 자동으로 생존 ID 를 따라간다.
- **분할(`split_from`)**: 한 트랙에서 갈라져 나온 자식 `world_id`(`split_from` 이 있는 트랙)는 부모의 수동 이름이나 미확인 번호를 복제하지 않는다. 부모 `world_id` 는 원래 이름·번호를 그대로 유지하고, 자식 `world_id` 는 상위 이름이 없으면 **새 `"미확인 N"` 번호**를 발급받는다.

### 2.3 입력 정규화·검증 및 동명이인(중복 이름) 구분 정책

- **입력 정규화·검증** (서버 `PersonDisplayNames.normalizeDisplayName` · 프론트 `normalizeDisplayName` · 계약 `$defs/person_display_name` 공통):
  - 앞뒤 공백(일반 공백·유니코드 공백·BOM `\uFEFF`)을 제거한다.
  - 제거 후 코드점 길이 **1~20자**만 허용한다(빈 문자열·공백만 있는 문자열·21자 이상은 `400 INVALID_DISPLAY_NAME`).
  - 제어문자(`Cc`), 서식문자(`Cf`), 줄/문단 구분자(`Zl`/`Zp`), 대리 코드점(`Cs`)을 모두 거부한다.
- **중복 이름 허용 및 화면 구분 근거**:
  - 실제 건설 현장에는 동명이인 작업자(`김철수` 2명)나 역할명 표기(`신호수`, `용접공`)가 자연스럽게 존재하므로 서버에서 중복 이름을 막지 않는다.
  - 대신 프론트 단일 어댑터가 현재 활성 인원 내 동일 표시 이름 출현 횟수를 세어 `isDuplicateName: boolean` 을 산출하고 화면 성격에 맞춰 구분한다:
    1. **3D 관제 이름표**: 사용자 결정에 따라 이름표 안에는 `world_id`·`device_id`·`"워치"` 글자를 넣지 않고 깔끔하게 이름만 그리되, 3D 공간 위치로 두 작업자가 분리되어 보이며 클릭 시 **사람 상세 패널**에서 `동명이인 있음` 배지·배정 워치 `device_id`·현재 `world_id` 를 즉시 확인할 수 있다.
    2. **2D 텍스트 목록·알림**(이벤트 카드, 사건 상세 헤더, 상단 경보 배너, 2D 비상 평면 말풍선, 워치 연결 알림): 공간 위치 없이 글자만 나열되므로 `isDuplicateName === true` 일 때 `김철수 (0015)` / `김철수 (w-0015)` / `⌚ 김철수(watch-02) 연결됨(자동)` 처럼 짧은 식별자를 괄호로 덧붙여 오인을 방지한다.

---

## 3. REST API 및 계약 (`v7.43.0`)

### 3.1 이름 변경·초기화 REST API (`PersonDisplayNameController`)

모든 쓰기 경로는 `@PreAuthorize("hasRole('ADMIN')")` 로 보호하며, 변경 성공 시 `PersonDisplayNamesChanged` 도메인 이벤트를 발행해 트랜잭션 커밋 직후(`AFTER_COMMIT`) `HealthPublisher` 가 STOMP `/topic/health` 스냅샷을 즉시 밀어준다.

| 구분 | 메서드·경로 | 설명 | 응답 |
|---|---|---|---|
| (a) 워치 기기 표시 이름 (권장·현장 지속) | `PUT /api/watches/{deviceId}/display-name` | 등록된 `WATCH` 기기의 현장 지속 표시 이름 설정 (`{ "display_name": "김철수" }`) | `200 OK` (`device_id`, `site_id`, `display_name`, `changed_by_user_id`, `updated_at`) / 미등록 기기 `404 WATCH_NOT_FOUND` / 검증 실패 `400 INVALID_DISPLAY_NAME` |
| (a) 워치 기기 표시 이름 초기화 | `DELETE /api/watches/{deviceId}/display-name` | 워치 표시 이름 해제(`CLEAR` 이력 행 추가) | `204 No Content` |
| (b) 워치 없는 사람 World ID 이름 (당일 한정) | `PUT /api/people/{worldId}/display-name` | 현재 `world_id` 에 `Asia/Seoul` 당일 한정 이름 설정 (`{ "display_name": "임시작업자" }`, 병합 시 승계) | `200 OK` (`world_id`, `site_id`, `site_date`, `display_name`, `name_source: "WORLD_MANUAL"`, `changed_by_user_id`, `updated_at`) |
| (b) World ID 당일 이름 초기화 | `DELETE /api/people/{worldId}/display-name` | 당일 World ID 수동 이름 해제(`CLEAR` 이력 행 추가 → 워치 이름 또는 기존 `"미확인 N"` 으로 복귀) | `204 No Content` |

### 3.2 서버 → 프론트 전달 계약 (`viewer_health` `1.5.0`, 묶음 `v7.43.0`)

- 스키마: `contracts/messages/viewer_health.schema.json` (`1.4.0` → **`1.5.0`**)
  - `$defs.person_display_name`: 1~20자, 앞뒤 공백 없음, 제어/서식/구분 문자 금지 정규식.
  - `payload.person_names` (선택 배열, 최대 256): `{ world_id, display_name, name_source: "WORLD_MANUAL" | "WATCH_DEVICE" | "UNIDENTIFIED" }`.
  - `payload.watch_names` (선택 배열, 최대 256): `{ device_id, display_name }`.
- **하위 호환성**:
  - `viewer_health.payload` 는 열린 객체이고 두 필드 모두 선택(optional)이므로 구버전 서버와 신버전 프론트, 신버전 서버와 구버전 프론트 모두 호환된다.
  - `watches.items` 는 `additionalProperties: false` 로 닫힌 객체이므로 건드리지 않았다.
  - **I1 `world_state.schema.json` 은 한 글자도 수정하지 않았다.**

---

## 4. 추가 전용(Append-only) 이력 저장 (`V65__person_display_name.sql`)

AGENTS 규칙 6과 명세 11.4 원칙 1에 따라 `UPDATE`·`DELETE` 없이 모든 상태 변화를 이력 행 추가로만 기록한다.

1. **`watch_display_name_history`**
   - 열: `history_id BIGSERIAL PK`, `device_id`, `site_id`, `action ('SET'|'CLEAR')`, `display_name`, `changed_by_user_id`, `created_at`.
   - 제약: `SET` 이면 `display_name` 1~20자 필수, `CLEAR` 이면 `display_name IS NULL`.
   - 현재 유효 값: `device_id` 별 `history_id DESC LIMIT 1` 행이 `SET` 일 때의 `display_name`.
2. **`person_world_name_history`**
   - 열: `history_id BIGSERIAL PK`, `site_id`, `site_date DATE`, `world_id`, `action ('SET'|'CLEAR')`, `reason ('ADMIN'|'INHERITED_MERGE')`, `source_world_id`, `display_name`, `changed_by_user_id`, `created_at`.
   - 현재 유효 값: `(site_id, site_date, world_id)` 별 `history_id DESC LIMIT 1` 행이 `SET` 일 때의 `display_name`.
3. **`person_unidentified_number_history`**
   - 열: `history_id BIGSERIAL PK`, `site_id`, `site_date DATE`, `world_id`, `unidentified_no INT (> 0)`, `reason ('ASSIGNED'|'INHERITED_MERGE')`, `source_world_id`, `created_at`.
   - 동시성 안전: 번호 발급 시 `pg_advisory_xact_lock(hashtext('person_unidentified:' || site_id || ':' || site_date::text))` 트랜잭션 잠금으로 같은 현장·날짜의 `MAX(unidentified_no) + 1` 단조 증가를 보장한다.
   - 현재 유효 값: `(site_id, site_date, world_id)` 별 `history_id DESC LIMIT 1` 행의 `unidentified_no`.
4. **`person_world_merge_log`**
   - 열: `merge_id BIGSERIAL PK`, `site_id`, `site_date DATE`, `survivor_world_id`, `alias_world_id`, `recorded_at`, `UNIQUE (site_id, site_date, alias_world_id, survivor_world_id)`.
   - 이미 기록된 별칭 쌍은 `ON CONFLICT DO NOTHING` 으로 중복 승계를 막는다.

---

## 5. 프론트엔드 단일 어댑터 및 3D 이름표(A안) 구조

### 5.1 단일 어댑터 (`front/apps/admin/src/features/people/`)

- `personDisplay.ts`: `resolvePersonDisplayMap` 이 `world_state` 활성 트랙 ID 집합, `viewer_health` 의 `person_names`·`watch_names`·`watches`, `watch-bindings` 캐시, 낙관적 변경 저장소(`personNameOverride.store.ts`)를 합쳐 `World ID → PersonDisplayInfo` 맵을 계산한다.
- `usePersonDisplay.ts`:
  - `usePersonDisplayMap()`: 10 Hz 좌표나 1 Hz 시계·배터리 숫자 변화에는 재계산하지 않도록 문자열 키(`worldIdsKeyOf`, `personNamesKeyOf`, `watchNamesKeyOf`, `linkKeyOf`)가 바뀔 때만 새 맵을 만든다. 또한 `useCachedWatchBindings()` 로 `SpatialViewportHost` 가 갱신하는 워치 배정 캐시를 수동 구독해 카드 수십 개가 중복 타이머를 만들지 않는다.
  - `usePersonRename()`: 관리자 저장·초기화 시 `personNameOverride.store.ts` 에 낙관적 값을 먼저 넣어 **3D 이름표·상세 패널·이벤트 카드가 즉시 바뀌게** 하고, 서버 응답 성공 시 확정 후 `GET /api/health` 스냅샷 보강, 실패 시 즉시 이전 이름으로 롤백하고 오류 문구를 표시한다. 워치가 배정된 작업자 이름을 고칠 때는 권장 경로인 `(a)` 워치 기기 표시 이름을 갱신하며, 만약 당일 `WORLD_MANUAL` 이름이 가리고 있었다면 함께 `CLEAR` 해 새 워치 이름이 즉시 드러나게 한다.

### 5.2 3D 이름표 A안 칸(Cell) 일반화 (`LabelSprite.tsx`, `personLabelCells.ts`)

기존 ` `${worldId} · ${badge}` ` 문자열 이어붙이기를 걷어내고 `LabelCell[]` 칸 목록 렌더러로 일반화했다.

```
┌───────────────┬──────────────┬────────────────────┐
│ [⌚ 스마트워치] │    김철수     │  안전모 미착용 (선택) │
└───────────────┴──────────────┴────────────────────┘
```

- **워치 연결(`connected`)**: 왼쪽 아이콘 칸에 둥근 스마트워치 선 아이콘(위·아래 스트랩 + 원형 베젤 + 시계 바늘, 하늘색 `#38bdf8`)과 은은한 하늘색 칸 배경(`rgba(56, 189, 248, 0.18)`)을 그리고 오른쪽에 이름 칸을 배치한다.
- **워치 끊김(`disconnected`)**: 왼쪽 아이콘 칸에 회색(`#94a3b8`) 스마트워치 선 아이콘과 우상단 작은 빨간 점(`#ef4444`, 외곽 대비 테두리 포함)을 그리고 이름은 그대로 유지한다.
- **워치 없음(`none`)**: 왼쪽 아이콘 칸을 두지 않고 이름 칸만 깔끔하게 그린다.
- **워치 연결 순간 강조(`bindHighlight`)**: 배정 성립 뒤 6초 동안 큰 이름표(`prominent`)로 `[워치 연결 아이콘 칸, "김철수 연결됨"]` 을 띄우고 상단 알림(`WatchBindNotice`)에 `"⌚ 김철수 연결됨(자동)"` 을 표시한다.
- **PPE 연동 및 향후 확장**: `ppeBadges` 문구는 뒤쪽 `{ kind: 'text', text }` 칸들로 붙이며, 향후 PPE 아이콘 칸을 추가할 때 `LabelCell` 유니온에 새 칸 종류만 더하면 렌더러 구조를 바꾸지 않고 확장할 수 있다.
- **위험 색·투명도 유지**: 기존 `riskStyle`(CRITICAL 빨강·HIGH 주황·CAUTION 노랑 글자/테두리 색)과 관측 상태별 `opacity`(OBSERVED 1 / PREDICTED·UNKNOWN 감쇠)는 그대로 유지한다.

### 5.3 사람 상세 패널 (`PersonDetailPanel.tsx`)

3D 또는 2D 에서 작업자를 선택(`selectedKey = "person:<worldId>"`)하면 우측 `MonitorSideColumn` 상단에 표시된다:
- 해석된 표시 이름, 동명이인 배지(`동명이인 있음`), 이름 출처(`워치 기기 이름 · 현장 지속` / `관리자 지정 · 오늘 한정` / `미확인 자동 번호 · 오늘 한정`)
- 워치 상태(`연결됨` / `연결 끊김` / `연결된 워치 없음`) 및 배정 기기 ID(`watch-01`)
- 현재 `World ID`(`w-0012`)
- `ADMIN` 전용 인라인 이름 변경 폼(1~20자 입력, 저장·수동 이름 초기화 버튼, 경로 안내 문구)

---

## 6. 버린 대안과 근거

1. **World ID 에만 이름을 붙이는 안 (기각)**
   - 카메라 사각지대 장기 이탈·재입장이나 Tier 0 재시작 시 `world_id` 가 새로 발급되면 관리자가 지정한 작업자 이름이 그날 안에도 끊어진다. 현장 지속이 필요한 작업자는 물리 기기인 **워치 기기 표시 이름(`WATCH_DEVICE`)**에 묶고 1:1 배정(`WatchBindings`)을 통해 현재 `world_id` 로 투영해야 재입장·날짜 변경 뒤에도 이름이 유지된다.
2. **I1 `world_state` 에 표시 이름을 싣는 안 (기각)**
   - Tier 0 실시간 안전 경로(10 Hz)는 사람 실명·별칭을 알 필요가 없으며, `world_state` 에 이름을 실으면 10 Hz 대역폭을 낭비할 뿐 아니라 위치·이벤트 정본에 사람 이름을 두지 않는 원칙(명세 11.4-5, NFR-14)과 충돌한다. 지시서에서도 I1 `world_state` 무변경을 명시했다.
3. **`viewer_health.payload.watches.items` 배열 원소에 이름을 넣는 안 (기각)**
   - `viewer_health.schema.json` 의 `watches.items` 원소는 `additionalProperties: false` 로 닫힌 객체이고, 무엇보다 **워치가 없는 작업자**(`"미확인 N"`·당일 `WORLD_MANUAL`)를 표현할 수 없다. 열린 `payload` 최상위에 `person_names`·`watch_names` 를 마이너 추가하는 편이 기존 검증기를 깨지 않으면서 모든 작업자를 포괄한다.
4. **별도 STOMP 토픽(`/topic/person-names`)과 별도 초기 조회 API 를 신설하는 안 (기각)**
   - 관제 화면은 이미 진입 시 `GET /api/health` 스냅샷을 받고 `/topic/health` 를 상시 구독한다. 별도 토픽을 늘리면 재연결 복구·권한 검사·프론트 스토어 동기화 지점이 이원화된다.
5. **3D 이름표에 `"워치 watch-01 · w-0012 · 안전모 미착용"` 문자열을 계속 이어붙이는 안 (기각)**
   - 원거리 3D 시점에서 글자가 길어져 아바타들을 가리고 정작 누구인지 한눈에 읽기 어렵다. 식별 코드(`watch-01`, `w-0012`)는 선택 시 상세 패널로 옮기고 이름표는 `[아이콘 칸 + 이름 칸 + 선택 PPE 칸]` 으로 단순화했다.

---

## 7. 남은 위험 및 후속 과제

1. **명세 NFR-14(`11.4-5`·`14장`) 문구 정합성 확인 (Lead 리뷰 항목)**
   - 본 구현은 위치(`world_track_state`)·이벤트(`risk_event`)·작업(`work_unit`) 원본 테이블에 이름을 저장하지 않고 별도 표시 이름 이력 테이블(`V65`)에만 보관하며 외부 LLM 에도 전혀 보내지 않는다. 다만 `GET /api/health`·`/topic/health` 로 관제 화면(`VIEWER` 이상)에 표시 이름이 내려가므로, 명세 `11.4-5`·`14장` 예외 문구(기존 `report_signature.signer_name`, `watch_registrant`)에 관제 표시 이름 이력을 함께 명시할지 MR 단계에서 Lead 확인이 필요하다.
2. **마이그레이션 번호(`V65`) 및 계약 묶음 버전(`v7.43.0`) 충돌 점검**
   - 병렬 진행 중인 다른 티켓(`!603` V62 / `-657` `v7.40.0` 등)의 `dev` 머지 순서에 따라 MR 머지 직전 번호 충돌 여부를 Lead 가 최종 확인해야 한다.
3. **워치 미배정 작업자의 장기 이탈 시 당일 수동 이름 단절**
   - 워치가 없는 작업자에게 `(b)` 경로(`PUT /api/people/{worldId}/display-name`)로 붙인 이름은 Tier 0 가 `aliases` 병합을 내줄 때는 자동 승계되지만, 카메라 밖 장기 이탈로 완전히 새로운 `world_id` 가 부여되면 이어지지 않는다. 상세 패널 안내 문구에 `"워치를 착용하면 현장에서 이름이 계속 유지됩니다"` 를 명시해 `(a)` 워치 이름 경로를 기본 권장한다.
