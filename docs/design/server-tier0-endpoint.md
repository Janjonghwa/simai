# EC2 Tier 0 엔드포인트 등록·조회

S15P31A101-290. 명세 3.12(엔드포인트 등록), 12.6(`GET /api/endpoints/tier0`), 12.7, 14, FR-138, 계약 `tier0_endpoint` 1.x.
코드는 `server/.../domain/tier0/endpoint/`. 보내는 쪽은 `ai/services/tier0/endpoint_registration.py`, 받는 쪽은 노트북 `gateway/site_alert/.../endpoint.py`.

## 흐름

1. Tier 0 가 I1 로 `tier0_endpoint` 를 보낸다. EC2 는 계약 스키마 전체로 검사하고 `tier0_endpoint` 표에 한 행을 추가한다(덮어쓰지 않음).
2. 노트북이 기기 토큰으로 `GET /api/endpoints/tier0` 를 부르면 현재 엔드포인트를 Tier 0 가 보낸 봉투 모양 그대로 돌려준다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 검사 | 다른 I1 메시지(칸별 타입 검사)와 달리 `ContractSchemas` 로 계약 전체를 본다. 틀리면 저장하지 않고 `simai.i1.messages{result=invalid}` 로 센다(연결 유지) | 노트북에 그대로 전달되는 메시지라 정의되지 않은 필드(비밀 값)를 막아야 한다(계약 `additionalProperties: false`) |
| source_id | `payload.gateway_id` 가 봉투 `source_id` 와 다르면 계약 위반 | 계약 설명. 노트북도 다르면 접속하지 않는다 |
| 봉투 저장 | V8 로 `msg_id`(유일)·`schema_version`·`sent_at` 칸을 더한다. 조회는 칸에서 봉투를 다시 만든다(`source_id` = `gateway_id`, 받은 적 없는 선택 필드는 싣지 않음) | V1 은 payload 칸만 있다. 봉투의 모르는 필드는 계약상 무시라 싣지 않는다 |
| 재전송 | 같은 `msg_id` 는 한 행(duplicate) | 명세 12.1 |
| 현재 엔드포인트 | `registered_at` 이 가장 늦은 행, 같으면 `received_at`·`endpoint_id` 가 늦은 행 | 계약 설명(registered_at 기준). 늦게 도착한 옛 등록이 현재 주소를 바꾸지 않는다 |
| 게이트웨이 | 쿼리 파라미터 없음. 모든 `gateway_id` 중 최신 하나 | 노트북이 고르지 않고(endpoint.py), MVP 는 GPU 인스턴스 하나(3.12) |
| 등록 없음 | 404 + ProblemDetail `code=TIER0_ENDPOINT_NOT_REGISTERED` | 계약 설명, endpoint.py 가 404 를 '등록 없음'으로 보고 다시 조회한다 |

## 시험

`Tier0EndpointIntegrationTest`(실제 포트, I1 + REST): 계약 예시 왕복이 같음·계약 검사 통과, 최신 선택(늦게 온 옛 등록, 같은 registered_at),
재전송, 계약 위반 예시 4종·gateway_id 불일치·읽을 수 없는 메이저를 버리고 연결 유지, 등록 없음 404, 토큰 없음·틀린 토큰·사용자 JWT·Tier 0 토큰 401.

## 남은 일

- Tier 0 가 여럿이 되면 노트북이 gateway_id 를 고르는 방법(쿼리 파라미터 또는 기기별 배정)을 계약에 정한다.
