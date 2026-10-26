# 분석 워커 tbm_report 작업 — 외부 LLM 초안·인용 검사 (S15P31A101-225)

명세 5.4 "LLM(텍스트 생성)", 8.3 F-02, 12.5, 14장. EC2 가 PLANNED 계산 결과(TBM) 또는 그날 이벤트 요약(DAILY)과 RAG 조항을 담아
`tbm_report` 작업을 등록하면, 분석 워커가 외부 LLM 으로 보고서 초안을 만들고 인용을 검사해 돌려준다. 본보기는 bev_floorplan(S15P31A101-239).

## 코드

| 위치 | 내용 |
|---|---|
| `contracts/messages/tbm_report.schema.json` | 입력(job_request 3.3.0)·결과(job_callback 2.3.0) 정의 |
| `ai/src/simai_ai/report/tbm.py` | 프롬프트(`PROMPT_VERSION`)·요청 조립·보내기 전 검사·인용 검사·재생성을 한곳에 |
| `ai/src/simai_ai/report/llm.py` | `ChatClient` Protocol, OpenAI 호환 chat completions 기본 구현(표준 라이브러리 urllib), 설정·오류 코드 |
| `ai/services/worker/tbm_report.py` | 처리기(입력·결과 계약 검사, 오류 → JobFailed) |
| `ai/services/worker/__main__.py` | `tbm_report` 늘 등록(설정은 작업마다 읽음) |

## 계약 요약

- **입력:** `report_kind`(TBM·DAILY), `report_id`, `site_id`, `report_date`(YYYY-MM-DD), `clauses[]`(1~50, `clause_id` 는 27 ID 규칙, `text`).
  TBM 은 `plan`(risk_plan_result OK 의 layout·evaluation_time·rules_version·`items`(216 `risk_item` 그대로)·`positions`·`no_evaluated_positions`),
  DAILY 는 `events[]`(event_id·규칙·등급·구역·발생·해소 시각·해소 방식·조치 상태). 선택 `options`(language ko, max_chars).
  모든 객체가 닫혀 있어 이미지·객체 주소·사람 이름 필드를 둘 수 없다. 사람은 World ID 도 싣지 않는다.
- **결과:** `sections[]`(SUMMARY·RISK_ITEM(item_index)·EVENT_REVIEW·RECOMMENDATION, 문단마다 `citations`), `risk_items`(입력 등급 그대로의 계산 근거 표),
  `citations`(전체, 처음 나온 순서), `generator`(model·prompt_version·attempts·토큰 수), `created_at`. 확률 필드 없음.
- 인용이 입력 조항 안인지는 스키마로 표현할 수 없어(메시지가 다름) 워커가 검사한다. 스키마는 ID 형식만 본다.

## 보내는 것(텍스트만)

- LLM 메시지는 system(규칙·출력 형식)과 user(사실 JSON 텍스트) 두 개다. 사실에는 보고 종류·날짜·조항(ID·본문)·위험 항목(번호·규칙·등급·구역·
  위치 ID·수치·근거 코드·확인 필요 이유)·위치(ID·지지면·출처) 또는 이벤트(규칙·등급·구역·시각·해소·조치 상태)만 넣는다. site_id·report_id·
  event_id·좌표는 넣지 않는다.
- 보내기 전 `assert_text_only`: 메시지마다 role·content 두 칸, content 는 문자열, `s3://`·`http(s)://`·`data:`·`file://` 이 없어야 한다.
  어기면 보내지 않고 입력 오류로 끝낸다(조항 본문에 주소가 섞인 경우 등).

## 인용 규칙과 재생성

- 응답은 JSON 객체(`response_format: json_object`, sections[].paragraphs[].citations)로 받는다. 본문 정규식만으로 뽑으면 근거가 어느 문단인지
  잃고 표기 흔들림에 약하다. 대신 본문 글자에서도 `제N조[의M][#k]` 표기를 찾아 입력에 있는 조인지 함께 본다(구조 필드를 빼먹은 인용도 잡는다).
- 허용: 입력 clause_id 와 같음. 조 단위 인용은 입력에 그 조(조 단위·그 조의 청크)가 있으면 허용. 청크 인용은 그 청크가 입력에 있어야 한다
  (입력이 조 단위여도 다른 청크 번호를 지어낼 수 있다). 256 의 legal_basis 는 조 단위, 27 의 RAG 는 청크 단위라 둘 다 맞게 된다.
- 그 밖에 버리는 초안: JSON 아님, 정해진 키 밖(severity 등), 섹션 종류가 그 보고서용이 아님, item_index 범위 밖, 같은 인용 두 번, 확률·퍼센트,
  max_chars(기본 6000) 초과.
- 버리면 같은 작업 안에서 **한 번만** 다시 만든다(사유를 user 메시지로 덧붙임). 두 번째도 버리면 재시도 불가 `REPORT_DRAFT_REJECTED`.
  같은 입력으로 새 대여를 받아도 같은 모델이 같은 실수를 할 가능성이 크고 외부 API 비용만 늘어서다. 사람이 입력(조항 검색)을 고쳐 다시 요청한다.
- 위험 등급은 입력 plan.items 를 그대로 결과 `risk_items` 로 옮긴다. LLM 이 낸 값은 결과의 등급 칸에 들어갈 길이 없다.

## 실패 코드

| code | retryable | 언제 |
|---|---|---|
| `TBM_INPUT_INVALID` | 아니오 | 입력 계약 위반(3.1.0 형식 포함), 보내기 전 검사 위반 |
| `LLM_CONFIG` | 아니오 | SIMAI_LLM_* 없음·잘못됨(호출 전) |
| `LLM_AUTH` | 아니오 | 401·403 |
| `LLM_REQUEST_REJECTED` | 아니오 | 그 밖의 4xx |
| `LLM_TIMEOUT` | 예 | 응답 대기 상한 초과 |
| `LLM_UNAVAILABLE` | 예 | 연결 실패·429·5xx |
| `LLM_BAD_RESPONSE` | 예 | chat completions 형식 아님·내용 없음 |
| `REPORT_DRAFT_REJECTED` | 아니오 | 인용·형식 검사로 두 번 버림 |
| `TBM_RESULT_INVALID` | 아니오 | 만든 결과가 계약 위반(워커 버그) |
| `CANCELLED` | 아니오 | 러너가 취소(시간 초과·대여 상실). 러너가 TIMED_OUT·반납으로 처리 |

작업 실행 상한(timeout_ms)·재시도(max_attempts)는 작업 대여 그대로다. LLM 응답 대기(SIMAI_LLM_TIMEOUT_S, 기본 60 s)는 두 번 부를 수 있게
timeout_ms 의 절반 아래로 둔다(EC2 가 tbm_report timeout_ms 를 정할 때 같이 본다).

## 설정 변수(Pod `.env`)

`SIMAI_LLM_BASE_URL`(https, OpenAI 호환 `/chat/completions` 의 앞부분), `SIMAI_LLM_MODEL`, `SIMAI_LLM_API_KEY`, `SIMAI_LLM_TIMEOUT_S`(1~600, 기본 60),
`SIMAI_LLM_TEMPERATURE`(선택 0~2, 없으면 요청에 싣지 않음). 키는 `LlmSettings` repr·오류 메시지·결과에 나가지 않는다.

SSAFY GMS 프록시(2026-10-26 확인): `SIMAI_LLM_BASE_URL=https://gms.ssafy.io/gmsapi/api.openai.com/v1` (끝에 `/responses`·`/chat/completions` 를 붙이지 않는다,
클라이언트가 `/chat/completions` 를 붙인다), `SIMAI_LLM_MODEL=gpt-6.1-sol`. 이 모델은 temperature 기본값(1) 밖의 값을 400 으로 거부하므로
`SIMAI_LLM_TEMPERATURE` 를 두지 않는다.

## 실제 LLM 한 번 확인(키·제공사를 받은 뒤 수동)

```sh
cd ai
# .env 에 SIMAI_LLM_* 를 넣은 뒤. 결과는 화면에만 나온다(파일·저장소에 남기지 않음)
uv run --env-file ../.env python -c "
import json, time
from services.worker.tbm_report import client_from_env
from simai_ai.report.tbm import generate_report
inp = json.load(open('../contracts/examples/job_request.tbm_report.json', encoding='utf-8'))['payload']['input']
print(json.dumps(generate_report(inp, client_from_env(), now_ms=lambda: time.time_ns() // 10**6), ensure_ascii=False, indent=2))"
```

## 남은 미결

- 19.1: 외부 LLM "GPT 6.1 sol" 의 제공사와 호출 설정(출력 길이·응답 시간). OpenAI 호환이 아니면 `ChatClient` 구현을 하나 더한다.
- 프롬프트 품질(문장)은 실제 LLM 으로 확인 전이다. 프롬프트를 바꾸면 `PROMPT_VERSION` 을 올린다.
- 본문의 조항 번호 검사는 보수적이다: LLM 이 입력 조항 본문에 적힌 다른 조(예: '제42조제1항에 따른')를 그대로 옮겨 써도 그 조가 입력에 없으면 버린다.
- 11.5 동의서의 국외 이전 문구는 이 스토리 밖이다.
