# 분석 워커 tbm_report 작업 — 외부 LLM 초안·인용 검사 (S15P31A101-225, 호출 방식 S15P31A101-285)

명세 5.4 "LLM(텍스트 생성)", 8.3 F-02, 12.5, 14장. EC2 가 PLANNED 계산 결과(TBM) 또는 그날 이벤트 요약(DAILY)과 RAG 조항을 담아
`tbm_report` 작업을 등록하면, 분석 워커가 외부 LLM 으로 보고서 초안을 만들고 인용을 검사해 돌려준다. 본보기는 bev_floorplan(S15P31A101-239).

## 코드

| 위치 | 내용 |
|---|---|
| `contracts/messages/tbm_report.schema.json` | 입력(job_request 3.3.0)·결과(job_callback 2.3.0) 정의 |
| `ai/src/simai_ai/report/tbm.py` | 프롬프트(`PROMPT_VERSION`)·요청 조립·보내기 전 검사·인용 검사·재생성을 한곳에 |
| `ai/src/simai_ai/report/llm.py` | `ChatClient` Protocol, 구현 둘(OpenAI Responses API 기본·Chat Completions, 표준 라이브러리 urllib), 설정·오류 코드 |
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
| `LLM_BAD_RESPONSE` | 예 | 응답 형식 아님·내용 없음, Responses 의 status failed·incomplete·error·거부(refusal)만 있음 |
| `REPORT_DRAFT_REJECTED` | 아니오 | 인용·형식 검사로 두 번 버림 |
| `TBM_RESULT_INVALID` | 아니오 | 만든 결과가 계약 위반(워커 버그) |
| `CANCELLED` | 아니오 | 러너가 취소(시간 초과·대여 상실). 러너가 TIMED_OUT·반납으로 처리 |

작업 실행 상한(timeout_ms)·재시도(max_attempts)는 작업 대여 그대로다. LLM 응답 대기(SIMAI_LLM_TIMEOUT_S, 기본 60 s)는 두 번 부를 수 있게
timeout_ms 의 절반 아래로 둔다(EC2 가 tbm_report timeout_ms 를 정할 때 같이 본다).

## 설정 변수(Pod `.env`)

| 변수 | 값 |
|---|---|
| `SIMAI_LLM_BASE_URL` | https, `/v1` 까지. 클라이언트가 `/responses` 또는 `/chat/completions` 를 붙인다. 그 경로까지 넣으면 `LLM_CONFIG` |
| `SIMAI_LLM_MODEL` | 모델 이름 |
| `SIMAI_LLM_API_KEY` | 키(Authorization: Bearer) |
| `SIMAI_LLM_API_STYLE` | `responses`(비우면 기본) 또는 `chat`. 그 밖은 `LLM_CONFIG`. 결과에는 넣지 않고 워커 로그에만 남긴다 |
| `SIMAI_LLM_TIMEOUT_S` | 1~600, 기본 60 |
| `SIMAI_LLM_TEMPERATURE` | 선택 0~2, 없으면 요청에 싣지 않음 |
| `SIMAI_LLM_MAX_OUTPUT_TOKENS` | 선택(Responses 만) 1~100000, 없으면 싣지 않음. 추론 토큰을 포함한 상한이라 작으면 본문이 잘려 `LLM_BAD_RESPONSE` |

키는 `LlmSettings` repr·오류 메시지·결과에 나가지 않는다.

SSAFY GMS 프록시(2026-10-26 확인): `SIMAI_LLM_BASE_URL=https://gms.ssafy.io/gmsapi/api.openai.com/v1`, `SIMAI_LLM_MODEL=gpt-6.1-sol`.
이 모델은 temperature 기본값(1) 밖의 값을 400 으로 거부하므로 `SIMAI_LLM_TEMPERATURE` 를 두지 않는다.

## 호출 방식(S15P31A101-285)

기본은 GMS 안내문의 공식 방식인 OpenAI Responses API(`POST …/v1/responses`)다. Chat Completions(`POST …/v1/chat/completions`)는
`SIMAI_LLM_API_STYLE=chat` 으로 남긴다. 둘 다 `ChatClient` Protocol 이라 `tbm.py`(프롬프트·인용 검사·재생성)와 결과 계약은 같다.
HTTP 오류 → 실패 코드 매핑도 한 함수(`_post`)를 같이 쓴다.

Responses 요청·응답(형식 근거: openai-python SDK 의 `types/responses` 정의. platform.openai.com 문서는 자동 조회가 막혀 SDK 로 봤고,
아래 필드는 GMS 실제 호출로 확인했다):

- 요청: `model`, `input`(메시지를 role·content 그대로, system 포함), `text: {"format": {"type": "json_object"}}`, `store: false`,
  설정이 있을 때만 `temperature`·`max_output_tokens`.
- system 을 `instructions` 로 빼지 않는다: `json_object` 는 **input 메시지**에 'json' 이라는 낱말이 있어야 받고 `instructions` 는 그 검사에 들지 않아
  400 으로 거부됐다("Response input messages must contain the word 'json' …"). 프롬프트(system)에는 'JSON' 이 있으므로 input 에 두면 된다.
- `store: false`: 보고서 초안을 제공사 쪽에 남겨 다시 읽을 일이 없다(Responses 는 기본이 저장). GMS 는 받아들이고 응답에 `store: false` 를 돌려줬다.
- 응답: `output[]` 의 `type: "message"` 항목 `content[]` 중 `type: "output_text"` 의 `text` 를 이어 붙인다. reasoning 등 다른 항목은 버린다.
  `output` 이 없고 맨 위 `output_text` 문자열만 있으면 그것을 쓴다(SDK 편의 필드를 그대로 주는 중계 대비).
  토큰은 `usage.input_tokens`·`usage.output_tokens`, 모델은 `model`(없으면 요청 모델).
- `status` 가 `completed` 가 아니거나(`failed`·`incomplete`·`in_progress` 등) `error` 가 null 이 아니면 `LLM_BAD_RESPONSE`(재시도 가능).
  `incomplete`(출력 상한·내용 필터로 잘림)는 잘린 JSON 을 쓰지 않으려고 형식 오류로 본다. `failed` 는 제공사 쪽 실패라 다음 대여에서 다시 해 볼 만하다.
  본문 없이 `refusal` 만 있어도 같다(필드 이름 `refusal` 은 SDK 기준, GMS 에서 거부 응답은 보지 못함).

실제 GMS 확인(2026-10-26, 아래 명령, `job_request.tbm_report.json` 예시 입력, 키는 출력하지 않음):

| 방식 | attempts | 응답 시간 | 토큰(입력/출력) | 인용 검사 | 등급 | 결과 계약 오류 |
|---|---|---|---|---|---|---|
| responses(기본) | 1 | 23.8 s | 1222 / 1616 | 4개 모두 입력 안 | 그대로 | 0 |
| chat | 1 | 20.6 s | 1222 / 1584 | 4개 모두 입력 안 | 그대로 | 0 |

처음 responses 호출은 system 을 `instructions` 로 보내 400(위 'json' 낱말 조건)이었고, input 에 두도록 고친 뒤 위 결과다.
GMS 응답의 message 항목 키는 `content·id·phase·role·status·type`, output_text 키는 `annotations·logprobs·text·type`, `usage` 에
`input_tokens_details`·`output_tokens_details`·`total_tokens` 도 있다. 이 짧은 호출에서는 reasoning 항목이 output 에 오지 않았다.

## 실제 LLM 한 번 확인(키·제공사를 받은 뒤 수동)

```sh
cd ai
# .env 에 SIMAI_LLM_* 를 넣은 뒤. 결과는 화면에만 나온다(파일·저장소에 남기지 않음)
# Chat Completions 로 보려면 앞에 SIMAI_LLM_API_STYLE=chat 을 붙인다(.env 는 그대로)
uv run --env-file ../.env python -c "
import json, time
from services.worker.tbm_report import client_from_env
from simai_ai.report.tbm import generate_report
inp = json.load(open('../contracts/examples/job_request.tbm_report.json', encoding='utf-8'))['payload']['input']
print(json.dumps(generate_report(inp, client_from_env(), now_ms=lambda: time.time_ns() // 10**6), ensure_ascii=False, indent=2))"
```

## 남은 미결

- 19.1 "외부 LLM 호출 방식" 항목은 이 문서의 Responses 기본 전환으로 닫을 수 있다(다음 명세 정리에서).
- 프롬프트 품질(문장)은 실제 LLM 으로 확인 전이다. 프롬프트를 바꾸면 `PROMPT_VERSION` 을 올린다.
- 본문의 조항 번호 검사는 보수적이다: LLM 이 입력 조항 본문에 적힌 다른 조(예: '제42조제1항에 따른')를 그대로 옮겨 써도 그 조가 입력에 없으면 버린다.
- 11.5 동의서의 국외 이전 문구는 이 스토리 밖이다.
