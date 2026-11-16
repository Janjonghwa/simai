# S15P31A101-689 이전 맵 재사용을 늘 고르게 하고, 카메라가 달라졌으면 끄고 멈춘다

관련 명세: 4.3(현장 세팅 자동 보정), 4.4(틀어짐 감시·재세팅 요구), 4.5(보정·Scene 계보), 8.1·13.4(설치·보정 화면), 12.5·12.6(I5·세팅 REST).
계약 `messages/site_setup` 1.18.0, `job_heartbeat` 2.17.0, `job_callback` 2.24.0(묶음 v7.51.0). DB 변경 없음.
연관 설계: `S15P31A101-614-setup-reuse-mode.md`(세팅 모드, 3절 "거부되면 새 모델로 이어 간다"를 이 티켓이 뒤집는다), `world-reanchor.md`(-584),
`camera-fall-drift-detection.md`(-540 회전 감시), `S15P31A101-686-fresh-setup-background.md`(배경 촬영 창), `S15P31A101-690-height-check-in-input.md`(높이 확인 창).

## 문제(15차, 2026-11-13, 코드 확인)

1. 「이전 모델 / 새 모델」 창(`ModelPickerDialog`)은 그 작업에 세팅 세션이 하나도 없을 때만 떴다. 한 번 실패·취소한 작업은
   `work_unit.active_setup_session_id` 가 남아 다시 안 뜨고, 재시도(`POST .../retry`)는 처음 방식을 그대로 복사했다.
2. REUSE 에서 World 다시 맞추기가 거부되면(카메라가 움직임) 워커가 같은 작업 안에서 조용히 NEW(맵 약 13분)로 넘어갔다(-614 3절).
3. 재사용 가능 판정은 현재 Scene 의 COMMITTED 여부뿐이라 카메라 이동을 미리 보지 않았다(`useReuseScene`).
4. 새로고침하면 세팅 화면 단계가 클라이언트 상태라 사라졌고, 다른 탭에서 새로 시작한 시도를 따라가지 않았다.

사용자 원칙(11-13~14): 이전 맵 재사용이 기본이고 세션 유무와 무관하게 늘 고른다. 카메라가 달라졌으면 재사용을 끄고 이유를 보인다.
자동 NEW 전환 금지(사용자가 고른다). 화면은 필요한 정보만, 실패는 "무엇이 / 어떻게" 두 줄.

> 갱신(S15P31A101-762, 사용자 결정 2026-11-16): 카메라 각도가 바뀌어 있을 위험이 높아 기본을 **새 맵 만들기**로 바꿨다. 이전 맵 재사용은
> 관리자가 고를 때만 쓰고(시점 확인 -712·차단 사유는 그대로), 진행 중 세션의 `requested_mode` 복원은 유지한다. 라디오 순서는
> `새 맵 만들기(기본 · 약 13분)` / `이전 맵 재사용(약 1~2분)`. 서버 REST 기본(생략하면 NEW)은 원래 NEW 라 바꾸지 않았다.
> 아래 본문의 "재사용이 기본"은 -689 당시 기준이다.

## 바꾼 동작

### 1. 1단계 안의 「맵」 선택(모달 대신)

- 1단계 오른쪽 칸 맨 위 카드 「맵」: `이전 맵 재사용(기본 · 약 1~2분)` / `새 맵 만들기(약 13분)` 라디오 둘(`SetupModeChoice`).
- 기본은 지금 세션의 요청 방식, 세션이 없으면 재사용이다. 재사용을 쓸 수 없으면 꺼지고 그 칸 안에 두 줄(무엇이 / 어떻게)을 보이며
  선택은 새 맵으로 보인다. 사용자는 그 상태를 보고 「다음」을 누른다(세팅 진행 중에 방식이 바뀌는 일은 없다).
- 모달을 버린 근거: (a) 늘 고를 수 있어야 하므로 "세션이 없을 때만 한 번" 뜨는 창의 조건 자체가 문제였다. (b) 사람이 움직일 일(배경 촬영·
  높이 실측)은 모달(-686·-690)이고, 방식 고르기는 높이 입력과 같은 자리의 입력값이다. (c) 실패·취소 뒤, 새로고침 뒤에도 같은 자리에서 보인다.
  (d) 이유(카메라 n 이 달라짐)를 고르는 순간 옆에서 볼 수 있다.
- 실패·취소한 시도가 있으면 「다음」이 그 시도의 다시 시도(`POST .../retry`)로 이어 고른 방식을 `mode` 로 보낸다(새 세션 `retry_of` 계보 유지).

### 2. 재사용 가능 판정에 카메라 변화(`GET /api/setup-sessions/reuse-check`, ADMIN)

서버 `SetupReuseCheck` 가 세팅 시작(mode=REUSE)과 같은 판단으로 돌려준다:
`{available, blocked_reason: NO_SCENE|SCENE_MISMATCH|CAMERA_CHANGED|null, scene_version, calibration_version,
camera_status: CHANGED|NOT_DETECTED|UNKNOWN, changed_cameras[], camera_source: TIER0_DRIFT|LAST_SETUP_REJECTED|null}`.

| 순서 | 근거 | 결과 |
|---|---|---|
| 1 | 현재 관제 맵(COMMITTED)과 보정 행 없음 | NO_SCENE(기존 409 SETUP_REUSE_SCENE_MISSING) |
| 2 | Scene 정합 MISMATCH(기존 -614 가드) | SCENE_MISMATCH |
| 3 | Tier 0 배경 회전 감시(-540)가 지금 재세팅을 요구하는 카메라(`viewer_health.re_setup` 과 같은 값, Tier 0 연결 중일 때만) | CAMERA_CHANGED, TIER0_DRIFT |
| 4 | 이 현장의 가장 최근 세팅 시도가 재사용 거부(`REUSE_WORLD_REJECTED`, 카메라 이동 사유)로 멈춤 | CAMERA_CHANGED, LAST_SETUP_REJECTED |
| 5 | Tier 0 연결 중이고 re_setup 이 빈 배열 | NOT_DETECTED(막지 않음) |
| 6 | 근거 없음(Tier 0 끊김·re_setup 없는 옛 Tier 0) | UNKNOWN(막지 않음, 화면에 "카메라가 그대로인지는 시작한 뒤 확인해요") |

- 신호를 이 둘로 고른 이유: 새 측정 없이 이미 있는 "직전 보정 대비" 정보다. -540 은 Tier 0 활성 보정의 배경과 지금 화면을 사람 없이 비교하고
  (순수 회전·위치 이동·큰 틀어짐 → DRIFTED, 2회 연속 확인), 워커 World 다시 맞추기(-584)의 기준도 같은 Tier 0 활성 보정이다. 직전 재사용 거부는
  같은 비교의 결과이므로 그 뒤 새 세팅이 없으면 다시 해도 같다.
- 서버 `create`(retry 포함)도 같은 판정으로 409 `SETUP_REUSE_CAMERA_CHANGED` 를 낸다(화면 우회 방어).
- 화면은 1단계에 있는 동안 10 s 마다 다시 받는다(카메라를 그 사이 건드리면 선택이 바뀐다).

### 3. 워커가 거부하면 새 맵으로 넘어가지 않고 멈춘다

- `SiteSetupHandler._reject_reuse`: REUSE_SCENE 요청이 거부(또는 기록 없음 NO_REFERENCE)되면 `mode: REUSE_SCENE` 그대로
  `world_anchor{accepted:false, reason, changed_cameras}` 를 실은 JOINT_BA_AND_VERIFY 스냅샷(진행률 62)을 보내고, 새 보정 문서를 **등록하지 않은 채**
  `JobFailed("REUSE_WORLD_REJECTED", retryable=False)` 로 끝낸다. Tier 0 는 지금 보정을 그대로 쓰고 카메라 확인(-646)도 시작하지 않는다.
- `changed_cameras` = 보정 문서 world_anchor 의 카메라별 `center_residuals_m`·`orientation_changes_rad` 가 문서 `limits` 를 넘은 카메라(오름차순).
  축척 차이(SCALE_OUT_OF_RANGE)처럼 카메라를 짚을 수 없으면 빈 배열(지어내지 않는다, AGENTS 규칙 2).
- 서버는 이 스냅샷을 받아 세션 `mode=REUSE` 그대로 `world_anchor` 를 두고(`SetupReuseMode.read`: READY_FOR_ZONES 전 REUSE_SCENE 의 거부 요약 허용),
  실패 콜백으로 `failure_code=REUSE_WORLD_REJECTED`.
- 화면 실패 안내(두 줄): "카메라 3의 위치나 방향이 직전 세팅과 달라졌어요" / "새 맵으로 다시 하세요." + 「새 맵으로 다시 하기」(retry mode=NEW)·
  「카메라 연결 관리로」. 「다시 시도」(같은 방식)는 두지 않는다(같은 결과). 다음 1단계에서도 재사용은 4번 근거로 꺼져 있다.
- 옛 워커(1.17.0 이하)의 새 모델 이어 가기 스냅샷(`reuse_fallback_reason`)은 호환으로 계속 받는다(새 워커는 보내지 않는다).

### 4. 방식 변경 저장, 「현재 시도 중단」과 「세팅 취소」

- `POST /api/setup-sessions/{id}/retry` 선택 `mode`(NEW | REUSE, 그 밖 400). 없으면 처음 요청한 방식 그대로(기존 호출 호환). 새 시도의 방식은
  새 세션의 `requested_mode` 로 저장된다(기존 상태 모델: 시도 = 세션, retry_of 계보, work_unit.active_setup_session_id 갱신). DB 변경 없음.
- 2단계 「현재 시도 중단」(옛 「세팅 취소」 자리): 확인 뒤 `POST .../cancel`(FAILED·CANCELLED_BY_ADMIN) → 1단계로 돌아가 높이·방식을 다시 고른다.
  세팅에서 나가지 않는다.
- 1단계 「세팅 취소」: 진행 중인 시도가 있으면 확인 뒤 멈추고, 세팅에서 나간다(날짜 화면은 보고서로 `onExit`, 단독 화면은 그 작업의 작업 관리로).
  예전 방식 창을 닫을 때의 "나가기"를 이 단추가 맡는다.

### 5. 새로고침·다른 탭 복원

- 화면은 작업의 서버 세션(`GET /api/work-units/{id}` 의 `active_setup_session_id`)을 5 s 마다 다시 본다. 다르면 그 세션으로 옮긴다(`followServerSetupSession`):
  처음 열 때(새로고침), 따르던 세션이 끝났거나(FAILED·COMPLETED) 다른 작업의 것일 때. 따르던 세션이 진행 중이면 옮기지 않는다(같은 현장에
  진행 중 세션은 하나라 서버 값이 늦게 온 것). 이 화면이 새로 시작·다시 시도하며 떠난 세션으로는 되돌아가지 않는다(`supersededRef`).
- 옮긴 세션은 서버 상태로 단계를 정한다: 기본 2단계(대기 화면). 세팅 완료(`setup_completed_at_ms`)했거나 세션 구역 목록을 저장했으면(revision > 0,
  구역 단계에서만 생긴다) 3단계(구역). 사용자 대기는 이미 서버 세션에서 오는 값이라 2단계에 들어서면 다시 열린다: 배경 수집 중이면 배경 촬영 창(-686),
  `paused_reason: HEIGHT_CHECK` 면 높이 확인 창(-690), 실패면 실패 안내. 다른 탭에서 높이 확인에 답하거나 시도를 멈추면 세션 조회(2 s)로 창이 닫힌다.
- 선택 방식은 고르지 않았으면 그 세션의 `requested_mode` 를 따른다. 1단계에서 고르고 시작하기 전의 선택(세션 없음)은 저장하지 않는다(기본값 재사용으로 돌아온다).

## 정한 수치와 근거

| 값 | 위치 | 근거 |
|---|---|---|
| 작업 서버 세션 다시 보기 5 s | `SetupPage` `WORK_SESSION_POLL_MS` | 다른 탭 변화는 사람 손으로 생겨 수 초면 충분하다. 세션 조회(2 s)보다 길게 해 요청 수를 늘리지 않는다(작업 하나 GET). |
| 재사용 판정 다시 받기 10 s(1단계에서만) | `useReuseScene` `REUSE_CHECK_POLL_MS` | -540 확정이 2회 연속 확인(배경 표본 주기 5 s)이라 그보다 자주 볼 이유가 없다. |
| 거부 스냅샷 진행률 62 | `_reject_reuse` | (f) 구간 61~63 안, 직전 61 보다 크다(같은 대여 단조 증가). |
| 「약 1~2분」·「약 13분」 | `SetupModeChoice` | 사용자 기준 재사용 시간, 15차 NEW 실측. 표시용. |

## 계약·배포

- `site_setup` 1.18.0: `world_anchor_summary.changed_cameras`(선택, 채택이면 금지), `reuse_rules` 의 "REUSE_SCENE world_anchor 는 채택" 검사를
  READY_FOR_ZONES 이후로 옮김. `job_heartbeat` 2.17.0, `job_callback` 2.24.0(설명·판). 실패 코드는 `job_callback.error.code` 자유 문자열이라 enum 변경 없음.
- REST(계약 밖): `GET /api/setup-sessions/reuse-check`, retry `mode`, 409 `SETUP_REUSE_CAMERA_CHANGED`.
- 배포 순서: EC2·화면 먼저, 워커 이미지 나중. 옛 EC2 는 REUSE_SCENE + 거부 요약 스냅샷을 400 으로 거부한다(워커는 작업을 버린다 → 세션은
  대여 만료로 실패). 옛 워커 + 새 EC2 는 예전처럼 새 모델로 이어 간다(호환).

## 버린 대안

- **모달을 조건만 넓혀 늘 띄우기**: 실패 뒤·새로고침 뒤에도 매번 창을 닫아야 하고, 이유를 높이 입력과 함께 볼 수 없다.
- **거부 시 같은 작업에서 새 맵으로 이어 가기(-614 3절)**: 사용자 결정으로 금지. 배경·Stage A 몇 분을 다시 쓰지만, 13분짜리 맵 굽기를 모르는 사이
  시작하지 않는 쪽을 택했다.
- **거부 시 보정 문서는 등록하고 실패**: 쓰지 않는 보정 버전이 늘고, 재사용 실패인데 Tier 0 묶음 경로가 이 보정을 볼 여지가 생긴다. 등록하지 않는다.
- **서버가 세팅 직전에 카메라 프레임을 이전 배경과 다시 비교(특징점·호모그래피)**: 새 측정 경로·GPU/CPU 시간·임계값이 필요하다. -540 이 이미 같은
  비교를 상시로 하고 있어 그 결과(re_setup)를 쓴다.
- **화면 단계를 서버에 새 열로 저장(`ui_stage`)**: 화면 전용 상태를 DB 계약에 넣게 된다. 단계는 서버 세션 상태(진행·완료·구역 목록)로 충분히 정해진다.
- **카메라 근거가 없으면 재사용을 막기**: Tier 0 가 꺼진 현장(시연 전 세팅)에서 재사용을 늘 못 쓴다. 원칙대로 「알 수 없음」으로 두고 워커가 확인한다.
- **retry 대신 새 세션 생성(POST /api/setup-sessions)만 허용**: retry_of 계보가 끊긴다. retry 가 mode 를 받게 했다(기존 호출은 그대로).

## 잔여 위험

- -540 은 Tier 0 가 돌고 배경 표본이 쌓인 뒤에만 판정한다. 세팅 직전에 카메라를 돌렸고 Tier 0 확인 전이면 UNKNOWN 이라 재사용을 고를 수 있고,
  배경 수집·초기 복원·Stage A(수 분) 뒤 워커가 거부해 멈춘다(새 맵으로 넘어가지는 않는다). 빠른 사전 비교는 -614 잔여 위험의 후속 후보 그대로다.
- -540 오탐(13·14차 cam_A·4대 오탐)이면 재사용이 꺼진다. 「새 맵 만들기」는 늘 고를 수 있고, 오탐 해소는 -540 쪽 일이다.
- 직전 재사용 거부(LAST_SETUP_REJECTED)는 카메라를 원래대로 되돌려도 새 세팅 전까지 재사용을 막는다(근거가 "직전 시도"라서). 새 맵 세팅이 끝나면 풀린다.
- 1단계에서 고르고 시작하기 전 새로고침하면 선택이 기본값(재사용)으로 돌아온다.
- 거부로 멈춘 시도도 배경 수집~Stage A 시간(수 분)은 쓴다.
- 다른 탭이 이 탭이 따르는 진행 중 세션을 취소하고 곧바로 새 시도를 만들면, 이 탭은 취소(FAILED)를 본 뒤(세션 조회 2 s) 다음 작업 조회(5 s)에서 옮긴다.

## 넘긴 일

- 16차 GPU 세션: 카메라 한 대를 돌린 뒤 재사용을 골라 (a) -540 이 잡으면 1단계에서 꺼지는지, (b) 못 잡으면 워커가 REUSE_WORLD_REJECTED 로 멈추고
  "카메라 n" 이 맞게 나오는지, 「새 맵으로 다시 하기」가 새 세션으로 이어지는지. 두 탭으로 시작·중단·높이 확인 답하기.
- 카메라 확인 위치(-687)·화면 전반 정리(-691)·NEW 완료 조건(-688)은 범위 밖. -691 에서 단추 배치(「세팅 취소」·「현재 시도 중단」) 정리 가능.
- 명세 정리 MR: 4.3·8.1(1단계 맵 방식 선택, 재사용 거부는 멈춤·자동 전환 없음), 12.6(reuse-check, retry mode, 새 오류 코드), -614 설계 3절 갱신.

## 코드·시험 위치

- 계약: `contracts/messages/site_setup.schema.json`(1.18.0), `job_heartbeat`·`job_callback` 설명, `contracts/examples/job_heartbeat.site_setup_reuse_rejected.json`,
  `contracts/examples/invalid/job_heartbeat.site_setup_reuse_ready_rejected_anchor.json`·`..._anchor_accepted_with_changed_cameras.json`,
  `ai/src/simai_ai/contracts/models.py`(생성).
- 워커: `ai/services/worker/site_setup.py`(`world_anchor_summary`·`_changed_cameras`·`_reject_reuse`). 시험 `ai/tests/services/worker/test_site_setup.py`
  (거부 멈춤·기록 없음 멈춤·changed_cameras 고르기).
- 서버: `SetupReuseCheck`(판정), `SetupService`(`retry(mode)`·`reuseCheck`·`requireReusableScene`), `SetupController`(`GET /reuse-check`, retry `mode`),
  `SetupReuseMode`(`read(readyOrLater)`·`WORLD_REJECTED_FAILURE`), `ViewerHealthService.reSetupCameraIds`, `ErrorCode.Setup.SETUP_REUSE_CAMERA_CHANGED`.
  시험 `SetupReuseModeIntegrationTest`(거부 멈춤 → 다음 선택 차단 → 새 맵 재시도, Tier 0 회전 감시 차단·모름), `SetupReuseModeTest`.
- 화면: `front/apps/admin/src/features/setup/SetupModeChoice.tsx`(신규), `useReuseScene.ts`(`useReuseChoice`·`reuseChoiceOf`), `SetupPage.tsx`
  (방식·다시 시도 mode·세팅 취소·현재 시도 중단·서버 세션 따라가기·구역 단계 복원), `setup.store.ts`(`followServerSetupSession`), `setupActions.ts`,
  `setupFailures.ts`(REUSE_WORLD_REJECTED), `SetupCalibrateStep.tsx`(「새 맵으로 다시 하기」), `api.ts`·`types.ts`, 목업 `mocks/setup.ts`.
  `ModelPickerDialog.tsx` 삭제. 시험 `SetupPage.test.tsx`·`SetupPageEmbedded.test.tsx`·`setupActions.test.ts`·`setupFailures.test.ts`·`reuseChoice.test.ts`.
