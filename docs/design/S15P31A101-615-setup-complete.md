# 세팅 완료 C17 (S15P31A101-615)

작업 관리 새 흐름의 서버 쪽 C17 이다. 관련 명세: 3.2(구성 묶음·작업 시작 전 세팅), 4.5(Scene 계보), 8.1~8.2(세팅·구역), 12.6(세팅 REST).
앞선 이슈와의 약속: -612 `SetupZoneApplier`(docs/design/setup-session-zones.md), -614 세팅 모드(docs/design/S15P31A101-614-setup-reuse-mode.md 10절),
-610 `requireToday`(docs/design/daily-work-units.md), -616 `TbmFormAiService.tryStartDraftForWork`(docs/design/S15P31A101-616-tbm-form-draft.md).

## 만든 것

| 항목 | 위치 |
|---|---|
| `POST /api/setup-sessions/{id}/complete` (ADMIN, 200) | `setup/SetupController.complete` |
| 완료 트랜잭션 | `setup/SetupCompletionService` |
| 부르는 쪽 트랜잭션 안에서 묶음 만들기(전송은 커밋 뒤) | `config/ConfigBundleService.publishInTransaction` |
| READY_FOR_ZONES 자동 묶음 발행·미리보기 현재화 제거, 완료 뒤 도착한 정식 맵·다시 굽기 맵만 올림 | `setup/SetupService.onProgress`·`onFinished` |
| 완료한 작업의 새 세션·재시도 거부 | `SetupService.checkWorkUnitCanSetup` |
| 완료 뒤 보강 촬영·다시 굽기 거부(`rebake_blocked_reason: SETUP_COMPLETED`) | `setup/SetupSupplementService.uploadBlockedReason` |
| 세션 조회에 `setup_completed_at_ms` | `SetupService.getInSite` |
| 시험 | `SetupCompleteIntegrationTest`(새), `SetupResultRegisterIntegrationTest`·`SetupSupplementIntegrationTest`·`SetupReuseModeIntegrationTest`(완료 뒤로 옮긴 기대값) |

DB 마이그레이션은 없다(`work_unit.setup_completed_at` 은 V54). 계약 변경도 없다(세션 조회는 계약 스키마가 없는 응답).

## 완료 트랜잭션 순서

1. 잠금: 레이아웃 구역 잠금 → 현장 행 → 세션 행 → 작업 행(작업 시작 `WorkSessionService.start` 와 같은 순서라 교착하지 않는다).
2. 검사(아래 표).
3. `SetupZoneApplier.apply(sessionId, sameWorld, actor)`. `sameWorld = mode == REUSE || world_anchor.accepted == true`. 적용 서비스의 409(`SETUP_ZONE_WORLD_MISMATCH`·`SETUP_ZONE_STALE`·`SETUP_ZONES_CLOSED`)는 그대로 낸다.
4. `work_unit.setup_completed_at` 기록.
5. Scene: 새 모델(NEW)은 이 세션의 최종 맵을 켠다(`setup_session.scene_version` = 정식 맵 또는 다시 굽기 APPLIED 가 옮긴 맵, 없으면 미리보기). 이 세션이 등록한 Scene 만, 활성화 이력 actor `setup:<id>`. 재사용(REUSE)은 지금 관제 맵을 둔다.
6. 구성 묶음 한 번: 이 세션이 등록한 보정 문서를 싣는다(등록한 보정이 없으면 지금 보정). 묶음을 못 만들면 모두 되돌린다(구역만 바뀌고 Tier 0 에 안 나가는 상태를 만들지 않는다).
7. 커밋 뒤 다른 스레드에서 `tryStartDraftForWork(workId)`. 기다리지 않고, 실패는 로그만.

응답: 세션 조회와 같은 모양 + `applied_zones{created,updated,kept,archived}`(zone_id 목록).

## 거부 코드(검사 순서)

| 조건 | 코드 |
|---|---|
| 세션 없음 | 404 `SETUP_SESSION_NOT_FOUND` |
| 이 작업은 이미 완료 | 409 `SETUP_ALREADY_COMPLETED` |
| READY_FOR_ZONES 전·실패 | 409 `SETUP_NOT_READY_TO_COMPLETE` |
| 작업에 지금 걸린 세션 아님 | 409 `SETUP_SESSION_NOT_ACTIVE` |
| 날짜 작업인데 오늘 아님 | 409 `WORK_NOT_TODAY` |
| 작업 PLANNED 아님 | 409 `WORK_INVALID_STATE` |
| 같은 현장 관제 중(IN_PROGRESS) | 409 `SETUP_COMPLETE_BLOCKED_BY_LIVE_WORK` |
| 완료 뒤 새 세션·재시도 | 409 `SETUP_ALREADY_COMPLETED` |
| 완료 뒤 보강 촬영·다시 굽기 | 409 `SUPPLEMENT_SETUP_COMPLETED` |

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 완료 판정 | 세션이 작업의 `active_setup_session_id` 이고 그 작업의 `setup_completed_at` 이 있음 | 새 칼럼 없이 -610·-612 와 같은 근거를 쓴다 |
| 굽기 도중 완료 | 허용. 미리보기를 켜고, 뒤에 온 정식 맵은 지금 현재 Scene 이 이 세션 미리보기일 때만 교체 | 미리보기로 관제를 시작하는 명세 3.2 5항과 같은 규칙 |
| 다시 굽기 도중 완료 | 허용. 완료 뒤 도착한 APPLIED 는 현재 Scene 이 기준 맵일 때만 교체 | 요청은 완료 전에 받았다. 요청 단계에서만 막는다 |
| 완료 전 도착한 정식 맵·다시 굽기 맵 | 현재 Scene 으로 올리지 않고 세션 맵만 바꾼다. 완료가 그 맵을 켠다 | 완료 전에는 관제 맵이 바뀌지 않는다(-613) |
| 묶음 실패 | 완료 전체를 되돌린다 | 구역 적용과 발행을 한 트랜잭션으로 묶은 -612 약속 |
| 보강 촬영 업로드 | 완료 뒤 막는다 | 다시 굽기를 못 하는 파일을 받지 않는다 |

## 버린 대안

- 묶음을 커밋 뒤 새 트랜잭션으로 발행(`publishForSetupAfterCommit`): 묶음이 실패해도 구역은 바뀐 채 남는다. 같은 트랜잭션 요구(곽나혁)와 맞지 않는다.
- TBM 초안을 `afterCommit` 안에서 바로 부르기: 끝난 트랜잭션의 연결에 끼어 초안 상태가 커밋되지 않을 수 있다. 다른 스레드에서 부른다.

## 프론트(같은 MR)

| 항목 | 위치 |
|---|---|
| 구역 단계 오른쪽 칸 맨 위 「세팅 완료」 카드(주 단추). 구역 목록이 한 번도 저장되지 않았으면(revision 0) 보관 경고를 확인 대화로 받는다. 성공하면 세션을 다시 읽어 완료 시각·적용 개수를 보이고 단추를 없앤다 | `features/setup/SetupCompleteCard.tsx` |
| 완료 뒤 구역 편집(ZonesPage)·AI 초안 목록·초안 윤곽을 감춘다. 마지막 단추(다음·작업 관리로)는 완료로도 켜진다 | `features/setup/SetupPage.tsx` |
| 거부 코드별 안내, 보강 사유 `SETUP_COMPLETED`·`SUPPLEMENT_SETUP_COMPLETED` | `setupErrorMessage.ts`, `supplement.ts` |
| 세팅 화면 3D(미리보기·구역 단계)는 완료 전 관제 Scene 이 바뀌지 않으므로 이 세션 맵(정밀 맵, 없으면 미리보기)을 `GET /api/scenes/{v}/view`(-613)로 받아 그린다. 재사용 모드는 맵이 없어 지금 관제 맵. 구현은 -618 의 것(`layout/useSceneView`·`useSceneQuery`, `setup.sessionSceneVersion`)을 그대로 쓴다 | `SetupPage`(SetupZoneStep sceneVersion) |
| 목업: `POST .../complete`, `GET·PUT .../zones`(세션별 목록·revision). Scene 보기 목업은 -618 의 것. 작업 목업의 SETUP_DONE 은 `setup_completed_at_ms` 기준(서버와 같음) | `mocks/setup.ts`, `mocks/work.ts`, `mocks/setupSupplement.ts` |
| 세팅 구역 단계의 구역 편집·AI 초안 수락은 관제 구역(`zones/commit`)이 아니라 세션 구역 목록(-612 `GET·PUT .../zones`, revision)에 저장한다. 화면 구역 목록·3D·작업면도 그 목록 기준(이어 쓰는 구역은 지금 관제 구역에 본문을 덮고, 새 구역은 임시 ID `setup-new-<순번>`). 세팅 밖 관제 구역 편집은 그대로 `zones/commit` | `zones/zoneSource.ts`(ZoneSourceContext), `setup/setupZonePlan.ts`, `useZones`·`useStaticObjectsQuery`·`useZoneCommitMutation`, `useAcceptZoneDraft`(AI 항목·candidate_id) |
| 목록 시작: 재사용은 빈 목록을 지금 관제 구역(zone_id 유지)으로 한 번 채운다. 새 모델은 빈 목록, 같은 World(world_anchor.accepted)면 「지금 구역 불러오기」, 아니면 불러오기를 끄고 새로 그리라고 안내 | `setup/SetupZonePlanCard.tsx` |
| 저장 충돌 `SETUP_ZONE_PLAN_CONFLICT`·닫힘 `SETUP_ZONES_CLOSED` 은 안내하고 목록(닫힘이면 세션도)을 다시 받는다. 완료의 `SETUP_ZONE_STALE` 도 다시 받는다. 완료 확인 대화는 목록이 실제로 비었을 때만 | `zones/commitErrors.ts`, `SetupCompleteCard.tsx` |
| 시험 | `__tests__/features/setup/SetupCompleteCard.test.tsx`, `__tests__/features/setup/setupZonePlan.test.tsx`, `__tests__/features/setup/SetupPage.test.tsx`, `__tests__/mocks/setupWorkFlow.test.ts` |

## 잔여 위험·넘긴 일

- 목록은 '기존 구역의 작업면만 지우기'를 나타내지 못한다(-612 남은 일). 세팅 구역 단계에서 그 저장은 안내만 하고 보내지 않는다.
- 새 구역의 임시 ID 는 목록 순번이라, 앞 항목을 빼면 뒤 항목 ID 가 바뀐다(선택이 풀릴 수 있다).
- 개발 목업의 완료는 적용 개수만 돌려주고 관제 구역 목업을 바꾸지 않는다.
- 작업 시작(`startSession`)은 아직 `setup_completed_at` 을 보지 않는다. 완료 없이 작업을 시작하면 이전 보정·구역·맵으로 관제하고, -625 가 대기 중 굽기를 회수해 세션이 COMPLETED 가 되며 작업이 PLANNED 가 아니라 그 뒤 완료는 409 `WORK_INVALID_STATE` 다(후속 결정 필요).
- 회수 세션(-597, 작업 시작 = 완료 뒤)의 다시 굽기는 요청 단계에서 막힌다(-621 설계 4항과 같음).
- 명세 12.6 세팅 REST 표에 `POST /api/setup-sessions/{id}/complete` 와 새 오류 코드를 더한다(명세 정리 MR).
