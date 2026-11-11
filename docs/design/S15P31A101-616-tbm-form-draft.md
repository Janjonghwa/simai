# TBM 양식 칸 AI 초안·종료 미팅 문장 (S15P31A101-616, 1단계: ai·contracts)

날짜 작업 TBM 보고서가 고정 양식(`report.form`, -609 곽나혁, 설계 `docs/design/tbm-daily-form.md`)으로 바뀌어,
AI 초안이 **양식 칸 JSON** 을 내고 스키마로 검사받는다(결정 "계약 d"). 서버는 나중에 빈 칸만 채운다.
이 이슈는 1단계로 AI·계약 쪽만 만든다. `server/` 는 고치지 않는다(-609 가 보고서 서비스를 바꾸는 중). 2단계(서버 연결)는 -609 머지 뒤.

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

## 2단계 서버 계획(-609 머지 뒤, 이 이슈 밖)

초안(B9):

1. 세팅 완료(C17)나 화면의 "AI 초안" 요청 때, 보고서 행을 `SELECT … FOR UPDATE` 로 잠그고 `draft_status=GENERATING`(기존 `status` 를 GENERATING 으로) 으로 바꾼 뒤 커밋한다.
   이미 GENERATING 이면 새로 시작하지 않는다. 같은 행이고 **새 `report_version` 을 만들지 않는다**(작업 전에 받은 서명이 버전에 묶여 있어서, -609 설계).
2. 커밋 뒤 비동기로 `tbm_form_draft_request` 를 만든다: `request_id = rpt-<report_id>-r<form_revision>`, `report_date` = 작업 날짜, `tasks` = `form.info.tasks` 를 `WorkTasks` 로 풀어(code·label·rule_tags / text),
   `rule_tags` = 합집합, `zones` = 그 작업 세팅에서 발행한 구역의 표시 이름·`zone_kind`·규칙, `existing` = 현재 양식의 초안 칸. 리더(`leader_user_id`)·서명은 넣지 않는다.
3. `POST /v1/report/form-draft`. 받은 본문을 `tbm_form_draft_result` 로 jsonschema 검증한다(contracts/README 사용 지침).
4. 같은 행을 다시 `FOR UPDATE` 로 잠그고, **그 사이 사람이 고친 칸을 포함해 지금 비어 있는 칸만**(글자 `""`, 예·아니오 null) 초안 값으로 채운다. 점검 줄은 코드로 맞추고 `label` 은 서버 값.
   `yes` 는 초안이 늘 null 이라 채울 것이 없다. 채운 칸이 하나라도 있으면 `form_revision + 1`(화면 낙관적 잠금이 새 판을 받게), `draft_status=SUCCEEDED`(대체 초안이어도 SUCCEEDED 이고 `fallback`·`fallback_reason`·모델을 함께 남김).
5. 연결 실패·시간 초과·500·계약 위반 응답이면 같은 행을 `draft_status=FAILED` 로 두고 양식은 건드리지 않는다(빈 칸 그대로, 화면이 다시 요청). 서버 재기동 때 GENERATING 으로 남은 행은 기존 `recoverInterrupted` 처럼 FAILED(INTERRUPTED).
6. 잠금된(제출된) 버전은 채우지 않는다(-611 제출 잠금 뒤면 건너뜀).

종료 미팅(B10):

1. 작업 종료 트랜잭션에서 지금처럼 집계 사실을 고정하고, 커밋 뒤 `POST /v1/report/form-ending`(`request_id = rpt-<report_id>-end`).
2. 같은 행을 `FOR UPDATE` 로 잠그고 `form.review.ending` 이 비어 있을 때만 채우고 `form_revision + 1`. **새 버전을 만들지 않는다**(지금의 `WorkWrapUpRepository.prepare` 새 버전 경로를 날짜 작업에서는 쓰지 않음).
3. 실패하면 지금 `wrapUpFallback` 과 같은 결정적 문장을 서버가 채운다(컨테이너는 LLM 실패도 200 대체 문장으로 주므로, 이 경로는 연결 실패·500 때만).

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
