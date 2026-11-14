# S15P31A101-699 GPU 세션 등록(서버): 부팅 비밀값 하나로 세션 토큰·기동 설정 받기

관련 명세: 3.12(GPU 실행 사이트, "한 시점에 GPU 인스턴스는 하나"), 12.2(I1·I5), 12.5(워커 토큰), 12.6(권한), 14(비밀 값·토큰).
사용자 결정 2026-11-14: 팀원 누구나 GPU 를 켜면(나중에는 관리자 화면 버튼) 사람 손 없이 세션이 준비된다. 공급자는 RunPod 먼저, 나중에
AWS 서울 GPU 도 같은 경로. 이 티켓은 그 첫 단계(서버)다. 뒤 티켓: -700 GPU 자동 부팅, -701 게이트웨이 자동 전환,
-702 공급자 추상화·RunPod·화면 켜기/끄기·자동 정지, -703 AWS 뼈대.
계약 묶음 v7.53.0: `messages/gpu_session_register`·`gpu_session_register_result`·`gpu_session_report` 1.0.0. DB V75.

## 지금 손으로 하는 일 → 이 티켓 뒤

| 지금(Lead) | 이 티켓 뒤(서버가 함) |
|---|---|
| `worker-token.sh issue`: EC2 에 SSH → `admin worker rotate`(폐기면 reactivate) → 토큰 원문을 노트북 파일로 → Pod 로 복사 | 등록 응답 `worker.token`(부팅 비밀값에 묶인 워커를 `reissue`) |
| `prep-pod.sh`: 노트북의 고정 Tier 0 토큰 파일(`ec2/tier0-token.txt`) 복사 | 등록 응답 `tier0.token`(세션마다 새로 발급, EC2 는 해시만) |
| `prep-pod.sh`: 시작 묶음 `lobby-bundle/calibration.json` | `start.config.calibration`(가장 최근 구성 묶음의 보정 서명 주소 + sha256) |
| `lobby-bundle/laptop_token.sha256` | `start.laptop_token_sha256`(폐기 안 된 LAPTOP 기기 토큰 해시 목록) |
| `lobby-bundle/tls/tier0.{crt,key}` | **서버가 주지 않는다**(아래 "TLS 키") |
| `start-lobby.sh` 인자 `--ec2-base-url`·`--camera-ids`·`--measured-heights`·`--scene-version`·`--config-version`·`--gateway-id` | `start.ec2_base_url`·`camera_ids`·`measured_heights_m`·`scene_version`·`config.config_version`·`tier0.gateway_id` |
| `--control-port`·`--video-ports`·`--public-host` | 인스턴스가 안다(서버는 등록 때 받은 값을 기록만) |
| 세션 끝 `worker-token.sh revoke` | 종료 보고·밀림·무응답·부팅 비밀값 폐기 때 서버가 폐기 |

## API·표·토큰 흐름

```
[공급자 비밀 저장소] --부팅 비밀값--> GPU 인스턴스
GPU 인스턴스 --POST /api/gpu/sessions (Bearer 부팅 비밀값, gpu_session_register)--> EC2
   EC2: advisory 잠금 → 열린 세션 SUPERSEDED 로 끝냄(Tier 0 토큰 무효·워커 토큰 폐기·커밋 뒤 I1 끊기)
        → 묶인 워커 reissue → Tier 0 토큰 새로 → gpu_session·REGISTERED 기록
   <-- 201 gpu_session_register_result (워커·Tier 0 토큰 원문 한 번, start, report 간격)
GPU 인스턴스 --POST /api/gpu/sessions/{id}/reports (Bearer 부팅 비밀값, gpu_session_report)--> 204 | 409 GPU_SESSION_ENDED
Tier 0 --/ws/tier0 (Bearer 세션 Tier 0 토큰)--> I1 (principal gpu-session:<id>)
워커 --/api/worker/** (Bearer 세션 워커 토큰)--> I5
관리 화면 --GET /api/gpu/sessions/current (관리자 JWT)--> 가장 최근 세션 상태
```

- 표(V75, 추가만): `gpu_boot_secret`(부팅 비밀값 해시·묶인 워커, 기기·워커 표처럼 회전·폐기 칸만 바뀐다), `gpu_session`(등록 사실 한 번,
  Tier 0 토큰 해시), `gpu_session_event`(REGISTERED 1행·PROGRESS 여러 행·ENDED 1행, 부분 UNIQUE 로 끝은 한 번). 지금 상태는 마지막
  PROGRESS 의 단계, 끝났는지는 ENDED 행 유무, 마지막 보고 시각은 이벤트 `received_at`(EC2 시계) 최댓값.
- 관리 명령: `admin gpu-boot register --id <ID> --worker-id <워커 ID>`(워커는 먼저 `admin worker register` 로 작업 유형과 함께),
  `rotate`, `revoke`(그 비밀값으로 연 열린 세션도 BOOT_SECRET_REVOKED 로 끝냄), `reactivate`. 원문은 한 번만 출력한다.
- 상태 조회 `GET /api/gpu/sessions/current`(관리자, 계약 밖): `{session: null | {session_id, state ACTIVE|ENDED, stage REGISTERED|WEIGHTS|…,
  stage_reason_code, stage_detail, stage_progress, stage_at_ms, provider, instance_id, gpu_type, gpu_count, region, public_host, ports,
  image_digest, price_per_hour_usd, tls_public_key_sha256, worker_id, gateway_id, registered_at_ms, last_report_at_ms, ended_at_ms,
  end_reason, end_reason_code, elapsed_ms, tier0_link CONNECTED|DISCONNECTED, report_interval_ms, stale_after_ms}, server_time_ms}`.
  `tier0_link` 는 그 세션 Tier 0 토큰으로 연 I1 연결이 지금 열려 있는지다(환경 변수 토큰 연결은 세지 않는다).

## 정한 수치와 근거

| 값 | 기본 | 근거 |
|---|---|---|
| `simai.gpu-session.report-interval` | 60 s | 단계 전환이 분 단위(가중치·엔진 빌드)라 1분이면 화면 갱신에 충분하고 EC2 부하가 없다 |
| `simai.gpu-session.stale-after` | 10 min | 보고 10번을 놓쳐야 끝난다. EC2 재배포(약 1~2분, `deploy/ec2/deploy.sh`)·짧은 망 끊김을 견디고, 죽은 인스턴스의 토큰이 살아 있는 시간을 10분으로 묶는다. 무응답 판정은 EC2 수신 시각으로(인스턴스 시계 차이 무관) |
| 무응답 예외 | 그 세션 I1 연결이 열려 있으면 끝내지 않음 | 보고기만 죽고 Tier 0 는 관제 중일 수 있다. 관제를 끊는 쪽이 더 위험하다(규칙 5·명세 3.2) |
| `simai.gpu-session.sweep-interval` | 30 s | 무응답 판정 오차를 stale-after 의 5% 안으로 |
| `simai.gpu-session.gateway-id` | `tier0-lobby-s9`(`SIMAI_GPU_TIER0_GATEWAY_ID`) | `simai-bootstrap lobby` 기본값과 같다. I1 저널 이어 받기가 gateway_id 에 묶여 세션이 바뀌어도 같아야 한다 |
| 토큰 | 무작위 256비트 Base64url 43자 | 기존 `TokenHashes.newToken`(소금 없는 SHA-256 저장으로 충분) |

## 보안

**부팅 비밀값이 새면(영향 범위).** 할 수 있는 일은 (1) 새 GPU 세션 등록 → 묶인 워커 하나의 새 토큰과 Tier 0 토큰을 받음, (2) 그 비밀값이 연
세션의 진행·종료 보고뿐이다. 즉 가짜 Tier 0 로 I1 에 붙어 world_state·이벤트를 넣거나, 그 워커 권한으로 작업을 가져갈 수 있다(지금 고정 Tier 0
토큰·워커 토큰이 샐 때와 같은 범위). 또 진짜 세션을 밀어내(SUPERSEDED) 관제를 끊을 수 있다(가용성). 할 수 없는 일: 사용자 API·관리 API·
기기(노트북) 경로·다른 워커·업로드 서명(부팅 비밀값 자체로는)·TLS 비밀 키·노트북 기기 토큰 원문(해시만 받음)·HF 토큰.
대응: `admin gpu-boot revoke` 한 번으로 비밀값 폐기 + 그 비밀값이 연 세션 종료(토큰 폐기·I1 끊기). 그 뒤 `register` 로 새 ID 발급.

**최소 권한.** 부팅 비밀값은 `MachineKind.GPU_BOOT` 이고 `gpuBootFilterChain` 이 `POST /api/gpu/sessions`·`POST /api/gpu/sessions/*/reports`
두 경로에서만 본다. 다른 체인은 이 종류를 모르므로 401 이다(시험 `부팅_비밀값으로는_등록과_보고_말고_다른_API_를_쓰지_못한다`).
반대로 Tier 0·워커 토큰·관리자 JWT 로는 등록하지 못한다. 보고는 같은 부팅 비밀값이 연 세션에만(다른 비밀값이면 404).

**비밀 값 흐름.** 서버는 부팅 비밀값·Tier 0 토큰·워커 토큰 모두 SHA-256 해시만 둔다. 원문은 관리 명령 출력(부팅 비밀값)과 등록 응답
(세션 토큰)에서 한 번만 나온다. 등록 응답은 `Cache-Control: no-store` 이고 서버는 요청·응답 본문을 로그에 남기지 않는다(로그는 세션 ID·
부팅 비밀값 ID·워커 ID 만, `Registered.toString` 도 토큰을 빼고). 등록 요청 payload 는 닫혀 HF 토큰 같은 비밀 값을 실으면 400 이다.
노트북 기기 토큰은 원문이 EC2 에 없고 해시만 준다(Tier 0 가 원래 해시로 검사한다).

**TLS 키.** EC2 는 Tier 0 TLS 비밀 키를 갖지도 주지도 않는다. EC2 가 뚫려도 Tier 0 를 사칭할 키가 나가지 않게 하려는 것이다.
인스턴스는 (a) 공급자 비밀 저장소에서 지금 고정 키를 받거나(노트북 고정 지문 `SIMAI_TIER0_PINNED_SPKI_SHA256` 그대로, 비밀값이 둘이 됨),
(b) 부팅 때 새로 만든다(노트북 고정 지문을 바꿔야 함). 어느 쪽인지와 노트북 지문 갱신은 -700·-701 이 정한다. 등록 메시지에 선택
`tls_public_key_sha256` 를 두어 서버가 세션별 지문을 기록·표시할 수 있게 했다.

**Tier 0 토큰 두 갈래.** `Tier0TokenAuthenticationFilter` 는 환경 변수 해시(`SIMAI_TIER0_TOKEN_SHA256`, 손으로 켠 세션)를 먼저 고정 시간
비교하고, 다르면 열린 GPU 세션의 `tier0_token_hash` 를 DB 에서 찾는다(UNIQUE 인덱스, JWT 모양은 DB 를 보지 않음). DB 를 못 읽으면 503.
환경 변수 토큰은 이 티켓 뒤에도 그대로 쓸 수 있다(Lead 의 손 세션, 예비).

## 토큰 수명

- 워커 토큰·Tier 0 토큰: 등록 순간부터 세션이 끝날 때까지. 끝나는 경우 다섯 가지(`end_reason`): END_REPORTED(인스턴스 종료 보고),
  SUPERSEDED(새 등록), STALE(무응답), BOOT_SECRET_REVOKED(관리 명령), ADMIN_STOPPED(-702 가 부를 서버 쪽 정지 `GpuSessionService.end`).
- 끝나면: Tier 0 토큰은 다음 요청부터 거부(I1 업그레이드·업로드 서명), 열린 I1 연결은 커밋 직후 끊는다(웹 서버 안에서 끝낸 경우 이벤트,
  관리 명령처럼 다른 프로세스면 30 s 주기 작업이). 워커 토큰은 폐기한다(REST 는 요청마다 DB 확인이라 바로, 아바타 WebSocket 은 업그레이드 때만
  확인해 열린 연결은 남는다 → 잔여 위험).
- FAILED 보고는 세션을 끝내지 않는다(같은 인스턴스가 다시 시도할 수 있다). 끌지는 -702 의 자동 정지가 정한다.
- 부팅 비밀값: 관리자가 폐기할 때까지. `rotate` 는 열린 세션을 끝내지 않는다(돌고 있는 인스턴스는 다음 보고부터 401 → I1 이 붙어 있으면
  관제는 계속, 없으면 10분 뒤 STALE). 세션이 없을 때 회전한다.
- 워커 되살리기: 세션이 끝나면 워커 토큰을 폐기하므로 다음 등록은 `reissue`(폐기돼 있어도 새 토큰 + 되살림)한다. 기존 `rotate` 의
  "폐기된 주체는 되살리지 않는다" 규칙의 예외다. 관리자가 GPU 세션을 막으려면 워커가 아니라 부팅 비밀값을 폐기한다.

## 동시성

- 등록·종료·부팅 비밀값 폐기·무응답 정리는 `pg_advisory_xact_lock(hashtext('simai.gpu_session'))` 하나로 줄 세운다. 같은 시점 열린 세션은
  하나(등록이 잠금 안에서 열린 세션을 모두 끝낸 뒤 새로 연다).
- 끝은 `gpu_session_event (session_id) WHERE kind = 'ENDED'` 부분 UNIQUE + `ON CONFLICT DO NOTHING` 으로 한 번만 기록하고, 이번에 기록한
  쪽만 워커를 폐기·이벤트를 낸다. 워커 폐기는 같은 워커를 쓰는 다른 열린 세션이 없을 때만(새 등록은 끝내기 → 발급 순서).
- 무응답 정리는 잠금 안에서 "마지막 수신이 판정 기준보다 이른가"를 다시 보고 끝낸다(판정 뒤 들어온 보고를 무시하지 않게).
- 진행 보고는 잠금 없이 덧붙인다. 밀림과 겹친 보고가 ENDED 뒤에 들어가도 상태는 ENDED 행 유무로 정하므로 해가 없다.

## 공급자 무관 구조

서버는 공급자 API 를 부르지 않는다. 공급자 차이는 등록 메시지의 값(`provider`·`instance_id`·`region`·`price_per_hour_usd`)뿐이고 토큰 발급·
기동 설정·수명은 같다. RunPod 는 Secrets(`{{ RUNPOD_SECRET_… }}`), AWS 는 Secrets Manager 나 user-data 로 부팅 비밀값을 넣는다.
`MANUAL` 은 사람이 켠 인스턴스(공급자 API 로 끄지 않음)다. 공급자별 비밀값을 따로 두고 싶으면 `admin gpu-boot register` 를 공급자마다
(같은 워커에 묶어도 된다).

## 다음 티켓이 쓸 인터페이스

- **-700 GPU 자동 부팅(Pod 쪽).** 환경 변수(공급자 비밀)로 부팅 비밀값·비밀값 ID·EC2 주소를 받는다 → `gpu_session_register` 를 보낸다
  (`instance_id` = `RUNPOD_POD_ID`, `region` = `RUNPOD_DC_ID`, `public_host` = `RUNPOD_PUBLIC_IP`, 포트 = `RUNPOD_TCP_PORT_<번호>`,
  `image_digest` = `SIMAI_IMAGE_DIGEST`, 모르면 null) → 응답 토큰을 권한 600 파일(`--worker-token-file`·`--tier0-token-file`)에 쓴다 →
  `start.config.calibration.uri` 를 받아 sha256 확인 후 `lobby-bundle/calibration.json`, `start.laptop_token_sha256` 를 쉼표로 이어
  `laptop_token.sha256`, TLS 는 위 (a)/(b) → `simai-bootstrap lobby --ec2-base-url <start.ec2_base_url> --camera-ids <start.camera_ids>
  --measured-heights <start.measured_heights_m> --scene-version <start.scene_version> --config-version <start.config.config_version>
  --gateway-id <tier0.gateway_id> --worker-id <worker.worker_id> …`. `start.config` 가 null 이면 lobby 대신 세팅 경로.
  단계마다 + `report.interval_ms`(60 s)마다 `gpu_session_report`(긴 단계 중에도 배경으로). 409 `GPU_SESSION_ENDED` 면 서비스를 멈춘다.
  인스턴스가 스스로 멈출 때 `stage: ENDED`. 토큰·부팅 비밀값은 명령줄·로그에 남기지 않는다(`simai-bootstrap` 의 `_redact` 대상에 넣기).
- **-701 게이트웨이 자동 전환.** 노트북은 지금처럼 `GET /api/endpoints/tier0`(기기 토큰)로 새 Tier 0 엔드포인트를 받는다. 세션이 바뀌면
  Tier 0 가 I1 `tier0_endpoint` 로 새 주소를 등록한다(정본은 여전히 이것). TLS 키를 (b) 로 하면 고정 지문 갱신 방식을 정해야 한다.
  세션 상태가 필요하면 기기 토큰용 조회를 따로 둔다(지금 `current` 는 관리자 JWT 만).
- **-702 공급자 추상화·화면.** 화면 표시는 `GET /api/gpu/sessions/current`. 끄기·자동 정지 뒤에는 `GpuSessionService.end(sessionId,
  EndReason.ADMIN_STOPPED)` 를 불러 토큰을 바로 폐기한다(인스턴스가 ENDED 를 못 보낼 수 있다). `instance_id`·`provider` 로 공급자 API 를 부른다.
  자동 정지 판단 재료: `stage`(FAILED 지속), `last_report_at_ms`, `tier0_link`, `elapsed_ms`, `price_per_hour_usd`.
- **-703 AWS.** `provider: AWS` 로 같은 메시지. 서버 변경 없음.

## 버린 대안

- **세션마다 Tier 0 토큰 대신 고정 토큰 원문을 돌려주기**: 서버는 고정 토큰 해시만 가져 원문을 줄 수 없다. 원문을 EC2 에 두면 명세 14 위반.
- **TLS 비밀 키를 서버가 보관·배포**: EC2 침해 = Tier 0 사칭 키 유출. 공급자 비밀 저장소나 인스턴스 생성으로 둔다.
- **워커 ID 를 등록 요청에 싣기**: 새 부팅 비밀값이 아무 워커나 되살릴 수 있다. 비밀값마다 한 워커로 묶었다.
- **상태를 고치는 세션 표(state 칸 UPDATE)**: 덮어쓰지 않는다 규칙(AGENTS 6). 사건 표에 덧붙이고 상태는 읽을 때 만든다.
- **무응답이면 무조건 끝내기**: 보고기만 죽어도 관제가 끊긴다. I1 연결이 있으면 둔다.
- **등록 응답을 별도 메시지 없이 REST 응답으로(계약 밖)**: -700 이 Pod 쪽 코드에서 쓰는 파트 사이 경계라 계약으로 뒀다. 관리자 상태 조회는
  화면 전용이라 다른 관리 조회처럼 계약 밖.

## 잔여 위험

- 부팅 비밀값 유출 시 진짜 세션을 밀어낼 수 있다(가용성). 등록 빈도 제한은 두지 않았다(사람이 켜는 빈도라). 필요하면 -702 에서.
- 끝난 세션 워커의 아바타 WebSocket(`/ws/worker/avatar`)은 업그레이드 때만 토큰을 봐서 열린 연결이 남는다. 인스턴스가 멈추면 같이 끊긴다.
- `rotate` 한 부팅 비밀값은 돌고 있는 인스턴스의 보고를 막는다(위 "토큰 수명").
- 환경 변수 Tier 0 토큰(손 세션)과 GPU 세션 Tier 0 가 동시에 붙으면 둘 다 받는다(같은 gateway_id 면 I1 쪽 기존 동작). 손 세션을 쓰지 않을 때
  `SIMAI_TIER0_TOKEN_SHA256` 를 비우면 막힌다.
- `start.measured_heights_m` 은 세팅 화면의 마지막 제출 높이(`site.default_camera_heights_m`)다. 카메라 거치대를 옮긴 뒤 세팅 전이면 옛 값이다(지금 손 인자와 같은 성격).

## 넘긴 일

- Pod 쪽 부팅·등록·보고·토큰 파일(-700), 게이트웨이 전환·지문(-701), 공급자 API·화면·자동 정지(-702), AWS(-703).
- 관리 화면(front) 표시는 -702.

## 코드·시험 위치

- 계약: `contracts/messages/gpu_session_register.schema.json`·`gpu_session_register_result.schema.json`·`gpu_session_report.schema.json`,
  예시 `contracts/examples/gpu_session_*`, 반례 `contracts/examples/invalid/gpu_session_*`, Python 모델 `ai/src/simai_ai/contracts/models.py`.
- 서버: `server/src/main/java/com/ssafy/simai/domain/gpu/`(`GpuSessionService`·`GpuSessionController`·`GpuStartConfig`·`GpuSessionSweeper`·
  `GpuSessionEnded`·`GpuPaths`), `domain/machine/MachineKind`(GPU_BOOT)·`MachineTokenService`(`registerGpuBoot`·`reissue`),
  `global/security/Tier0TokenAuthenticationFilter`·`Tier0SessionTokens`, `global/config/SecurityConfig`(`gpuBootFilterChain`),
  `global/command/MachineCommands`(`gpu-boot`), `domain/tier0/i1/I1Connection.principalName`, `db/migration/V75__gpu_session.sql`.
- 시험: `server/src/test/java/com/ssafy/simai/domain/gpu/GpuSessionIntegrationTest`(등록·중복 등록·권한·다른 API 거부·보고·무응답),
  `GpuSessionI1IntegrationTest`(실제 I1 연결이 새 등록에 끊기고 옛 토큰으로 다시 못 붙음), 계약 왕복 `ContractExamplesRoundTripTest`.
