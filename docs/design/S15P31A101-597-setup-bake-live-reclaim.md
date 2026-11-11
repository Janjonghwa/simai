# S15P31A101-597 작업 시작(LIVE) 때 현장 세팅 정식 굽기 회수

## 관련 명세

- 명세 3.2 5항(`docs/spec/03-system.md`): 작업 시작(LIVE)을 누르면 Tier 0 실시간 관제 가용성을 우선해 백그라운드 후처리 자원을 즉시 회수하고, 빠른 미리보기 맵(`preview_scene`)으로 먼저 관제를 시작한다.
- 부록 E 장면 1: 세팅 → 구역 → TBM → 시작을 1:30 안에 하고, 정밀 텍스처는 뒤에서 굽는다. 그래서 굽기 도중 작업 시작이 시연 기본 경로다.
- `docs/design/site-setup-pipeline.md` 잔여 위험 2: 굽는 동안 작업이 시작돼도 워커가 알 길이 없어 굽기를 계속한다. "서버가 하트비트 응답 등으로 LIVE 작업 중을 알려 주면 `live_gate` 에 잇는다(계약 변경 필요)". 이 티켓이 그 일이다.
- 발견 경위: 2026-11-11 전체 잔여 위험 점검 F03(P1).

## 바꾼 것

| 경계 | 위치 | 내용 |
|---|---|---|
| 계약 | `contracts/messages/job_lease_status.schema.json` 2.1.0 | ACTIVE 의 선택 `live_work_active`. ACTIVE 가 아니면 싣지 않는다 |
| 계약 | `contracts/messages/site_setup.schema.json` 1.7.0, `job_callback` 2.14.0 | result `scene_version` null 허용 + 선택 `bake_skipped`(`LIVE_WORK_STARTED`). null ↔ `bake_skipped` 를 스키마로 묶고, null 이면 `artifacts.scene` 금지 |
| 서버 | `JobLeaseService.heartbeat`, `LeaseStatus`, `JobMessages` | site_setup 대여의 ACTIVE 답에 `live_work_active`(그 작업 입력 `site_id` 에 IN_PROGRESS 작업이 있는가)와 `heartbeat_interval_ms` 5000 을 싣는다 |
| 서버 | `SetupService.applySnapshotCore`, `V50__setup_session_bake_skipped.sql` | `bake_skipped` 결과는 정식 Scene 없이 COMPLETED. `setup_session.bake_skipped` 에 남기고 `GET /api/setup-sessions/{id}` 에 싣는다. 이유 없는 null Scene 은 기존처럼 거부 |
| 워커 | `client.LeaseStatus`, `handlers.JobContext.live_work`, `runner._note_live_work` | 하트비트 답의 신호를 `ctx.live_work` 로 켠다. 취소(`ctx.cancelled`)와 다르다: 작업을 버리지 않는다 |
| 워커 | `site_setup.py` (h), `site_setup_stages._run_stage` | 굽기 전에 신호가 있으면 굽지 않는다. 굽는 중이면 `bake`·`fallback` 단계만 신호로 멈추고(자식 프로세스 종료는 기존 `ProcessStages`) `BakeReclaimed` → `bake_skipped` COMPLETED. 대여 취소가 함께 켜져 있으면 회수가 아니라 기존 CANCELLED 실패 |
| 프런트 | `features/setup/types.ts`, `setup.ts deriveSceneTierStatus` | `bake_skipped` 를 받고, COMPLETED 여도 "정밀 3D 맵"이 아니라 "미리보기 맵 · 작업 시작으로 정밀 텍스처 굽기를 건너뜀"으로 보인다 |

관제 화면 Scene 은 바꾸지 않는다. 미리보기는 READY_FOR_ZONES 때 이미 현재 Scene 이고(`SetupService.promoteSetupSceneAfterCommit`), 진행 중 작업이 있으면 정식 Scene 자동 교체도 원래 하지 않는다.

## 명세에 없어 정한 수치

| 값 | 근거 |
|---|---|
| site_setup 하트비트 주기 5 s | 회수 지연 = 하트비트 주기 + 자식 종료(ProcessStages 0.05 s 확인 주기). 기본 10 s 의 절반. 세팅 중에만 쓰여 요청 증가는 세팅 1회당 수십 건 |
| `bake_skipped` 값은 `LIVE_WORK_STARTED` 하나 | 이번에 생긴 이유는 하나뿐이다. 닫힌 enum 이라 이유가 늘면 마이너로 더한다 |

## 버린 대안

- **기존 취소 경로(`SetupService.cancel`, REJECTED/JOB_CANCELLED)**: 세션이 `FAILED + CANCELLED_BY_ADMIN` 이 된다. 관제 중인 작업의 세팅이 실패로 보이고, 실패면 작업 시작 버튼을 막는 화면 규칙과 충돌한다.
- **미리보기 버전을 `scene_version` 에 넣기**: 서버 계보 검사(`validateVersionLineage`)가 SCENE 종류가 아니라 거부한다. 같은 버전을 두 종류로 등록하면 버전 의미가 흐려진다.
- **Tier 0 구성 묶음(`work_id`)을 표시 파일로 워커에 알리기**: 지연은 짧지만 Tier 0 와 표시 파일 형식을 시연 직전에 건드린다. 서버가 이미 IN_PROGRESS 를 안다.
- **굽기를 멈추지 않고 스레드만 줄이기**: 명세는 "줄이거나 중단"을 허용하지만 texrecon 스레드 조절 경로가 없고, 끝까지 구워도 LIVE 중에는 정식 Scene 을 자동 교체하지 않으므로 얻는 것이 없다.

## 배포 순서

EC2 먼저, 워커 이미지 나중(`contracts/README.md` v7.29.0).
- 이전 EC2 + 새 워커: 신호가 오지 않아 지금처럼 끝까지 굽는다. `bake_skipped` 결과도 나오지 않는다.
- 새 EC2 + 이전 워커: 워커가 `live_work_active` 를 무시하고 지금처럼 굽는다. 주기 5 s 만 바뀐다.

## 시험

- 계약: `uv run contracts/scripts/validate_examples.py` — 예시 `job_lease_status.active_live_work`·`job_callback.site_setup_bake_skipped`, 반례 4개.
- 워커: `ai/tests/services/worker/test_site_setup.py`(굽기 전 신호 → 굽지 않음, 굽는 중 회수 → COMPLETED, 취소가 이김), `test_site_setup_stages.py`(bake 만 신호로 멈춤, 실제 `ProcessStages` 자식 종료), `test_runner.py`(하트비트 신호가 처리기에 닿고 취소가 아님), `test_client.py`.
- 서버: `SetupSessionIntegrationTest.굽는_중_작업이_시작되면_하트비트가_회수를_알리고_정식_Scene_없이_완료된다`(시험 DB 필요), `ContractExamplesRoundTripTest`.
- 프런트: `__tests__/features/setup/setup.test.ts`.

## 잔여 위험

1. 회수 지연은 최대 하트비트 주기(5 s) + 하트비트 실패 재시도만큼 늦다. "즉시"를 수치로 정한 명세는 없다.
2. 회수한 세팅은 정밀 텍스처 Scene 이 없다. 작업이 끝난 뒤 다시 굽는 경로(`scene_regenerate` 등)는 이 티켓 범위 밖이다.
3. 시연에서 정밀 텍스처를 보여 주려면 대본을 "굽기 완료 뒤 작업 시작"으로 바꿔야 한다(시연 담당 결정).
4. 서버는 `bake_skipped` 결과를 받을 때 진행 중 작업이 이미 끝났으면 경고 로그만 남기고 받는다(신호 뒤 바로 종료한 경우).
