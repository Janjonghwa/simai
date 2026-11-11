# S15P31A101-598 같은 순번의 VLM 근거를 관제 상세에 반영

## 문제

- EC2 `VlmRationaleListener` 는 근거 저장 뒤 `risk_event.rationale` 투영만 바꾸고 `RiskEventsDelivered` 로 다시 발행한다. `event_sequence` 는 Tier 0 전이만 올리므로 그대로다.
- 관제 앱 `mergeEvent`(`front/apps/admin/src/features/events/entries.ts`)는 순번이 같으면 확인·상향·구간 요약 기록만 합치고 나머지는 버렸다. 그래서 이벤트 상세 "AI 설명"(`EventDetailPanels.tsx` `AiPanel`)이 새로고침 전까지 비어 있었다. 시연 장면 7(부록 E)이 이 문장을 보여 준다.

관련 명세: 5.4(VLM 빠른 경로·상향 의견·운영자 확인 상향), 12.3·12.6(`/topic/events`, `POST /api/events/{id}/escalation`), 3.2(Tier 0 가 등급·생명주기의 단일 소유자). 근거 설계: `docs/design/vlm-rationale.md` 6절(성공 제출 때 최신 근거 투영 갱신).

## 바꾼 규칙(프런트만)

`mergeEvent` 에서 순번이 **같으면**(둘 다 `event_sequence` 가 있고 같을 때만):

| 칸 | 규칙 |
|---|---|
| `severity`·`lifecycle`·`resume_count`·시각·`metrics` 등 Tier 0 전이 칸 | 지금처럼 이전 값 유지 |
| `rationale` | 새 값이 문자열이고 다르면 받음. `null` 로는 지우지 않음(서버가 근거를 비우는 경로가 없음) |
| `proposed_severity` | 새 값이 있으면(`null` 포함) 받음. 거절(`EventEscalationService`)·`NOT_ACTIVE`/`NOT_UPWARD`(`EventEscalateResultIngestor`)가 같은 순번으로 비우기 때문 |
| `assessment_id`·`assessmentId`·`assessment_job_id`·`delivery` | 새 값이 있으면 받음 |
| `assessments` | 새 값이 있고 내용이 다르면 받음(상세 응답만 싣는다) |
| `acks`·`escalations`·`summaries` | 기존 합치기 그대로 |

낮은 순번은 표시용 칸도 받지 않는다. 바뀐 칸이 없으면 같은 객체(`prev`)를 돌려줘 다시 그리지 않는다. 경보 단계 시작 시각(`stageStartedAt`)은 같은 순번에서 바뀌지 않는다.

## 서버 `proposed_severity = NULL` 점검(수정 안 함)

`VlmRationaleListener` 의 `UPDATE risk_event SET rationale = :text, proposed_severity = NULL` 은 이미 저장된 상향 의견을 지우지 않는다. 근거:

1. `risk_event.proposed_severity` 에 NULL 이 아닌 값을 쓰는 서버 코드가 없다. 이벤트 생성(`RiskEventRepository` `ENSURE_EVENT`·`ENSURE_FROM_SNAPSHOT`)은 이 칸을 넣지 않고, 전이 반영(`APPLY`)은 유지하거나 등급이 따라잡으면 비우기만 한다. 거절·`event_escalate_result` 처리도 비우기만 한다.
2. 빠른 경로 결과는 `matchesInput` 이 `proposed_severity` 가 `null` 인 결과만 받고, `vlm_assessment` 에도 `NULL` 로 저장한다. 명세 5.4 MVP 는 CRITICAL 이벤트에만 돌아 더 높은 등급이 없다.

즉 지금 이 UPDATE 는 "최신 평가의 상향 의견(항상 null)" 투영이며 `vlm-rationale.md` 6절과 맞다. 빠른 경로를 CRITICAL 아닌 이벤트로 넓혀 상향 의견을 받게 되면 이 줄을 결과 값으로 바꿔야 한다(잔여 위험 3).

## 버린 대안

- **서버가 근거 저장 때 `event_sequence` 를 올림**: 순번은 Tier 0 저널 전이의 정본이다(3.2, AGENTS.md 1·6). EC2 가 올리면 이후 Tier 0 전이(`APPLY` 의 `event_sequence < :seq`)가 막힌다.
- **별도 `display_version`·`updated_at` 비교**: 계약(`risk_event`) 변경이 필요하고 I1 LIVE 계약 승인 범위다. 2pt 범위를 넘는다.
- **같은 순번이면 통째로 새 값 사용**: 같은 순번에서 등급이 다른 값(재전송·늦은 목록 응답)이 오면 Tier 0 값을 덮을 수 있어 버렸다.

## 시험

- `front/apps/admin/src/__tests__/features/events/events.test.ts`: 같은 순번 → 표시용 칸만 반영·등급/생명주기 유지, 거절 시 `proposed_severity` 비움·근거 유지, 낮은 순번 무시.
- `front/apps/admin/src/__tests__/features/events/vlmRationaleMerge.test.tsx`: STOMP(`riskEvent`)로 같은 순번 + 근거가 오면 상세 "AI 설명"에 문장이 보이고 "규칙과 근거"의 등급은 그대로, 낮은 순번 근거는 무시.

## 잔여 위험

1. **늦게 도착한 이전 목록 응답**: 같은 순번에서 `proposed_severity` 는 `null` 도 받으므로, 상향 의견 발행 전에 시작한 REST 응답이 뒤늦게 오면 의견이 잠깐 사라질 수 있다(근거는 null 로 지우지 않아 해당 없음). 지금은 상향 의견을 만드는 경로가 없어 실제 영향은 없다. 표시 버전 칸이 생기면 해소된다.
2. **3D 저장소(`viewer3d/data/spatialData.store.ts` `compareEvent`)**: 같은 순번을 duplicate 로 버린다. 3D 는 `rationale`·`proposedSeverity` 를 그리지 않아 이번 결함과 무관해 고치지 않았다. 다만 `delivery`(AUDIT_ONLY 숨김)는 `RiskEventRepository.setDelivery` 가 순번 없이 바꿀 수 있어 같은 문제가 남는다(별도 확인 필요).
3. 빠른 경로가 상향 의견을 내게 되면 `VlmRationaleListener` 의 `proposed_severity = NULL` 을 결과 값으로 바꿔야 한다.
