# 관제 앱 R9 안전모 계약 연동 설계

> 후속 정책(2026-11-09, S15P31A101-556): Lead 시연 예외로 R9·R11 을 시드에 포함한다. 아래 기본 꺼짐·잔여 작업 설명은 이 문서 작성 시점의 기록이다. 현재 활성화·프런트 보완·복구 절차는 [`ppe-demo-activation.md`](ppe-demo-activation.md)를 따른다.

S15P31A101-520. 기획서 §7.1(확인 필요 이유 `PPE_UNKNOWN`), §7.2 R9 행과 설명(“PPE 요구 미수신”, 요구 해제 해소의 `config_version` 구분), §7.3, §8.10 이 기준이다.
선행: -519(`docs/design/risk-r9-helmet.md`, 계약 v7.17.0), -518(`docs/design/server-ppe-policy-publish.md` D3, `GET /api/config-bundles` 의 `ppe_policy` 요약).
사람별 안전모·조끼 상태 표시는 -531 범위다.

## 코드와 시험

| 부분 | 코드 | 시험 |
|---|---|---|
| 묶음 요약 받기 | `front/apps/admin/src/features/zones/types.ts`(`ppe_policy`), `zones/useRecentBundles.ts`(`readBundles`, `useRecentBundles`) | `__tests__/features/events/ppePolicy.test.ts` |
| 적용 묶음·미수신·해소 원인 | `features/events/ppePolicy.ts`(`appliedBundle`, `ppePolicyMissing`, `r9ReleaseOf`, `PERSON_ONLY_RULES`) | 같은 파일 |
| “PPE 요구 미수신” 안내 | `features/monitor/PpePolicyNotice.tsx`(관제 알림 자리, `layout/AppLayout.tsx`) | `__tests__/features/monitor/PpePolicyNotice.test.tsx` |
| 해소 원인 문구 | `events/format.ts`(`statusText`), `events/incidentView.ts`(`incidentTimeline`), `events/EventDetailPanels.tsx` | `__tests__/features/events/EventPanel.test.tsx` |
| `PPE_UNKNOWN` 문구·R9 이름 | `events/constants.ts` | `EventPanel.test.tsx`, `IncidentCard.test.tsx`, `ppePolicy.test.ts` |
| 작업 시작·종료 뒤 묶음 다시 받기 | `features/tbm/useWorkSession.ts` | (무효화만, 기존 시험) |

## 확인한 사실

- 묶음 스키마(zod `z.object`)는 모르는 칸을 지우고 통과시킨다. -518 머지로 묶음 화면이 깨지지 않는다.
- EC2 는 전이마다 `risk_event.config_version` 을 그 전이의 값으로 갱신한다(`server/.../tier0/event/RiskEventRepository.java` `APPLY`). 목록·`/topic/events` 가 같은 투영을 싣는다(`EventQueryRepository.java`). 해소된 R9 의 `config_version` 은 해소 전이의 것이라 계약·서버 변경 없이 구분할 수 있다.

## 명세에 없어 정한 것과 근거 (사용자 승인 2026-11-06)

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 적용 중 묶음 | 최근 묶음(새것부터 20개) 중 가장 새 APPLIED | 뒤 묶음이 대기·거부·불일치여도 Tier 0 는 앞서 적용한 묶음을 쓴다(3.2) |
| 미수신 표시 조건 | 적용 묶음의 `ppe_policy` 가 null 일 때만. 적용 묶음이 없거나 칸이 없으면(-518 이전 서버) 표시하지 않음 | 모르는 것을 지어내지 않는다 |
| 미수신 표시 자리 | 실시간 관제 알림 자리, 작업 세션 바 아래. 작업 여부와 무관 | 7.2 “관제 화면에” |
| 해소 원인 | 해소 묶음 NOT_REQUIRED → “작업 종료로 해소”, null → “PPE 요구 미수신으로 해소”, REQUIRED → “착용 확인으로 해소”, 못 찾음 → “해소” | 7.2: 열린 R9 는 착용 관측이나 요구 해제로만 해소 |
| 해소 원인 범위 | LIVE·RESOLVED·운영자 종료 아님. 상세 창(처리·규칙과 근거·타임라인)만, 카드 해소 칩은 그대로 | 묶음 목록은 해소된 LIVE R9 가 있을 때만 받는다 |
| R9 이름 | “작업 중 안전모 미착용” | 7.2 R9 행 |
| R9 구역 칸 | 상세 근거 줄에서 뺀다(“구역 없음” 대신) | R9 는 사람 단위, `zone_id` 없음(-519) |
| 요약 모양 오류 | 묶음은 받고 `ppe_policy` 만 모름(undefined) | 구역 발행 상태 표시를 막지 않는다 |
| 시험의 `zone.requirements {helmet:true}` | 그대로 둔다 | 11.1 `zone.requirements` 칸은 남아 있고 형식은 19.1 미결. 받은 값을 다시 보내는 시험이다 |

## 잔여 위험과 넘긴 일

- 해소 묶음이 최근 20개 밖이면 “해소”로만 보인다. `GET /api/config-bundles/{version}` 단건 조회로 메울 수 있다(후속).
- 작업은 시작됐는데 REQUIRED 묶음이 아직 적용 대기인 짧은 구간의 표시는 명세에 없다(미확정, 표시하지 않음).
- 카드 해소 칩에 해소 원인을 넣을지는 정하지 않았다.
