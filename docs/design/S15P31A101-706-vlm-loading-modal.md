# S15P31A101-706 VLM 가중치 받는 중 안내(세팅 구역 단계 창·사건 상세)

관련 명세: 3.2(Tier 0 우선, VLM 은 보조), 5.3·8.2(AI 구역 초안), 5.4(VLM 근거). 앞 티켓: -700(부팅 단계 WEIGHTS → ENGINE → TIER0 → VLLM → READY),
-702(`GET /api/gpu/sessions/current`, `features/gpu`), -691·-687(세팅 화면: 사람이 기다려야 하는 것은 큰 창, 화면은 최소).
사용자 결정 2026-11-14: VLM 가중치(31.5GB)는 부팅 때 뒤에서 받고, 상단 GPU 점은 Tier 0 연결만 본다. 구역을 만들 때까지 VLM 이 준비되지 않았으면 그때 창으로 알린다.
front 만 바꿨다. 서버·ai·계약 변경 없음(`stage_detail` 은 서버가 이미 싣던 칸을 front zod 에 더했다).

## 판정 규칙(`features/gpu/gpuStatus.ts` 의 `vlmReadiness`)

| 상태 | 조건 | 화면 |
|---|---|---|
| LOADING | 열린 세션(`state=ACTIVE`)의 `stage=VLLM` | 구역 단계 「AI 모델(VLM) 준비 중입니다」 창, 사건 상세 「AI 근거 준비 중」 |
| TIMED_OUT | `stage=READY` 이고 `stage_detail` 에 「vLLM 미준비」 포함(부팅 `lobby_stage` 의 「vLLM 미준비(시한 초과): …」) | 구역 단계 「AI 초안 없이 직접 구역을 만드세요」 창 |
| READY | 그 밖의 READY | 없음 |
| UNKNOWN | 세션 없음·끝남(손으로 켠 GPU, 환경 변수 토큰), 관리자 아님(상태 API 403, 훅이 부르지 않음), 그 밖의 단계 | 지금 동작 그대로 |

- 구역 단계 창(`features/setup/VlmZoneNotice.tsx`)은 구역 단계이고, 이 브라우저가 고치는 세팅 세션이며(`zoneSource`), 완료 전일 때만 뜬다.
  「직접 구역 만들기」(또는 Esc)로 닫으면 그 세팅 세션에서는 다시 띄우지 않는다(손으로 그리는 중에 다시 막지 않는다).
  LOADING 창은 READY 가 되면 저절로 닫히고(10초 주기 상태 조회) 그 세션의 초안 조회(`setupZoneDraftsKey`)를 다시 받는다. TIMED_OUT 으로 바뀌면 시한 초과 창으로 바뀐다.
  진행 정보는 지금 응답에 있는 것만 보인다: `stage_detail`(「VLM 가중치·vLLM 기동 대기」)과 GPU 를 켠 뒤 경과(`gpuSummary`). `stage_at_ms` 는 마지막 보고 시각이라
  (보고기가 주기마다 다시 보낸다) 「이 단계 경과」로 쓰지 않았고, `stage_progress` 는 VLLM 단계에서 보고되지 않는다.
- 사건 상세(`features/events/EventDetailPanels.tsx` `AiPanel`)는 LIVE·CRITICAL(서버가 근거를 요청하는 유일한 조합, `VlmRationaleIngestor`)이고 근거가 없을 때
  LOADING 이면 빈 칸(—) 자리에 「AI 근거 준비 중」을 보인다. GPU 상태는 새로 요청하지 않고 상단바 점이 받아 둔 캐시만 읽는다(`useCachedGpuStatus`).

## 초안 시작 경로 조사

- 초안 작업(`vlm_zone_draft`)은 화면이 시작하지 않는다. 서버 `SetupService.enqueueZoneDraftIfReady` 가 미리보기 Scene 이 등록되면(워커 하트비트·제출 안)
  미리보기 판마다 한 번 넣는다(`setup_zone_draft_run` 에 판당 한 줄, 다시 넣는 API 없음). 화면은 `GET /api/setup-sessions/{id}/zone-drafts` 를 QUEUED 동안 2초마다 받기만 한다.
- 워커는 초안을 VLM 때문에 미루지 않는다: `ai/services/worker/vlm_gate.py` 가 vLLM 미준비 때 빼는 유형은 `vlm_rationale`·`incident_summary` 뿐이고,
  `ai/src/simai_ai/vlm/zone_draft.py` 는 VLM 비가용이면 기하 후보를 「AI 의견 없음」(`vlm_status=NO_OPINION`)으로 돌려준다. 실패·무한 대기가 아니다.
- 그래서 화면이 초안 요청을 미루는 것은 맞지 않고(미룰 요청이 없다), READY 때 초안 조회를 다시 받는 것까지만 한다. 미리보기가 VLM 준비보다 먼저 나왔으면
  그 판의 초안은 AI 의견 없이 남는다(목록 카드의 「AI 의견 없음」). 창 문구는 「준비되면 … AI 구역 초안을 보여 드립니다」로, 의견이 붙는다고 약속하지 않는다.
- 경보 근거(`vlm_rationale`)는 마감이 관측 + 5 s 라(`AnalysisJobService`) VLLM 동안 난 경보는 준비 뒤에도 근거가 붙지 않는다. 「AI 근거 준비 중」에 마우스 올림 설명으로 적었다.

## 버린 대안

- **초안 요청을 화면이 미루기**: 화면이 넣는 요청이 없다(위). 서버·워커가 미루게 하는 것은 아래 남은 위험.
- **VLLM 단계면 상단 GPU 점을 노랑으로**: 사용자 결정(점은 Tier 0 연결만)과 어긋난다.
- **구역 단계 안 한 줄 안내(창 없이)**: 사람이 기다릴지 손으로 할지 정해야 하므로 원칙상 큰 창. 시한 초과도 같은 까닭으로 창.
- **사건 상세가 `useGpuStatus` 로 상태를 받기**: 관리자 화면에서는 같은 키라 요청이 겹치지 않지만, 사건 상세가 뜨는 시험·화면마다 상태 요청이 생긴다. 캐시만 읽는다.
- **VLLM 동안 상태 조회를 3초로**: 창이 닫히는 데 최대 10초 늦을 뿐이라 주기를 바꾸지 않았다.

## 남은 위험·넘긴 일

- 미리보기 맵이 VLM 준비보다 먼저 나오면(첫 부팅 HF 받기 20분+ 등) 그 판 초안은 AI 의견 없이 남는다. VLM 의견을 붙이려면 서버가 VLLM → READY 전환 때
  그 세션의 마지막 초안 판이 NO_OPINION 이면 다시 넣거나, 워커 게이트가 시한 안에서만 `vlm_zone_draft` 를 미루는 변경이 필요하다(서버 또는 ai 티켓, 이 티켓 범위 밖).
- 시한 초과 판정은 부팅이 싣는 문구(「vLLM 미준비」)에 기대므로 부팅 문구가 바뀌면 같이 고쳐야 한다(`VLLM_TIMEOUT_MARK`). 서버가 사유 코드를 주면 그것으로 바꾼다.
- 관리자가 아니면 창이 뜨지 않는다(상태 API 가 관리자 전용). 세팅은 관리자가 하므로 지금은 문제없다.

## 코드·시험 위치

- 코드: `front/apps/admin/src/features/gpu/{types.ts,gpuStatus.ts,useGpu.ts}`, `features/setup/VlmZoneNotice.tsx`(SetupPage 가 그린다), `features/events/EventDetailPanels.tsx`.
- 시험: `__tests__/features/gpu/gpuStatus.test.ts`(판정), `__tests__/features/setup/VlmZoneNotice.test.tsx`(VLLM 창·직접 만들기·READY 자동 닫힘·초안 다시 받기·시한 초과·세션 없음·비관리자·구역 단계 아님),
  `__tests__/features/events/aiRationalePending.test.tsx`(「AI 근거 준비 중」).
