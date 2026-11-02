# 관제 화면 CCTV 원격 시청 (S15P31A101-399)

현장 노트북이 아닌 곳에서도 로그인한 관리자가 배포된 관제 화면(`https://simai.live/cctv`)에서 카메라 4대 실시간 영상을 본다.
노트북 영상 중계(`gateway/relay`)가 이미 받고 있는 H.264 스트림을 카메라 RTSP 추가 접속 없이 EC2 수신 WebSocket 으로 올리고,
EC2 는 영상을 저장하지 않은 채 메모리에서 시청 중인 관리자 브라우저에만 나눠 준다. **보는 사람이 있을 때만** 게이트웨이가
EC2 로 프레임을 보내 평소 업로드와 EC2 송출 트래픽을 0 으로 유지한다.

현장 노트북 루프백(`127.0.0.1:47032`)에서 바로 보는 로컬 미리보기는 [cctv-local-preview.md](cctv-local-preview.md)(S15P31A101-310)를 따른다.

## 관련 명세

- 3.1 데이터 흐름의 원칙, 3.3 RTSP 접속 규칙, 3.5 네트워크, 16장 결정 "관제 화면 실시간 영상"(v1.3 수정 46, 사용자 결정 2026-11-02):
  현장 노트북 로컬 미리보기와 함께, 로그인한 관리자에 한해 EC2 경유 원격 실시간 시청(보는 사람 있을 때만 전송, EC2 무저장)을 허용한다.
  이전 결정인 "원격 관제 화면 실시간 영상 제외(v1.2 r4)"와 "관제 미리보기 영상은 EC2 를 거치지 않음(v1.3 수정 34)"은 이 결정으로 대체됐다.
- 그대로 지키는 불변 원칙:
  1. **C110 RTSP 동시 접속 2세션 한도 보호.** 카메라 RTSP 세션을 늘리지 않는다. 중계가 이미 열어 둔 1세션에서 Tier 0·로컬 미리보기·원격 송출이 같은 I0 기록을 나눠 쓴다.
  2. **EC2 영상 무저장(명세 3.1·11.2·14).** EC2 는 받은 영상 바이트를 디스크·DB·S3 어디에도 적지 않고 메모리에서 열린 시청 소켓으로만 넘긴다.
  3. **위험 알림 채널 분리(AGENTS.md 규칙 1·5).** 원격 CCTV 영상과 월 송출량 표시는 운영 확인용이며 위험 판정·알림 채널에 들어가지 않는다.
  4. **추가 전용 기록(AGENTS.md 규칙 6).** 월 송출 바이트 집계는 기존 행을 덮어쓰지 않고 1분 단위 사용량 기록을 추가(append-only)해 합산한다.
- 계약: `contracts/messages/cctv_remote_control.schema.json`(1.0.0), `contracts/messages/cctv_preview.schema.json`(1.1.0, 묶음 v6.11.0).
- 요구사항: `docs/requirements.md` FR-133, NFR-11, NFR-12, NFR-15.

## 경로 구조

```
카메라 4대 ─RTSP 1세션─ 게이트웨이 영상 중계(gateway/relay)
                          ├─ 127.0.0.1:47032 로컬 미리보기 (지금 그대로, simai-cctv-preview/1)
                          └─ wss://<EC2>/ws/devices/cctv (상시 연결, demand active 일 때만 프레임 전송)
                                │
                             EC2 서버 (저장 없이 메모리 중계, 동시 5명·월 80 GB 상한)
                                │
                                └─ wss://<EC2>/ws/cctv/view?ticket=… (일회용 입장권, 관리자 전용)
                                      ├─ 로그인한 관리자 브라우저 1 (WebCodecs VideoDecoder)
                                      └─ 로그인한 관리자 브라우저 N (최대 5대)
```

## 연결·메시지 형식과 인증

### 1. 게이트웨이 → EC2 수신 (`wss://<EC2>/ws/devices/cctv`)

- **인증:** 기존 `SecurityConfig.deviceFilterChain`(`server/src/main/java/com/ssafy/simai/global/config/SecurityConfig.java`)이 `/api/devices/**` 에 적용하는 기기 토큰 헤더(`X-Device-Token` 등 `MachineKind.DEVICE` 인증)를 `/ws/devices/cctv` 에도 그대로 적용한다.
- **연결 유지:** 중계는 프로세스가 떠 있는 동안 EC2 수신 WebSocket 연결을 상시 유지한다. 끊기면 지수 백오프(초기 `1 s`, 최대 `30 s`)로 재연결한다.
- **EC2 → 게이트웨이 제어 메시지(`cctv_remote_control` 1.0.0):**
  - 형식: `{"kind": "demand", "active": true | false, "sent_at_ms": <UTC ms>}` (봉투 없음, 닫힌 객체).
  - 전송 시점: 게이트웨이 접속 직후 현재 수요 상태를 1회 보내고, 이후 시청자 유무가 바뀔 때마다 보낸다.
- **게이트웨이 → EC2 영상 전송(`simai-cctv-preview/1`):**
  - `active: true` 를 받으면 로컬 미리보기와 똑같은 형식으로 보낸다: `hello`(카메라 목록) 1회 → 카메라별 마지막 `codec`(I0 `codec_info` + `codec_string`) → 카메라별 다음 키프레임부터 이진 프레임(`u32 BE 헤더 길이 | I0 frame_header JSON | Annex B 접근 단위`). 카메라가 새 `stream_epoch` 를 시작하면 해당 카메라의 `codec` 을 다시 보낸다.
  - `active: false` 를 받으면 WebSocket 연결은 닫지 않고 프레임 전송만 즉시 멈춘다.

### 2. EC2 → 관리자 브라우저 시청 (`wss://<EC2>/ws/cctv/view?ticket=…`)

- **입장권 발급(`POST /api/cctv/remote/tickets`):** 브라우저 `WebSocket` API 는 임의 `Authorization` 헤더를 붙일 수 없다. 관리자가 기존 JWT 인증으로 `POST /api/cctv/remote/tickets` 를 호출하면 서버가 `ROLE_ADMIN` 권한을 확인한 뒤 무작위 일회용 입장권(`ticket`, 유효 `60 s`, 1회 소비 시 즉시 삭제)을 발급한다.
- **WebSocket 접속:** 브라우저는 `wss://<EC2>/ws/cctv/view?ticket=<발급값>` 으로 접속한다. `WebSocketConfig`(`server/src/main/java/com/ssafy/simai/global/config/WebSocketConfig.java`)에 `AvatarWebSocketHandler` 와 같은 방식의 원시(raw) WebSocket 핸들러로 등록한다.
- **EC2 → 브라우저 메시지(`cctv_preview` 1.1.0):**
  - 텍스트 `hello`·`codec` 과 이진 프레임은 `simai-cctv-preview/1` 그대로다. 새로 붙은 시청자에게는 EC2 가 기억하고 있는 `hello`·카메라별 최신 `codec` 을 먼저 보내고, 각 카메라의 다음 키프레임부터 이진 프레임을 넘긴다.
  - 텍스트 `status` 메시지(`cctv_preview` 1.1.0 에 추가):
    ```json
    {
      "kind": "status",
      "state": "live | waiting_source | viewer_limit | monthly_quota",
      "monthly_sent_bytes": 123456789,
      "monthly_quota_bytes": 80000000000,
      "sent_at_ms": 1775000000500
    }
    ```
    - `live`: 게이트웨이가 연결되어 있고 프레임이 정상 중계되는 상태.
    - `waiting_source`: 게이트웨이 수신 연결이 없거나 아직 첫 `hello`/프레임이 오지 않은 상태.
    - `viewer_limit`: 동시 시청자 상한(5명) 초과로 거절될 때 보내고 연결을 닫는다.
    - `monthly_quota`: 월간 송출 상한(80 GB) 도달로 거절·중단될 때 보내고 연결을 닫는다.
- **로컬 미리보기와의 공존:** 프론트 관제 앱(`/cctv`)은 현장 노트북에서 로컬 미리보기(`ws://127.0.0.1:47032`)가 연결되면 로컬을 우선 쓰고, 로컬 연결이 안 되거나 원격 모드를 고르면 EC2 원격 시청으로 붙는다.

## 수요 신호와 대기열 격리

- **즉시 켜기(0 → 1명):** 시청자가 0명에서 1명이 되는 순간 EC2 는 게이트웨이로 `{"kind":"demand","active":true,"sent_at_ms":…}` 를 즉시 보낸다.
- **유예 뒤 끄기(1 → 0명):** 마지막 시청자가 나가 0명이 되면 `15 s` 유예 타이머를 돌린다. `15 s` 뒤에도 시청자가 0명이면 그때 `{"kind":"demand","active":false,"sent_at_ms":…}` 를 보낸다. 관리자가 화면을 새로고침할 때 키프레임부터 다시 시작하며 깜빡이는 일을 막는다.
- **유령 연결 감지(ping/pong):** 게이트웨이 수신 소켓과 브라우저 시청 소켓 모두 `15 s` 마다 WebSocket ping 을 보내고 `30 s` 안에 pong 이 오지 않으면 연결을 닫고 시청자 수에서 뺀다.
- **게이트웨이 대기열 격리:** 원격 송출 큐는 Tier 0 전송 큐·로컬 미리보기 큐와 완전히 분리한다. 업로드 회선이 밀려 원격 송출 큐가 차면 해당 카메라만 다음 키프레임까지 건너뛰고(`dropped_overflow`), Tier 0 전송 스레드와 로컬 미리보기는 절대 늦추지 않는다.
- **EC2 시청자별 대기열 격리:** EC2 도 시청자마다 독립된 송신 큐를 둔다. 특정 시청자의 네트워크가 느려 큐가 차면 그 시청자의 해당 카메라만 다음 키프레임까지 건너뛰고 다른 시청자에게 영향을 주지 않는다.

## 딴짓 방지·비용 상한과 설정 초기값

### 브라우저 쪽 차단 규칙

1. **탭 비가시(`Page Visibility API`):** `document.visibilityState === "hidden"` 이 되면 즉시 WebSocket 을 닫는다. 다시 `"visible"` 이 되면 새 입장권을 받아 재연결한다.
2. **무입력 자동 종료:** 마우스·키보드·터치 입력이 `10분` 동안 없으면 연결을 닫고 화면에 "자리 비움으로 중지됨 — 다시 보기" 버튼을 띄운다.
3. **연속 시청 확인:** 입력이 있어도 `30분` 연속 시청마다 연결을 멈추고 "계속 보시겠습니까? — 다시 보기" 확인을 받는다.
4. **경로 이탈:** `/cctv` 라우트를 벗어나면 즉시 연결을 닫는다.

### 서버 쪽 상한·집계 규칙

1. **동시 시청자 상한:** 서버 전체 동시 시청 WebSocket 은 최대 `5명`이다. 6번째 접속에는 `{"kind":"status","state":"viewer_limit",…}` 를 보내고 연결을 닫는다.
2. **월간 송출량 상한(80 GB)과 추가 전용 기록:**
   - 서버는 원격 CCTV 시청 소켓으로 내보낸 바이트 수를 세어 `1분`마다 사용량 기록 행을 DB 에 **추가 전용(append-only)** 으로 적는다(AGENTS.md 규칙 6 준수, 기존 행 UPDATE 없음).
   - UTC 달력 월 기준 누적 송출 바이트가 `80 GB`(`80,000,000,000 bytes`) 이상이 되면 즉시 `demand active: false` 로 내리고, 열려 있는 모든 시청자에게 `status: monthly_quota` 를 보낸 뒤 연결을 닫으며, 그 달에는 새 시청 접속도 `status: monthly_quota` 로 거절한다.
3. **관리자 상태 패널 표시:** 관제 화면에 "원격 CCTV 월 송출 `x / 80 GB` · 차단 여부"를 표시한다. 이는 운영 상태 표시이며 위험 알림 채널이 아니다(AGENTS.md 규칙 1·5).

### 정한 수치와 근거

| 항목 | 초기값(설정으로 변경 가능) | 근거 |
|---|---|---|
| 동시 시청자 상한 | 5명 | 시연·운영 관리자 소수 확인용. 최대 대역폭(5 × 4 Mbps = 20 Mbps)과 EC2 메모리 제한 |
| 월간 송출 상한 | 80 GB (UTC 월) | AWS 계정 월 100 GB 무료 송출 한도 안에서 다른 트래픽 여유 20 GB 확보 |
| 송출량 기록 주기 | 1분 (추가 전용) | AGENTS.md 규칙 6(추가 전용) 준수 및 서버 재시작 시 월 누적치 복원 |
| 무입력 자동 종료 | 10분 | 탭을 켜 두고 자리를 비운 채 시간당 1.8 GB 가 새는 일 방지 |
| 연속 시청 확인 주기 | 30분 | 장시간 켜 놓기 방지(30분당 약 0.9 GB) |
| 마지막 시청자 종료 유예 | 15 s | 브라우저 새로고침·짧은 재연결 시 게이트웨이 송출이 끊겼다 켜지는 깜빡임 방지 |
| WebSocket ping / pong 타임아웃 | 15 s / 30 s | 비정상 종료된 유령 시청자가 수요 신호(`active: true`)를 붙잡고 있는 시간 상한 30 s |
| 일회용 입장권 유효 시간 | 60 s (1회 사용) | 발급 직후 WebSocket 핸드셰이크에만 쓰고 재사용·노출 위험 차단 |
| 게이트웨이 재연결 백오프 | 1 s → 최대 30 s | EC2 재시작·순단 시 자동 복구 |
| 큐 크기·버림 규칙 | 50 프레임 · 16 MiB (카메라별 키프레임 건너뛰기) | 로컬 미리보기(`cctv-local-preview.md`)와 동일 기준 |

## 대역폭·비용 산출

- **게이트웨이 업로드 대역폭:**
  - 카메라 4대 stream1(2304×1296, 25 fps, H.264) 합산은 약 `4 Mbps`다(명세 3.3 실측 대당 `0.86~1.1 Mbps`).
  - LIVE 관제 세션 중에는 Tier 0 영상도 EC2 SSH 포워딩(S15P31A101-392)을 거쳐 올라가므로, 관제 중 동시에 원격 CCTV 를 켜면 게이트웨이 업로드는 합산 약 `9 Mbps`(`Tier 0 약 4.5 Mbps + 원격 CCTV 약 4 Mbps`)가 된다.
  - 명세 3.5 최소 업로드 요건(`10 Mbps` 이상) 및 명세 3.12 SSAFY 현장 망 실측치 안쪽이나 여유가 크지 않으므로 아래 잔여 위험의 완화책을 둔다.
- **EC2 송출(Data Transfer Out) 트래픽과 비용:**
  - 시청자 1명당 `4 Mbps` ≈ `0.5 MB/s` ≈ 시간당 약 `1.8 GB`.
  - AWS 계정은 계정 전체 합산 월 `100 GB` 까지 인터넷 송출이 무료이고, 초과분은 서울 리전(`ap-northeast-2`) 기준 GB당 약 `$0.126`(2026-11 기준)이다.
  - 서버가 월 `80 GB`(시청자 1명 기준 약 44시간)에서 자동 차단하므로 무료 한도(100 GB)를 넘지 않는다. 단, **SSAFY 실습 계정에 별도 트래픽·비용 한도가 있는지는 확인 필요**다.

## 버린 대안

- **STOMP 토픽 브로드캐스트:** 이진 H.264 접근 단위를 JSON 텍스트·base64 로 감싸면 바이트가 약 33% 불어나고(4 Mbps → 5.3 Mbps), Spring STOMP 브로커가 카메라별 키프레임 경계 버림 제어를 하지 못한다.
- **WebRTC · HLS 미디어 서버(mediasoup, MediaMTX 등):** 별도 미디어 데몬 운영·포트 개방·트랜스코딩/패키징 복잡도가 크다. 이미 로컬 미리보기에 검증된 `simai-cctv-preview/1` + 브라우저 WebCodecs 경로를 그대로 중계하는 편이 가장 단순하다.
- **키프레임만 보내는 저속(0.5 fps) 기본 모드:** 대역폭은 줄지만 실시간 관제 화면으로서 움직임이 끊긴다. 사용자 결정(2026-11-02)으로 실시간 원본 전달을 채택하고, 저속 모드는 향후 대역폭 부족 시 옵션으로만 남겨 둔다.
- **GPU Pod 경유 중계:** RunPod GPU Pod 는 관제 세션 중에만 켜지므로(명세 3.1·17장 위험 27) 평소에 원격으로 카메라 상태를 볼 수 없다.
- **원격 시청 영상 EC2 녹화:** EC2 에 영상을 저장하지 않는 원칙(명세 3.1·11.2·14)에 어긋난다.

## 잔여 위험

- **관제 중 업로드 회선 공유로 인한 Tier 0 지연 증가:** LIVE 관제 중 원격 시청을 함께 켜면 게이트웨이 업로드가 약 9 Mbps 로 올라가 현장 공유기·업링크 상태에 따라 Tier 0 프레임 도착 지연이 늘 수 있다.
  - **완화:** 원격 송출 큐를 분리해 큐가 차면 원격 쪽만 버리고, **10차 GPU 세션**에서 원격 시청을 켠 채 Tier 0 지연을 실측한다. 지연이 늘어나면 게이트웨이 설정으로 "LIVE 관제 세션 중에는 원격 CCTV 송출 자동 일시 중지" 옵션을 켠다.
- **EC2 정지·재시작:** EC2 가 멈추면 원격 시청은 중단된다(`waiting_source` 또는 연결 종료). 현장 노트북의 로컬 미리보기(`127.0.0.1:47032`)는 EC2 와 무관하게 그대로 동작한다.
- **키프레임 대기·브라우저 디코더 부하:** 접속 직후·건너뛰기 뒤 최대 2 s 키프레임 대기와 2304×1296 4스트림 WebCodecs 디코딩 부하는 로컬 미리보기(`cctv-local-preview.md`)와 같다.

## 넘긴 일 (후속 구현 티켓)

| 티켓 | 파트 | 구현 범위 및 예상 위치 |
|---|---|---|
| `S15P31A101-400` | 게이트웨이 중계 (`gateway/relay`) | `wss://<EC2>/ws/devices/cctv` 상시 연결·지수 백오프 재연결, 기기 토큰 헤더, `cctv_remote_control` 수신 해석, `demand active` 에 따른 `simai-cctv-preview/1` 송출·중단, Tier 0·로컬 미리보기와 분리된 원격 송출 큐 및 키프레임 건너뛰기, LIVE 관제 중 원격 송출 끄기 설정(`gateway/relay/src/simai_relay/`, `gateway/relay/tests/`), Python 계약 모델 재생성(`ai/src/simai_ai/contracts/models.py`) |
| `S15P31A101-400`(서버, -401 합침) | EC2 서버 (`server`) | `POST /api/cctv/remote/tickets`(관리자 전용, 60 s 일회용 입장권), `/ws/devices/cctv`(기기 토큰 인증·수요 신호 `cctv_remote_control` 전송·15 s 종료 유예), `/ws/cctv/view`(입장권 검증·동시 5명 상한·카메라별 `hello`/`codec`/키프레임 시작 중계·15 s ping/30 s 타임아웃), 1분 단위 추가 전용 월 송출 바이트 기록·80 GB 차단·상태 조회 API(`server/src/main/java/com/ssafy/simai/domain/cctv/`) |
| `S15P31A101-400`(프론트, -402 합침) | 프론트 관제 앱 (`front/apps/admin`) | `/cctv` 로컬 루프백 실패 시(또는 원격 선택 시) `POST /api/cctv/remote/tickets` → `wss://<EC2>/ws/cctv/view?ticket=…` 연결, `cctv_preview` 1.1.0 `status`(`live`·`waiting_source`·`viewer_limit`·`monthly_quota`) 표시, 탭 비가시·10분 무입력·30분 연속 시청·라우트 이탈 시 자동 종료와 "다시 보기", 관리자 상태 패널 월 송출량(`x / 80 GB`·차단 여부) 표시(`front/apps/admin/src/features/cctv/`, `front/packages/contracts/`) |
