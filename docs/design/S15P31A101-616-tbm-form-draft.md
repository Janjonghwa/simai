# TBM 양식 칸 AI 초안·종료 미팅 문장 (S15P31A101-616, 1단계: ai·contracts, 2단계: server)

날짜 작업 TBM 보고서가 고정 양식(`report.form`, -609 곽나혁, 설계 `docs/design/tbm-daily-form.md`)으로 바뀌어,
AI 초안이 **양식 칸 JSON** 을 내고 스키마로 검사받는다(결정 "계약 d"). 서버는 나중에 빈 칸만 채운다.
1단계(MR !555)는 AI·계약 쪽만 만들었다. 2단계는 -609 머지 뒤 서버를 연결했다(아래 '2단계 서버 연결'). 계약은 2단계에서 바꾸지 않았다.

관련 명세: 8.3(TBM 보고서, 작업 마무리 자동 채움), 5.4(외부 LLM 텍스트만), 11.3·11.4-5·14장(NFR-14 개인정보), 7장(규칙 R1~R11), AGENTS 규칙 4·5·8.

## 만든 것

| 항목 | 위치 |
|---|---|
| 계약(v7.32.0, 4개 신설 1.0.0) | `contracts/messages/tbm_form_draft_request·_result.schema.json`, `tbm_form_ending_request·_result.schema.json`, 예시·반례 `contracts/examples/(invalid/)tbm_form_*` |
| 초안 생성·검사·대체 | `ai/src/simai_ai/report/tbm_form.py` |
| 대체 문장 사전(초안 대체 문장) | `ai/src/simai_ai/report/tbm_form_templates.py` |
| 종료 미팅이 쓰는 작업 마무리 | `ai/src/simai_ai/report/wrap_up.py` 의 `run()`(대체 사유·길이 상한 추가, 기존 `generate()` 응답은 그대로) |
| 컨테이너 경로 | `ai/services/risk_engine/app.py` `POST /v1/report/form-draft`, `POST /v1/report/form-ending` |
| 시험 | `ai/tests/report/test_tbm_form.py`, `ai/tests/services/risk_engine/test_form_api.py` |

## 입력과 출력

### 초안 `POST /v1/report/form-draft`

요청 `tbm_form_draft_request`(봉투 없음, 닫힌 객체):

```json
{
  "request_id": "rpt-41-r3",
  "report_date": "2026-11-12",
  "tasks": [
    {"code": "SLAB_OPENING_COVER", "label": "슬래브 개구부 덮개 설치", "rule_tags": ["R1", "R2", "R5"]},
    {"text": "자재 정리"}
  ],
  "rule_tags": ["R1", "R2", "R5"],
  "zones": [{"name": "개구부 1", "zone_kind": "OPENING", "rule_id": "R2"},
            {"name": "3층 슬래브 단부", "zone_kind": "FLOOR_EDGE", "rule_id": null}],
  "existing": {"task_name": "", "place": "", "risk_assessed": null, "risks": [...3줄], "focus": {...},
               "checks": [{"code": "PPE", "yes": true, "action": ""}, ...5줄], "review": {"daily": ""}}
}
```

- `tasks` 는 양식 `info.tasks` 순서 그대로이고, 항목이면 `GET /api/work-tasks` 의 `code`·`label`·`rule_tags` 셋, 직접 쓴 작업이면 `text` 하나다(섞으면 스키마 위반).
- `rule_tags` 는 항목 태그의 합집합이다(예전 공종 `rule_tags` 대신). 합집합과 다르면 400(스키마 밖, `check_request`).
- `zones` 는 그 작업의 현장 세팅에서 발행한 구역의 표시 이름·유형·규칙. 없으면 빈 배열이고 장소는 비운다.
- `existing` 은 선택이다. 보낼 때는 초안이 채우는 칸을 모두 싣는다(빈 칸은 `""`·`null`). 비지 않은 칸만 LLM 에 사실로 준다.

응답 `tbm_form_draft_result`(200):

```json
{
  "request_id": "rpt-41-r3",
  "form_draft": {
    "task_name": "슬래브 개구부 덮개 설치, 자재 정리",
    "place": "개구부 1, 3층 슬래브 단부",
    "risk_assessed": true,
    "risks": [{"risk": "...", "measure": "..."}, {...}, {...}],
    "focus": {"risk": "...", "measure": "..."},
    "checks": [{"code": "PPE", "yes": null, "action": "..."}, ... "ZONE_CONTROL", "OPENING_GUARD", "WORK_SURFACE_FALL", "HEALTH"],
    "review": {"daily": "..."}
  },
  "fallback": false,
  "fallback_reason": null,
  "citations": [{"node_id": "산업안전보건기준규칙 제43조", "source_name": "...", "article_ref": "제43조", "title": "...", "excerpt": "..."}],
  "generator": {"model": "gpt-5-mini", "prompt_version": "tbm-form-1", "attempts": 1, "input_tokens": 2310, "output_tokens": 640},
  "steps": [{"step": "CANDIDATE_SEARCH", "status": "SUCCEEDED", "detail": "후보 14건, 허용 조문 9건"}, ...]
}
```

- `form_draft` 에는 초안이 채우는 칸만 있다. 일시·작업일 동일·작업내용(`info.date_time_ms`·`same_day`·`tasks`), 점검 문구(`label`, 서버가 덮음), 종료 미팅(`review.ending`)은 없다.
- 응답 위치: 서버 양식의 `info.task_name`·`info.place`·`info.risk_assessed` ← `form_draft` 의 같은 이름, `risks`·`focus` ← 그대로, `checks[i].yes`·`action` ← 같은 코드 줄, `review.daily` ← `form_draft.review.daily`.
- 글자 수 상한은 서버 `TbmForm` 과 같다: 작업명 100, 장소 200, 위험·대책·조치 500, 일일 미팅 2000(코드 포인트 수. JSON Schema `maxLength`·Python `len`·Java `codePointCount` 가 같다).
- 오류: 스키마·일관성 위반 400(본문 없음), 본문 4 MiB 초과 413. 응답이 계약을 어기면 보내지 않고 본문 없는 500. LLM·검사 실패는 오류가 아니라 200 대체 초안이다.

### 종료 미팅 `POST /v1/report/form-ending`

요청 `tbm_form_ending_request`: `{request_id, window_ms: [실제 시작, 실제 종료], facts}`. `facts` 는 기존 작업 마무리(`/v1/report/wrap-up`, `docs/design/work-start-end-wrapup.md`)의 집계 사실과 같은 키다.
응답 `tbm_form_ending_result`: `{request_id, ending(1~2000자), fallback, fallback_reason, model}`. 대체 문장이면 `model` 은 `deterministic`.

## 누가 무엇을 정하나

| 칸 | 정하는 쪽 | 이유 |
|---|---|---|
| `task_name` | 코드: 작업내용 이름을 `, ` 로 잇고 100자를 넘으면 `첫 이름 외 N건` | 입력에 그대로 있는 사실이다. LLM 이 바꿔 쓸 이유가 없고 지어낼 여지만 생긴다(-610 의 작업 이름 규칙과 같은 꼴) |
| `place` | 코드: 구역 이름(중복 제거, 순서대로), 200자를 넘으면 `… 외 N곳`. 구역이 없으면 `""` | 모르면 비운다. 장소를 지어내지 않는다 |
| `risk_assessed` | 코드: 합집합 태그가 하나라도 있으면 true, 없으면 null | 아래 '위험성평가 실시' |
| `risks`·`focus`·`checks[*].action`·`review.daily` | LLM(검사 통과 시), 아니면 대체 문장 | 문장 작성이 LLM 몫 |
| `checks[*].yes` | 늘 null(스키마가 const null 로 막음) | 아래 'yes 를 비우는 이유' |

### yes 를 비우는 이유

TBM 점검표의 예·아니오는 "보호구를 착용했는가", "개구부 덮개가 설치됐는가"처럼 **작업 직전에 사람이 현장을 보고 확인하는 답**이다.
초안 입력(작업내용·구역·날짜·이미 채운 칸)에는 그 확인의 근거가 하나도 없다. AI 가 true 를 채우면 아무도 보지 않은 것을 확인했다고 적는 셈이고,
서버는 빈 칸만 채우므로 사람이 그 칸을 다시 보지 않을 수 있다. 그래서 초안은 `yes` 를 null 로 두고 `action`(오늘 할 조치 제안)만 쓴다.
AGENTS 규칙 4(AI 는 제안만 한다)와 규칙 2(모르면 비운다)를 따른 것이다. 스키마가 `const: null` 로 막아(반례 `tbm_form_draft_result.check_yes_true`)
나중에 근거 입력(예: LIVE PPE 상태)을 붙이려면 계약을 넓히는 판 변경과 그 근거 필드가 함께 있어야 한다.
LLM 에도 "확인 결과를 쓰지 않는다"고 지시하고, 응답 점검 줄은 `code`·`action` 두 키만 받는다(`yes` 키가 오면 그 초안을 버린다).

### 위험성평가 실시(`risk_assessed`)

요청서는 이 칸을 초안이 채우는 칸으로 넣었다. 이 칸은 "이 작업의 위험성평가 내용이 있는가"이고, 초안이 위험 3줄·대책을 작업내용 태그에서 만들었을 때는
그 평가 내용이 문서 안에 실제로 있으므로 true 로 제안한다. 태그가 없으면(직접 쓴 작업만) 일반 위험 문장만 들어가므로 null(모름)로 둔다.
`yes` 와 달리 현장 상태를 확인했다는 주장이 아니라 문서 내용에 대한 사실이라 채운다. 사람이 이미 값을 넣었으면 서버가 덮지 않는다.

## 프롬프트 모양

- system: 역할(TBM 일지 양식 보조), 응답 JSON 모양(`risks` 3줄 `{risk, measure, rule_tags}`, `focus`, `checks` 5줄 `{code, action}` 주어진 순서, `review_daily`, `citations`),
  규칙(태그 주제 먼저, 줄의 `rule_tags` 는 입력 태그만, 확인 결과 쓰지 않기, 글자 수, `clauses` 의 node_id 만 인용·본문 조문 번호도 그 조만·clauses 가 비면 조문 번호 금지,
  법령·수치·사람·사고 사실 지어내지 않기, 확률·퍼센트 금지, `existing` 과 어긋나지 않기). 프롬프트 판 `tbm-form-1`.
- user: JSON 텍스트 하나 `{report_date, task_name, place, tasks[{label}|{text}], rule_tags[{rule_tag, name, check_code}], zones[{name, zone_kind, rule_id}],
  checks[{code, label}], existing{비지 않은 칸만}, clauses[{node_id, source_name, article_ref, title, body(600자)}]}`.
- 규칙 태그 → 위험 주제·점검 코드 연결(LLM 에 `check_code` 로 함께 준다): R2 → OPENING_GUARD, R3·R4 → WORK_SURFACE_FALL, R1·R5·R6·R7·R8·R10 → ZONE_CONTROL, R9·R11 → PPE. HEALTH 는 태그와 무관.
- 법령 조문: 기존 법령 그래프 검색(`retrieval.retrieve`, `PgGraphStore`)에 합집합 태그·구역 유형·작업내용 이름을 넣어 허용 목록을 만든다.
  `SIMAI_RAG_PG_DSN` 이 없으면 건너뛰고(SKIPPED), DB 오류·빈 코퍼스면 FAILED 로 적고 조문 없이 만든다(초안을 막지 않는다).

### 외부 LLM 에 보내는 것

텍스트 두 칸(system, user)뿐이고 보내기 전에 `assert_text_only`(role·content 문자열만, URL·객체 주소·data URI 없음)로 확인한다.
요청 계약이 닫힌 객체라 리더·참석자·서명자 실명, 계정, 얼굴·영상·이미지·객체 주소, World ID·좌표가 들어올 자리가 없다(반례 `leader_identity`·`zone_image`).
처리기는 스키마 위반·주소·NUL 이 든 요청을 LLM 을 부르기 전에 400 으로 거절한다.
남는 위험: 구역 이름·직접 쓴 작업·이미 채운 칸은 관리자가 쓴 자유 글자다. 사람 이름을 넣지 않는 운영 기준에 기댄다(작업 마무리의 구역 표시명과 같은 처지).

## 검사와 재시도

LLM 응답마다(`check_output` → `assemble` → `form_problems`):

1. JSON 객체(```json 한 겹은 벗김), 최상위 키는 정확히 `risks, focus, checks, review_daily, citations`.
2. `risks` 정확히 3줄, 줄마다 `risk, measure, rule_tags` 만. `rule_tags` 는 중복 없는 입력 태그 부분집합. 입력 태그가 있는데 세 줄 모두 태그가 없으면 거절.
3. `checks` 정확히 5줄, 키 `code, action` 만, 코드 순서 PPE·ZONE_CONTROL·OPENING_GUARD·WORK_SURFACE_FALL·HEALTH.
4. 모든 글자 칸: 공백 제거 뒤 비지 않음, 상한 이하, NUL 없음, 확률·퍼센트 표현 없음, 본문의 `제N조` 는 허용 조문의 조만.
5. `citations` 는 허용 목록 안의 node_id, 중복 없음.
6. 조립한 `form_draft` 를 줄 수·순서·글자 수·`yes` null 로 다시 본다. 처리기가 응답 전체를 계약 스키마로 한 번 더 검사한다.

걸리면 문제 문장만(최대 20개, 응답 값은 넣지 않음) 덧붙여 처음 두 메시지로 한 번 더 만든다. 거절한 초안은 다시 보내지 않는다(지어낸 내용이 다음 입력의 사실로 섞이지 않게, wrap_up 과 같은 규칙).
두 번째도 걸리면 `VALIDATION_FAILED` 대체 초안이다. LLM 설정이 없으면 `LLM_NOT_CONFIGURED`(호출 0회), 시간 초과 `LLM_TIMEOUT`, 그 밖의 연결·인증·응답 오류 `LLM_UNAVAILABLE`.
대체 초안에는 인용이 없다(스키마). `generator.model` 은 LLM 을 한 번이라도 불렀으면 그 모델, 못 불렀으면 `deterministic`.

## 대체 문장(초안 대체 문장)

요청서가 말한 '초안 대체 문장' 묶음은 저장소에 없어서 `tbm_form_templates.py` 한 모듈에 정했다. 자리 표시는 `{where}` 하나(그 태그와 관련된 첫 구역 이름 + ` 주변`, 없으면 `작업 구간`)다.
관련 구역은 구역의 `rule_id` 가 그 태그인 것이 먼저, 그다음 구역 유형(`OPENING`→R1·R2·R5, `FLOOR_EDGE`→R2, `ELEVATED_WORK_SURFACE`·`SCAFFOLD`→R3·R4, `EQUIPMENT_PARKING`→R8, `LIFTING`→R1·R8).

규칙 태그별 위험 문장(위험 / 대책, 점검 코드):

| 태그 | 이름 | 점검 | 위험 | 대책 |
|---|---|---|---|---|
| R1 | 위험 구역 진입 | ZONE_CONTROL | {where} 위험구역에 작업자가 들어가 다칠 위험 | {where} 위험구역 경계를 표시하고 작업자 외 출입을 통제한다 |
| R2 | 가장자리 근접 | OPENING_GUARD | {where} 개구부·단부 가장자리에 다가가 떨어질 위험 | 개구부 덮개를 고정하고 단부에 안전난간을 설치한 뒤 작업한다 |
| R3 | 고소 위치 | WORK_SURFACE_FALL | {where} 높은 작업면에서 균형을 잃고 떨어질 위험 | 작업발판·난간 상태를 확인하고 안전대를 걸고 작업한다 |
| R4 | 낙상 의심 | WORK_SURFACE_FALL | {where} 자재·장애물에 걸리거나 미끄러져 넘어질 위험 | 통로의 자재와 장애물을 치우고 바닥 상태를 확인한 뒤 작업한다 |
| R5 | 진입 예측 | ZONE_CONTROL | {where} 위험구역 쪽으로 이동하다 모르고 들어갈 위험 | 이동 동선을 위험구역과 떨어뜨려 정하고 동선을 작업자에게 알린다 |
| R6 | 위험 구역 내 관측 상실 | ZONE_CONTROL | {where} 위험구역 안 작업자가 가려져 위험을 놓칠 위험 | 위험구역 안에서는 2인 1조로 작업하고 서로 위치를 확인한다 |
| R7 | 구역 감시 능력 저하 | ZONE_CONTROL | {where} 자재·시야 가림으로 위험구역 감시가 약해질 위험 | 시야를 가리는 자재를 치우고 위험구역 감시자를 둔다 |
| R8 | 장비 근접 | ZONE_CONTROL | {where} 작업 장비에 가까이 다가가 부딪히거나 끼일 위험 | 장비 작업 반경을 표시하고 신호수를 배치한다 |
| R9 | 안전모 미착용 | PPE | 안전모를 쓰지 않아 떨어지는 물체에 머리를 다칠 위험 | 작업 전 안전모 착용과 턱끈 체결을 서로 확인한다 |
| R10 | 불확실 점유 근접 | ZONE_CONTROL | {where} 확인되지 않은 물체가 있는 곳에 다가갈 위험 | 작업 전 주변을 확인하고 확인되지 않은 곳은 출입을 통제한다 |
| R11 | 조끼 미착용 | PPE | 안전조끼를 입지 않아 장비 운전자 눈에 띄지 않을 위험 | 작업 전 안전조끼 착용을 서로 확인한다 |

- 위험 3줄: 합집합 태그를 우선순위 R2, R3, R4, R1, R5, R8, R9, R11, R6, R7, R10(떨어짐 → 구역 진입 → 장비·보호구 → 감시 상태) 순으로 앞에서 셋.
  모자라면 일반 위험으로 채운다: ① 작업 통로에 놓인 자재에 걸려 넘어질 위험 / 작업 전 통로를 정리하고 자재는 정해진 곳에 쌓는다 ② 보호구를 갖추지 않고 작업에 들어갈 위험 / 안전모·안전화·장갑 착용을 작업 전에 서로 확인한다 ③ 작업 순서를 공유하지 않아 함께 작업하다 부딪힐 위험 / 작업 순서와 담당을 TBM 에서 정하고 바뀌면 다시 알린다.
- 중점 위험 = 첫 줄.
- 점검 조치(관련 태그가 있을 때 / 없을 때):

| 점검 | 관련 태그 있음 | 없음 |
|---|---|---|
| PPE | 안전모 턱끈 체결 등 보호구 착용 상태를 작업 전 서로 확인한다 | 안전모·안전화 등 보호구 착용을 작업 전 서로 확인한다 |
| ZONE_CONTROL | {where} 위험구역 경계 표시와 출입 통제 상태를 확인한다 | 작업 구간의 위험구역 표시와 출입 통제 상태를 확인한다 |
| OPENING_GUARD | {where} 개구부 덮개 고정과 안전난간 설치를 확인한다 | 작업 구간에 개구부가 있는지 확인하고 있으면 덮개·난간을 설치한다 |
| WORK_SURFACE_FALL | {where} 작업발판 고정·난간·안전대 걸이 설비를 확인한다 | 높은 곳 작업이 있으면 작업발판과 추락 방호 설비를 확인한다 |
| HEALTH | 작업 전 작업자 컨디션과 음주·피로 여부를 확인한다 | (같음) |

- 일일 미팅: `{날짜} {작업명} 작업 전 TBM. 장소: {장소}.(장소가 있을 때) 주요 위험: {세 줄 위험 ; 로 이음}. 중점 관리: {중점 위험}. 대책: {중점 대책}. 작업 전 점검 항목은 현장에서 확인한 뒤 표시한다.` (2000자 넘으면 자름)
- 시험이 모든 태그(R1~R11)·모든 작업내용 항목 7개·태그 없음에 대해 대체 초안이 계약과 상한을 통과하는지 본다.

## 종료 미팅(B10)

명세 8.3 의 작업 마무리 경로(작업 종료 때 서버가 실제 진행 구간·대상 구역의 LIVE `risk_event` 집계 사실을 모음 → 컨테이너가 문단 작성 → 숫자 검사·1회 재작성·결정적 문장)를 그대로 쓴다.
`wrap_up.run()` 에 `max_chars` 와 대체 사유를 더했고(기존 `/v1/report/wrap-up` 응답 `{text, fallback, model}` 은 그대로), `generate_ending` 이 `work_type` 없이(작업내용은 공종이 아니어서 넘기지 않음)
`max_chars=2000` 으로 부른다. 2000자를 넘는 LLM 문단은 형식 위반으로 한 번 다시 쓰게 하고, 결정적 문장이 넘으면(규칙·구역이 아주 많을 때) 마지막 온전한 문장까지 자른다.
집계 일관성(등급·규칙·시간대 합, 미확인 CRITICAL ≤ CRITICAL, 구역 건수 순서 등)이 틀리면 400 이다(`wrap_up.validate_request`).

## 2단계 서버 연결(-609 머지 뒤)

### 만든 것

| 항목 | 위치 |
|---|---|
| 초안·종료 미팅 서비스(비동기, 계약 검사, 결정적 문장 대체, 재기동 복구) | `server/.../domain/report/TbmFormAiService.java` |
| 초안 행 잠금·상태 전이·세팅 구역 조회·양식 저장 | `server/.../domain/report/TbmFormAiRepository.java` |
| 초안 요청의 `existing`, 빈 칸만 채우기 | `TbmForm.draftFields`, `TbmForm.fillDraft` |
| 관리자 경로 `POST /api/reports/tbm/{reportId}/form-draft`(202) | `TbmReportController.formDraft` |
| 작업 종료: 날짜 작업은 같은 행 종료 미팅, 예전 작업은 새 버전 마무리 | `WorkWrapUpRepository.prepare`(mode), `endingRequest`, `completeEnding`, `WorkSessionService.end` |
| 오류 코드 | `REPORT_NO_FORM`(409, 양식 없는 예전 보고서), `REPORT_FORM_NO_TASKS`(409, 작업내용 없음), `REPORT_FORM_MANAGED`(409, 날짜 작업 보고서에 예전 `POST /api/reports/tbm`) |
| 예전 새 버전 생성 경로 막기 | `TbmReportRepository.createGenerating`(작업 행 잠금 뒤 최신 TBM 보고서에 양식이 있으면 409 `REPORT_FORM_MANAGED`, 레이아웃 검사보다 먼저) |
| 시험 | `server/src/test/.../report/TbmFormAiIntegrationTest.java`, `WorkSessionIntegrationTest`(날짜 작업 종료 2건) |

### 초안(B9)

```
POST /api/reports/tbm/41/form-draft            (ADMIN, 본문 없음)
202 {"report_id":41,"work_id":7,"report_version":1,"draft_status":"GENERATING","form_revision":3}
```

1. 트랜잭션 1: 작업 행 → 보고서 행 순서로 `FOR UPDATE`(서명·예전 생성 경로와 같은 순서). 받는 상태는 **PENDING(첫 초안)·FAILED(재시도)·SUCCEEDED(다시 초안)** 다.
   다시 초안을 받아도 빈 칸만 채우므로 사람이 쓴 칸은 지켜지고, 새 버전을 만들지 않으니 서명도 그대로다. 거절: GENERATING 409 `REPORT_IN_PROGRESS`(본문에 `report_id`),
   양식 없음 409 `REPORT_NO_FORM`, `info.tasks` 비었음 409 `REPORT_FORM_NO_TASKS`(계약 `tasks` 최소 1개), 최신 버전 아님 409 `REPORT_VERSION_STALE`, 잠김 409 `REPORT_LOCKED`, 없음 404.
   같은 작업에 다른 GENERATING 행(예전 경로)이 있어도 409. 통과하면 `status=GENERATING`, `failure_reason`·`completed_at` 비움. 같은 트랜잭션에서 요청을 고정하고 커밋한다.
2. 요청(`tbm_form_draft_request`): `request_id = rpt-<report_id>-r<form_revision>`, `report_date` = `work_unit.work_date`, `tasks` = `info.tasks` 순서대로 `WorkTasks` 로 풀기(`{text}` 그대로),
   `rule_tags` = 합집합(R 번호 순), `zones` = 아래, `existing` = 지금 양식의 초안 칸(`TbmForm.draftFields`). 리더·서명·계정·작업 이름(`work_unit.name`)·구역 ID·좌표는 싣지 않는다.
   보내기 전에 요청 스키마로 검사한다(위반이면 FAILED `INVALID_DRAFT_REQUEST`, 엔진을 부르지 않음).
3. `zones`: `work_unit.active_setup_session_id` 의 `setup_session_zone` 중 지금 유효하고(`valid_to IS NULL`) 구성 묶음으로 발행한 구역의 `name`·`zone_kind`
   (`SetupZoneLayoutService.list` 와 같은 기준, 만든 순서). 같은 이름·유형은 한 번, 이름 1~100자·유형 1~64자 밖은 빼고 50개까지.
   **`rule_id` 는 늘 null** 이다. `zone` 표에 규칙 칸이 없어 모르면 비운다(AGENTS 규칙 2). 세팅이 없으면 빈 배열이고 장소는 비워진다.
4. 커밋 뒤 가상 스레드에서 기존 `RiskEngineReportClient.postJson("/v1/report/form-draft")`(주소·타임아웃은 보고서 생성과 같은 설정). 응답 전체를 `ContractSchemas`
   (`messages/tbm_form_draft_result`, jar 의 contracts/)로 검사하고 `request_id` 가 같은지 본다.
5. 트랜잭션 2: 보고서 행을 다시 `FOR UPDATE` 로 잠그고 **지금** 양식에서 빈 칸(글자 `""`, 예·아니오 null)만 채운다(`TbmForm.fillDraft`). 채운 칸이 있으면
   `TbmForm.normalize` 로 다시 검사해 저장하고 `form_revision + 1`, 그다음 기존 `completeSucceeded`(citations·steps·llm_model 저장, sections 등은 빈 값).
   채울 칸이 없으면 revision 은 그대로다(편집 중인 화면이 쓸데없이 409 를 받지 않게).
6. 실패: 연결 실패·5xx·4xx `ENGINE_UNAVAILABLE`, 시간 초과 `ENGINE_TIMEOUT`, 계약 위반·`request_id` 다름·양식에 맞지 않음 `ENGINE_CONTRACT_VIOLATION` → 기존 `completeFailed`, 양식은 그대로.
   트랜잭션 2에서 그 사이 잠겼으면 `REPORT_LOCKED`, 새 버전이 생겼으면 `REPORT_VERSION_STALE`, `info.tasks` 가 바뀌었으면 `FORM_TASKS_CHANGED`(초안의 작업명·위험이 지금 작업과 맞지 않음)로 FAILED.
7. 대체 초안(`fallback: true`)도 SUCCEEDED 다. 대체 여부·사유는 따로 칸을 두지 않고 `steps` 에 남는다(컨테이너가 넣는 `FALLBACK` 단계, 없으면 서버가
   `{"step":"FALLBACK","status":"SUCCEEDED","detail":"결정적 대체 초안(<사유>)"}` 를 붙임). `llm_model` 은 `generator.model`, 서버 로그에 `fallback`·`reason`·`model` 을 한 줄 남긴다.
8. 재기동: GENERATING 으로 남은 행은 기존 `TbmReportService.recoverInterrupted` 가 FAILED(`INTERRUPTED`)로 마감한다(양식은 그대로).

#### 빈 칸만 채우는 규칙(`TbmForm.fillDraft`)

- `info.task_name`·`info.place`·`checks[*].action`·`review.daily`: 지금 `""` 이면 초안 값.
- `info.risk_assessed`·`checks[*].yes`: 지금 null 이면 초안 값(초안 `yes` 는 계약상 늘 null 이라 실제로는 채우지 않는다).
- 위험·대책 한 쌍(`risks[i]`, `focus`): 빈 칸이어도 **같은 줄의 다른 칸이 비었거나 요청 때(`existing`)와 같을 때만** 채운다.
  요청이 나간 뒤 사람이 위험을 새로 쓰면 AI 는 그 글을 못 봤으므로 짝이 맞지 않는 대책을 넣지 않고 비워 둔다.
- 점검 줄은 코드로 맞추고 `label` 은 서버 값. 작업내용·일시·작업일 동일·종료 미팅은 건드리지 않는다.

#### -615(세팅 완료, C17) 호출 계약

- 부르는 곳: 세팅 완료 트랜잭션이 **커밋된 뒤**(구역이 발행돼 있어야 `zones` 에 실린다). 같은 트랜잭션 안에서 부르면 아직 커밋 안 된 구역이 빠지고, 바깥이 되돌려지면 초안은 채우지 않고 끝난다.
- 메서드: `TbmFormAiService.tryStartDraftForWork(long workId) → Optional<ObjectNode>`(세팅 완료처럼 초안이 부가 작업일 때. 시작 못 하면 사유를 INFO 로그로 남기고 빈 값)
  또는 `startDraftForWork(long workId) → ObjectNode`(예외를 그대로 받음: 404 `WORK_UNIT_NOT_FOUND`·`REPORT_NOT_FOUND`, 409 위 목록). 대상은 그 작업의 최신 TBM 보고서다.
- 돌아오는 값은 HTTP 경로와 같은 `{report_id, work_id, report_version, draft_status:"GENERATING", form_revision}`. 결과는 기다리지 않는다(화면은 GET 의 `draft_status`·`form_revision` 으로 본다).
- 작업내용이 비어 있으면(아직 양식에서 안 골랐으면) 시작하지 않는다(409 `REPORT_FORM_NO_TASKS`). 그때는 화면의 "AI 초안" 버튼(`POST .../form-draft`)으로 나중에 받는다.

### 종료 미팅(B10)

1. 작업 종료 트랜잭션(`WorkWrapUpRepository.prepare`)이 그 작업의 최신 TBM 보고서를 `FOR UPDATE` 로 잠근다. **양식이 있으면(날짜 작업) 새 버전을 만들지 않고**
   같은 행에 `wrap_up_status=GENERATING`·`wrap_up_facts`(기존 집계 그대로)를 적고 `{mode:"FORM_ENDING", status:"GENERATING", report_id}` 를 낸다.
   초안 상태(`status`)와는 따로 간다(PENDING·GENERATING·SUCCEEDED·FAILED 어느 것이든 종료 미팅을 받는다). 잠겼으면 `SKIPPED`·`REPORT_LOCKED`.
   양식이 없으면(예전 작업) 지금까지처럼 `mode:"NEW_VERSION"` 새 버전 마무리(`/v1/report/wrap-up`).
2. 커밋 뒤 `WorkSessionService.end` 가 mode 를 보고 `TbmFormAiService.startEnding` 을 부른다. 요청 `{request_id: rpt-<report_id>-end, window_ms, facts}` 를 요청 스키마로 검사하고
   `POST /v1/report/form-ending`, 응답을 `tbm_form_ending_result` 스키마·`request_id` 로 검사한다.
3. 실패(연결·시간 초과·5xx·계약 위반)면 서버 결정적 문장 `TbmReportService.wrapUpFallback(facts)`(2000자에서 자름), `fallback=true`, `model=deterministic`.
4. `completeEnding`: 행을 `FOR UPDATE` 로 잠그고 `form.review.ending` 이 `""` 일 때만 채우고 그때만 `form_revision + 1`. `wrap_up_status=SUCCEEDED`, `wrap_up_fallback`·`wrap_up_model` 기록.
   **`wrap_up_summary` 는 쓰지 않는다**(지금 서명 해시 `content_sha256` 이 덮는 칸이라, 이미 서명한 행에 쓰면 해시가 어긋난다). 그 사이 잠겼으면 양식은 그대로 두고 `wrap_up_status=FAILED`.
5. 재기동: 종료 미팅을 쓰다 멈춘 날짜 작업 행은 `TbmFormAiService.recoverEndings` 가 저장된 사실로 같은 결정적 문장을 채워 마감한다.
   `failInterrupted` 는 양식 있는 행의 `wrap_up_status` 를 건드리지 않게 바꿨다(두 복구가 순서와 무관).

#### -609 의 작업 종료 새 버전 복사와의 관계(결정)

날짜 작업은 B10 이 같은 행을 채우므로 **작업 종료 새 버전이 더 필요 없다.** 새 버전은 작업 전에 받은 서명을 끊고(서명은 `report_version` 에 묶임) 양식을 두 행으로 나눈다.
그래서 `prepare` 는 양식이 있으면 새 버전 경로로 가지 않는다. 예전 경로의 `form, form_revision, leader_user_id` 복사는 남겨 뒀다(그 경로에 오는 보고서는 보통 양식이 없고, 리더 복사는 예전 작업에도 의미가 있다).

### 명세에 없어 정한 것(2단계)

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 다시 초안 | PENDING·FAILED·SUCCEEDED 모두 받음, GENERATING 만 409 | 빈 칸만 채워 사람 칸은 지켜지고 새 버전이 없어 서명도 그대로다. 사람이 한 줄을 비우고 다시 받는 쓰임이 있다 |
| revision 증가 | 채운 칸이 있을 때만 +1 | 1단계 계획과 같다. 채운 것이 없는데 올리면 편집 중인 화면이 이유 없이 409 를 받는다 |
| 위험·대책 쌍 | 같은 줄의 다른 칸이 비었거나 요청 때와 같을 때만 채움 | AI 가 보지 못한 사람 글과 짝이 맞지 않는 대책이 들어가는 것을 막는다 |
| 작업내용 변경 | 두 트랜잭션 사이 `info.tasks` 가 바뀌면 FAILED `FORM_TASKS_CHANGED` | 초안 작업명·위험이 지금 작업과 어긋난다. 화면이 다시 요청하면 된다 |
| 구역 출처 | 작업의 현재 세팅 세션에서 만들고 발행한 현재 구역 | 세팅이 작업에 묶여 있고(`active_setup_session_id`) `SetupZoneLayoutService.list` 가 같은 기준을 쓴다. 현장 전체 구역은 다른 작업 구역까지 섞인다 |
| 구역 `rule_id` | 늘 null | `zone` 표에 규칙 칸이 없다. 구역 유형으로 규칙을 짐작하는 일은 컨테이너가 이미 한다(대체 문장의 관련 구역) |
| 대체 여부 저장 | `steps` 의 FALLBACK 단계 + 서버 로그, 새 칸 없음 | 마이그레이션 번호 경쟁(-609 V49→V51)을 피함. 화면이 필요하면 `steps` 에서 읽거나 나중에 칸을 더한다 |
| 실패 사유 | `ENGINE_TIMEOUT`·`ENGINE_UNAVAILABLE`(기존), `ENGINE_CONTRACT_VIOLATION`·`INVALID_DRAFT_REQUEST`·`FORM_TASKS_CHANGED`·`REPORT_LOCKED`·`REPORT_VERSION_STALE`(신설), 재기동 `INTERRUPTED`(기존) | 연결 장애와 계약 어긋남을 운영에서 구분 |
| 잠긴 보고서 | 초안·종료 미팅 모두 양식을 건드리지 않음 | 양식 저장(`PUT .../form`)이 잠긴 행을 막는 것과 같다 |

### 남은 일·위험(2단계)

- **-611 전에는 첫 서명이 보고서를 잠근다.** 작업 전에 서명을 받으면 작업 종료 때 `REPORT_LOCKED` 로 종료 미팅을 건너뛴다(양식 저장도 막히는 것과 같은 처지).
  -611 이 잠금을 '제출'로 옮기면 풀린다. 또 지금 서명은 `status=SUCCEEDED` 를 요구하므로, 초안을 한 번도 받지 않은 PENDING 보고서에는 서명할 수 없다(-611 범위).
- -611 의 제출 해시(`content_sha256`)에 `form` 이 들어가면 날짜 작업의 종료 미팅은 `form.review.ending` 으로 해시에 포함된다(`wrap_up_summary` 는 날짜 작업에서 비어 있음).
- 화면: `POST .../form-draft` 버튼, `draft_status` 폴링, 대체 초안 표시(`steps` 의 FALLBACK), 409 `REPORT_FORM_NO_TASKS` 안내.
- 컨테이너가 요청을 400 으로 거절하면(예: 사람이 쓴 칸에 URL) `ENGINE_UNAVAILABLE` 로 보인다. 응답 코드를 실패 사유로 나누지 않았다.
- (해결) 세팅 구역을 작업 배치에 반영하면(`SetupZoneLayoutService`) 날짜 작업에도 `layout_id` 가 생겨 예전 `POST /api/reports/tbm` 이 통과했다.
  양식 없는 새 버전이 최신이 되어 양식·서명이 가려지므로, 최신 TBM 보고서에 양식이 있으면 409 `REPORT_FORM_MANAGED` 로 거절한다(Lead 지시).

## 명세에 없어 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 계약 판 | v7.32.0(v7.27~v7.31 은 다른 브랜치 선점, 원격 모든 브랜치에서 v7.32 미사용 확인) | Lead 지시 |
| 계약 묶음 | 요청·응답 각 파일 4개(봉투 없음) | 예시가 파일 이름으로 스키마를 찾는다. 같은 내부 HTTP 인 `risk_plan_request/result` 와 같은 나눔 |
| 점검 줄 순서 | 스키마는 코드마다 한 줄(contains ×5 + 5줄), 순서는 컨테이너 코드·시험 | `prefixItems` 는 생성 모델(pydantic·jsonschema2pojo)이 다루지 못해 계약 시험이 깨진다 |
| `yes` | `type [boolean, null]` + `const null` | Java 생성 타입이 Object 가 되지 않게(서버 `generatedFieldsKeepTheirTypes`) |
| 작업내용 한 줄 | 한 객체에 code·label·rule_tags·text 를 선택으로 두고 if/then/else 로 섞지 않게 | oneOf 두 객체는 Java 에서 Object 로 떨어진다 |
| `existing` | 보내면 초안 칸 전부 필수(빈 칸은 `""`·null) | 선택 필드 null 은 Java 왕복에서 키가 빠진다. 통째로 보내는 화면 저장과도 같다 |
| 대체 사유 | LLM_NOT_CONFIGURED·LLM_TIMEOUT·LLM_UNAVAILABLE·VALIDATION_FAILED | 기존 보고서 실패 사유 이름과 맞춤. 운영에서 키 누락과 장애를 구분 |
| 재시도 | 같은 요청 안에서 최대 2회 호출 | 기존 TBM·마무리 생성과 같다 |
| 법령 그래프 실패 | 초안을 막지 않고 조문 없이(본문 조문 번호 금지) | 양식은 조문 칸이 없고 초안이 늦거나 없는 편이 더 나쁘다 |

## 버린 대안

- LLM 이 작업명·장소도 쓰게 하기: 입력 사실을 바꿔 쓸 이유가 없고 지어낼 여지만 생긴다.
- 대체 초안에 검색한 조문을 인용으로 붙이기: 대체 문장은 조문을 근거로 쓴 글이 아니라 근거 표시가 거짓이 된다.
- 기존 `/v1/report/wrap-up` 을 그대로 B10 에 쓰기: 2000자 상한·대체 사유·계약 응답이 없어 서버가 다시 잘라야 한다. 내부는 같은 함수를 쓴다.
- 기존 `tbm_report` 계약 확장: 폐기 예정(-415)이고 I5 작업 입력이라 맞지 않는다.

## 잔여 위험

- 자유 글자(구역 이름·직접 쓴 작업·이미 채운 칸)에 사람 이름이 들어가면 LLM 으로 간다(운영 기준 의존).
- LLM 문장의 사실성은 형식·인용·조문 번호·확률 표현까지만 기계로 본다. 위험 문장이 작업과 맞는지는 사람이 본다(초안은 제안).
- `existing` 과 어긋나지 않게 하는 것은 프롬프트 지시뿐이다. 서버가 빈 칸만 채우므로 사람이 쓴 칸은 지켜진다.

## 시험

- `ai/tests/report/test_tbm_form.py`: LLM 초안 계약 통과·yes null·코드가 정한 칸, 재시도 뒤 성공(거절 초안 미전송), 두 번 거절 → VALIDATION_FAILED, 시간 초과·연결 오류·설정 없음 대체, 그래프 실패에도 초안,
  텍스트만·신원 필드 계약 거절, 일관성 검사, 응답 검사 10가지(글자 수·키·순서·확률·조문·태그), 작업명·장소 상한, 모든 태그·작업내용 항목의 대체 초안, 종료 미팅(LLM·상한 초과·시간 초과·설정 없음·집계 불일치).
- `ai/tests/services/risk_engine/test_form_api.py`: 두 경로의 200·400·413, 설정 없을 때 대체 초안.
- 계약: `uv run contracts/scripts/validate_examples.py`, `ai/tests/contracts`(생성 모델 왕복·반례 거부), 서버 `ContractExamplesRoundTripTest`(Java 왕복·타입 잃은 필드 없음).
