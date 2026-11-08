# EC2 터널 방식에서 Tier 0 엔드포인트 등록 포트 분리

S15P31A101-543. 명세 3.12(엔드포인트 등록, 막힌 망 경로), 12.2(I1 `tier0_endpoint`), 12.6(`GET /api/endpoints/tier0`), 12.7(I0 제어·영상 TLS), 14(TLS 공개키 고정), 계약 `contracts/messages/tier0_endpoint.schema.json`.

- 코드: `deploy/scripts/simai_bootstrap.py`(`_apply_lobby_port_env`, `prepare_lobby_work`), `ai/services/tier0/endpoint_registration.py`(`build_payload`), `ai/services/tier0/i1_publisher.py`(`EndpointI1Registration`), `ai/services/tier0/service.py`(`_build_link`, `status`)
- 시험: `ai/tests/deploy/test_simai_supervisor.py`, `ai/tests/services/tier0/test_endpoint_registration.py`

## 문제

11·12·13차 GPU 세션에서 게이트웨이(노트북)는 EC2 경유 SSH 터널로 `127.0.0.1:17000`(제어)·`127.0.0.1:17001`(영상 4대 공용)에 붙고, Pod 에서는 `simai-bootstrap lobby --public-host 127.0.0.1 --control-port 17000 --video-ports cam_A:17001,cam_B:17001,cam_C:17001,cam_D:17001 ...` 로 기동했다.
이때 `prepare_lobby_work` 가 `SIMAI_TIER0_CONTROL_PORT=17000`, `SIMAI_TIER0_VIDEO_PORTS=cam_A:17001,...` 처럼 듣는 포트(1~65535)를 그대로 대칭 매핑 요청 번호 자리에 넣어서, `endpoint_registration.build_payload` 의 `MIN_REQUESTED_PORT = 70000` 검증(`contracts/messages/tier0_endpoint.schema.json` 의 `requested_port >= 70000`)에서 매번 거부되고 EC2 의 `tier0_endpoint` 등록 기록(특히 새 세션의 `tls_public_key_sha256`)이 갱신되지 않았다.

## 설계 선택과 정한 값

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 계약(`tier0_endpoint.schema.json`) | 변경 없음 (`requested_port >= 70000`, `public_port` 1~65535 유지) | I1 LIVE 계약 불변 규칙 및 EC2·게이트웨이 기존 검증기 호환 |
| 듣는 포트(`1..65535`) 입력 시 요청 번호 자동 분리 | `control_port < 70000` 이면 제어 요청 번호는 `70000` (`SIMAI_TIER0_CONTROL_PORT=70000`, `SIMAI_PUBLIC_PORT_70000=<control_port>`), 영상 요청 번호는 고유 듣는 포트 등장 순서대로 `70001, 70002, ...` (`SIMAI_PUBLIC_PORT_7000x=<listen_port>`, 같은 듣는 포트를 쓰는 카메라는 같은 요청 번호) | 13차 lobby 인자를 바꾸지 않고 그대로 실행해도 10-31 EC2 정상 기록(`public_ip=127.0.0.1`, CONTROL `70000->17000`, VIDEO `cam_A~D` `70001->17001`)과 동일한 페이로드를 만들고, Tier 0 의 `_control`·`listen_specs_from_env` 도 `SIMAI_PUBLIC_PORT_70000/70001` 에서 실제 듣는 포트 `17000/17001` 을 읽는다 |
| 대칭 매핑 구성(`>= 70000`) 유지 | `control_port >= 70000` 이면 요청 번호를 그대로 쓰고 `SIMAI_PUBLIC_PORT_<req>=<req>` 를 넣지 않는다 | `70000` 은 TCP 포트(1~65535)가 아니므로 `SIMAI_PUBLIC_PORT_70000=70000` 을 기본값으로 넣으면 `RUNPOD_TCP_PORT_70000` 을 가리고 범위 오류가 난다. 환경의 `SIMAI_PUBLIC_PORT_*` 또는 `RUNPOD_TCP_PORT_*` 를 그대로 따르게 한다 |
| 포트 중복·범위 오류 검증 | `prepare_lobby_work` 와 `endpoint_registration.build_payload` 양쪽에서 (1) 제어·영상 공개(듣는) 포트 중복, (2) 서로 다른 요청 번호가 같은 공개 포트를 가리키는 중복, (3) 아바타 포트와 제어·영상 포트 중복, (4) `65536..69999` 또는 `<= 0` 잘못된 포트 번호, (5) 듣는 포트와 요청 번호 혼용을 거부 | 기동 전(`EXIT_USAGE=64`)과 엔드포인트 등록 시점(`EndpointConfigError`) 모두에서 잘못된 포트 구성을 즉시 드러낸다 |
| 시작 로그 및 `Tier0Service.status()` | `_build_link` 기동 시점에 `EndpointRegistrar.build` 준비 가능 여부를 로그로 남기고, I1 연결 시 `EndpointI1Registration.register` 성공(`INFO`)·실패(`ERROR`) 로그와 `status()["endpoint_registration"]`(`state`, `registered`, `failures`, `last_error`, `registered_at`, `public_ip`, `ports`)을 기록 | `tier0.live` 표시 파일·`pod.jsonl`·기동 로그만 보고도 엔드포인트 등록 성공 여부와 실패 사유를 바로 확인할 수 있다 |

## 버린 대안

1. **`70000 + (listen_port - control_port)` 오프셋 계산**: 영상 듣는 포트가 제어 포트보다 작은 구성(예: 제어 `17001`, 영상 `17000`)에서 요청 번호가 `69999 < 70000` 이 되어 계약 검증에 실패한다. 고유 영상 듣는 포트 순서대로 `70001, 70002, ...` 를 부여하면 포트 대소 관계와 무관하게 항상 `70001` 이상이고 `17000/17001` 입력에서도 동일하게 `70000/70001` 이 된다.
2. **lobby 에 별도 `--register-ports` 필수/전용 인자 추가**: 운영 스크립트와 13차 기동 명령을 모두 고쳐야 하고, 빠뜨렸을 때 다시 등록 누락이 재발한다. 기존 `--control-port`·`--video-ports` 에서 `< 70000`(듣는 포트)과 `>= 70000`(대칭 매핑 요청 번호)을 자동 판별하는 편이 가장 작은 변경이며 기존 호출과 완전히 호환된다.
3. **`contracts/messages/tier0_endpoint.schema.json` 의 `requested_port` 하한 완화**: I1 LIVE 계약 변경 금지 규칙에 어긋나며 EC2 서버·게이트웨이 검증기와 불일치가 생긴다.

## 남은 위험

- 게이트웨이(노트북)는 `tls_public_key_sha256` 을 고정(`--pin` 또는 설정)해 검증하므로, Pod 에서 새 TLS 키를 생성한 경우에는 게이트웨이 고정 지문도 함께 맞춰야 한다(번들 디렉터리의 기존 `tier0.crt`/`tier0.key` 를 재사용하면 지문이 유지되고, EC2 에는 매 세션 새 `registered_at` 으로 등록된다).
