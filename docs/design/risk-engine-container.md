# EC2 Risk Engine 컨테이너 (S15P31A101-99)

EC2 Spring 이 TBM 보고서(F-02)를 만들 때 compose 내부망으로 부르는 PLANNED 판정 컨테이너다. Tier 0 와 같은 Risk Engine 라이브러리
(`simai_ai.risk`)·같은 규칙 등록부를 FastAPI 로 감싼다. 계약은 `contracts/messages/risk_plan_request`·`risk_plan_result` 1.0.0(-216)이다.

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 3.2 Risk Engine 배치 | 같은 코드·같은 규칙 버전을 Tier 0 와 EC2 컨테이너에 배포, Java 재구현 금지 | `ai/uv.lock`·`simai_ai` 를 그대로 설치, 판정은 `simai_ai.risk.plan` |
| 3.8 기술 스택 | EC2 컨테이너 내부 API 는 FastAPI(기본안) | FastAPI 0.142.4 + uvicorn 0.54.0 |
| 8.3 F-02 | 계획 작업 위치가 없으면 컨테이너가 작업면·구역에서 대표 위치를 만들고 GENERATED, ID `gen-` | 아래 "계획 작업 위치 생성" |
| 11.1 `sim.*` | 결과의 위치 목록을 Spring 이 저장 | 결과 `positions` 에 판정에 쓴 좌표(mm 반올림) 그대로 |
| 12.2 EC2 내부 | compose 내부망 HTTP/JSON, 공개 포트 없음, `POST /v1/plan/evaluate`, 알림 채널로 안 감 | compose `risk-engine` 서비스(internal 망), import-linter 계약 |
| 19.1 | 생성 규칙·FastAPI 구현 미결 | 이 문서로 닫는다(명세 v1.3 수정 22) |
| 21.2 | 계약 타입은 스키마에서 생성, 손으로 고치지 않음 | Pydantic 모델을 쓰지 않고 `contracts/` 스키마로 직접 검증 |
| 부록 D-G3-04 | Tier 0 와 EC2 컨테이너의 Risk Engine 동일성(공통 벡터) 불일치 0건 | 아래 "D-G3-04" |

## 코드·시험 위치

| 위치 | 내용 |
|---|---|
| `ai/src/simai_ai/risk/plan.py` | 스키마를 통과한 payload → 규칙(등록부)·구역·작업면·위치 → `RiskEngine.evaluate(..., Origin.PLANNED)` (`FixedClock(evaluation_time)`), 위치 생성, 항목 변환(holding 제외, metrics mm). 표준 라이브러리만(risk 격리 그대로) |
| `ai/services/risk_engine/handler.py` | 본문 → (HTTP 상태, 응답 바이트). 오류 판정 순서 1~3단계(JSON·request_id·출처·스키마)와 4~6단계 예외 대응, 응답 계약 검사. FastAPI 없이 부를 수 있다 |
| `ai/services/risk_engine/app.py`, `__main__.py` | FastAPI 앱(`POST /v1/plan/evaluate`, `GET /healthz`, 문서 경로 끔), `python -m services.risk_engine` |
| `deploy/risk-engine/Dockerfile`, `Dockerfile.dockerignore` | CPU 이미지(python:3.11.15-slim-trixie, extra `risk_api` 만) |
| `deploy/compose.yaml` `risk-engine`, `deploy/docker-bake.hcl` `risk-engine` | 내부망 서비스, 빌드 대상 |
| `deploy/scripts/check_image.sh` (`SIMAI_IMAGE_KIND=risk_engine`), `risk_plan_roundtrip.py`, `risk_engine_g304.sh` | 이미지 점검, 계약 예시 왕복, D-G3-04 두 이미지 대조 |
| `ai/tests/risk/test_plan_positions.py`, `ai/tests/services/risk_engine/test_api.py`, `ai/tests/deploy/test_risk_plan_roundtrip.py`, `ai/tests/architecture/test_import_rules.py` | 시험 |

## 요청 처리

1. 본문 읽기: 4 MiB 초과, JSON 아님(NaN·Infinity·`1e400` 처럼 유한하지 않은 수, 깊은 중첩의 RecursionError 포함), 객체 아님, `payload.request_id` 없음·UUID 아님 → INVALID_REQUEST(`request_id` null).
   4 MiB 상한은 받는 중에 센다: Content-Length 가 상한을 넘으면 읽기 전에, 헤더가 없거나 거짓이면 실제로 받은 바이트가 넘는 순간 나머지 청크를 읽지 않고 거절한다.
   NaN 을 받아들이면 엔진 안에서 예외가 나 ENGINE_ERROR(재시도 가능)로 잘못 분류되므로 읽기 단계에서 막는다.
2. `payload.source` 가 있고 PLANNED 아님 → SOURCE_NOT_PLANNED. 3. `risk_plan_request` 스키마 위반 → INVALID_REQUEST(첫 위반 하나를 message 에).
4. 규칙 묶음(`plan.build_rules`): 같은 `rule_id` 둘, 등록부에 없는 (ID, 버전) → UNKNOWN_RULE(422).
5. 레이아웃(`plan.read_layout`): `Zone.from_json`·`WorkSurface.from_json` 거부, (zone_id, zone_version)·surface_id·position_id 중복,
   판정 시각에 유효한 같은 zone_id 둘, 모르는 support_surface_id → INVALID_LAYOUT. 정수 값 실수 시각(`1.0` 형태, 스키마 integer 가 받음)은 정수로 맞춘다.
6. 판정 뒤 예외 → ENGINE_ERROR(500, retryable true). message 는 예외 이름만(경로·값은 로그에만).

응답은 보내기 전에 `risk_plan_result` 스키마로 검사하고 `allow_nan=False` 로 직렬화한다. 맞지 않으면(규칙이 NaN·확률 이름 metrics 를 낸 경우 등)
ENGINE_ERROR 로 바꾸고, 그 ENGINE_ERROR 도 계약 문서로 만들 수 없으면(request_id 를 모르는 내부 오류, 스키마 파일 없음) 본문 없는 500 을 보낸다
(계약 '본문 없는 실패': Spring 이 다시 보낼 수 있는 실패). 계약 밖 risk_plan_result 는 보내지 않는다. 계약 스키마 파일이 없으면 기동에서 실패한다.
같은 요청은 같은 결과다(계산만 하고 저장하지 않음). 요청마다 엔진·규칙을 새로 만들어 동시 요청이 상태를 나누지 않는다(R4 기억 없음).

`GET /healthz`: `engine_version`(simai-ai 패키지 버전), `rules`(등록부의 (ID, 버전)), `registry_digest`(규칙 등록부 설정값
`rule_catalog.catalog()` 의 sha256. Tier 0 와 같은 등록부인지 한 값으로 대조), `position_rule`(생성 규칙 식별자).

## 계획 작업 위치 생성 (`plan-positions-1`)

요청에 `work_positions` 가 없거나 빈 배열일 때만 만든다. 하나라도 있으면 그 위치만 쓴다(REQUESTED 와 섞지 않음).

| 대상 | 위치 | 수치 | 근거(규칙 정의의 거리) |
|---|---|---|---|
| 작업면마다 | 상판 가운데, 서 있는 면 = 그 작업면 | — | R3(서 있는 면 높이차) 판정 대상. 좁은 면은 가운데도 R2 발생 거리 안 |
| 넓은 작업면 | 가장 긴 변 가운데에서 안쪽 `SURFACE_EDGE_INSET_M` = 0.5 m (정사각형은 footprint 첫 변) | 조건: 짧은 변 절반 − 0.3 > 1.0 m | 가운데가 R2 발생 거리(1.0 m, 점유 반경 0.3 m 뺀 간격) 밖이면 가장자리 위험이 항목에 안 나온다. 0.5 m 안쪽은 간격 0.2 m 로 발생 거리 안(한 걸음) |
| 유효한 개구부(`zone_type` OPENING) | 바닥, 가장 긴 변 가운데에서 바깥 `OPENING_OFFSET_M` = 0.2 m | 그 점이 다각형 안(오목)이면 다음으로 긴 변, 맞는 변이 없으면 만들지 않음 | 점유 반경 0.3 m 보다 0.1 m 짧아 R1 발생(`xy_uncertainty_overlap`, zone_distance 0.2 m). 개구부(바닥 구멍)는 R2 등록 형식이 없어 R1 로만 드러난다 |

- 순서: 작업면(surface_id 순) → 구역((zone_id, zone_version) 순). ID `gen-<surface_id 또는 zone_id>-<두 자리 순번>`. 작업면과 구역이 같은 ID 면 순번을 잇는다
  (`gen-x-01`, `-02`, `-03`). 끝이 고정 길이(`-NN`)라 서로 다른 ID 에서 같은 문자열이 나오지 않는다. 요청 ID 는 스키마가 `gen-` 을 막는다.
- 좌표는 mm 로 반올림한 값으로 판정하고 그 값을 싣는다. Spring 이 결과 위치로 다시 계산하려면 요청 스키마가 `gen-` ID 를 막으므로 계획 ID 로 바꿔
  REQUESTED 로 보낸다(같은 좌표·같은 판정, 항목의 position_ids 만 바뀜, 시험). 같은 레이아웃·판정 시각이면 입력 순서와 상관없이 같다.
- 머리 높이는 주지 않는다(모르면 비운다, 규칙 2). VOLUME_3D 구역에 걸리면 확인 필요 `Z_UNKNOWN` 이다.
- 위치는 규칙 묶음과 무관하게 레이아웃만으로 정한다(운영·교실 시험 프로파일이 같은 위치를 본다. 높이 기준은 규칙이 판단).
- 만들 작업면·개구부가 없으면 `positions` 가 빈 배열이고 항목도 없다. 빈 `positions` 가 그 표시다.
  Spring·보고서는 이것을 '평가한 작업 위치 없음'으로 보이고 '위험 없음'으로 쓰지 않는다(필요하면 관리자가 위치를 준다). 계약 `risk_plan_result` 1.1.0(-217)이
  이 뜻을 명시하는 선택 표시 `no_evaluated_positions`(true ⇔ 빈 `positions`)를 더했다. 이 컨테이너는 아직 1.0.0 응답이라 싣지 않는다(채우는 것은 남은 일, 명세 19.1).
- 계약 예시 `risk_plan_result.generated_positions.json`(가운데 `gen-scaffold-1-01`, 개구부 `gen-z-open-1-01` (2.5, 0.8))은 이 규칙의 출력과 같다(시험).

## 이미지·compose

- 베이스 `python:3.11.15-slim-trixie@sha256:9074…`(Tier 0 와 같은 Python 판, CPU), uv 0.11.27 로 `uv sync --locked --no-dev --extra risk_api`.
  `risk_api` = fastapi `>=0.142.4,<0.143`, starlette `>=1.7,<1.8`, uvicorn `>=0.54,<0.55`, jsonschema. 셋 다 올린 지 2주가 지난 판(2026-10-22 기준 최신
  fastapi 0.157.0 은 하루 전·하루 다섯 판이 나와 고르지 않았다). 올릴 때 MR 에 적는다.
- 크기 225 MB(노트북 빌드). torch·numpy·opencv·websockets·Tier 0 코드 없음(점검이 확인). 비루트(uid 10001), EXPOSE 없음, 헬스체크 `/healthz`, CMD `0.0.0.0:8080`.
  이미지에 계약 스키마(예시 제외)와 공통 벡터(`/opt/simai/vectors`)를 싣는다.
- compose `risk-engine`: `ports` 없음, 망 `simai-ec2-internal`(internal: 밖으로 못 나감), 읽기 전용 루트, `cap_drop: ALL`, `no-new-privileges`. 볼륨·환경 변수·비밀 없음.
- Spring(-101): 같은 망에 붙어 `POST http://risk-engine:8080/v1/plan/evaluate`(본문 `risk_plan_request`, `Content-Type: application/json`). HTTP 상태보다 본문
  `status` 를 따르고 `retryable` true 만 같은 `request_id` 로 다시 보낸다. 연결 실패·시간 초과·비JSON 응답도 다시 보낼 수 있는 실패다(계약).

## D-G3-04 (공통 벡터 동일성)

API 계약(`risk_plan_request`)은 world_state 를 받지 않으므로(Tier 0 입력) 공통 벡터는 API 가 아니라 각 이미지 안 라이브러리로 돌리고, API 쪽은 계약 예시 PLANNED 요청으로 대조한다.

```sh
bash deploy/scripts/check_image.sh simai/risk-engine:$TAG                       # 이미지 안 벡터 53/53 + 망 없는 컨테이너 HTTP 왕복
bash deploy/scripts/risk_engine_g304.sh simai/risk-engine:$TAG simai/tier0:$TAG  # 두 이미지: 벡터 출력 같음 + API 응답 = Tier 0 라이브러리 응답
```

2026-10-22 노트북 실측: 이 티켓의 risk-engine 이미지와 로컬 Tier 0 이미지(`3926a87` 빌드, 21 GB. 그 뒤 `ai/src/simai_ai/risk`·벡터 변경 없음)의
벡터 출력이 같다(53/53). PLANNED 대조는 그 Tier 0 이미지에 이 티켓의 `plan.py`·`services/risk_engine`·계약을 임시로 얹어 돌렸고 5/5 같았다.
얹지 않은 대조(같은 커밋으로 빌드한 두 이미지)는 다음 Tier 0 이미지 빌드 때 `risk_engine_g304.sh` 로 한다(CI·GPU 세션). 두 이미지가 같은 `ai/uv.lock`·같은
`simai_ai` 코드를 설치하고 risk 는 표준 라이브러리만 쓰므로 Python 판(3.11.15, 빌드 출처는 다름: 공식 이미지 대 python-build-standalone) 밖의 차이가 없다.
벡터 metrics 는 1e-9 허용(libm 끝자리)이다.

## 버린 대안

- 요청·응답 Pydantic 모델: 손으로 쓰면 스키마와 어긋나고(21.2), FastAPI 자동 검증(422)은 계약의 오류 판정 순서(SOURCE_NOT_PLANNED 먼저, 400/422 구분)를 따르지 않는다.
- 모든 구역 유형(장비 주차·양중)에 위치 생성: 사람이 어디서 일할지 레이아웃으로 알 수 없고, 계약 예시(-216)가 개구부만 만든 형태다. 필요하면 관리자가 위치를 준다.
- 기본 머리 높이(예: 1.7 m)를 넣기: 지어낸 값으로 VOLUME_3D 판정을 확정하게 된다(규칙 2). Z_UNKNOWN 이 정직하다.
- 격자 표본(작업면·구역 둘레를 촘촘히): 같은 위험이 위치 수만큼 항목으로 늘어 보고서가 읽기 어렵고, 대표 위치로 규칙마다 한 번 드러나면 충분하다.
- Tier 0 CUDA 베이스 재사용: 21 GB 이미지가 CPU EC2 에 필요 없다. alpine(musl): 같은 잠금 휠과 libm 이 달라 D-G3-04 근거가 약해진다.
- jsonschema 를 risk 안에서: risk 는 서드파티 금지(격리)라 검증은 컨테이너(services)에서 한다.

## 잔여 위험

- 이미지는 linux/amd64 만(EC2 가 Graviton 이면 bake `platforms` 에 arm64 를 더해야 한다).
- 인증 없음: 공개 포트가 없고 internal 망이라 Spring 만 닿는다는 전제다. 망 구성을 바꾸면 다시 본다.
- 통신 금지는 import-linter 계약(지정 모듈·표준 socket·ssl·http·urllib·DB 클라이언트 이름)이 보장하는 범위까지다. 동적 import·새 클라이언트 이름은 잡지 못하고,
  실행 망 차단(compose internal)이 두 번째 겹이다.
- uvicorn 워커 1개(판정은 스레드 풀). 레이아웃 하나는 ms 단위라 MVP 부하에 충분하다고 보고 재지 않았다.
- 개구부 바깥 0.2 m 점이 다른 구역·작업면 footprint 안에 떨어질 수 있다(그 판정도 함께 나온다). 오목한 개구부에 맞는 변이 없으면 위치를 만들지 않는다(항목 없음).
- 계약 스키마 파일이 없는 배포는 기동에서 실패한다(요청을 받지 않음). 노트북에서 이전 Tier 0 이미지(-216 전 contracts/)로 대조하다 찾은 경우다.

## 넘긴 일

- Spring 호출·재시도·`sim.*` 저장(S15P31A101-101).
- 같은 커밋 두 이미지의 `risk_engine_g304.sh`(다음 Tier 0 이미지 빌드·CI). 레지스트리 푸시·EC2 배포는 사람이 확인한 뒤.
