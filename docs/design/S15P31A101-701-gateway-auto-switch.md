# S15P31A101-701 게이트웨이 자동 전환(GPU G3): 팀원이 켠 GPU 세션을 따라 관제 ↔ 미리보기

관련 명세: 3.2(LIVE 지연·관제를 끊는 쪽이 더 위험), 3.12(GPU 실행 사이트·엔드포인트 등록·막힌 망 경로), 12.2(I0·I1), 12.6(기기 REST),
12.7(I0 TLS 공개키 고정), 14(기기 토큰·비밀 값). 사용자 결정 2026-11-14: 팀원 누구나 GPU 를 켜면 사람 손 없이 세션이 준비된다.
앞 티켓: -699(G1 서버 GPU 세션 등록, `docs/design/S15P31A101-699-gpu-session-register.md`). 병렬: -700(Pod 자동 부팅). 뒤: -702(공급자·화면·자동 정지).
계약 묶음 v7.54.0(`messages/gpu_session_status`·`gateway_switch_state` 1.0.0). DB 변경 없음.

## 지금 손으로 하는 일 → 이 티켓 뒤

| 지금(Lead) | 이 티켓 뒤 |
|---|---|
| RunPod 화면에서 Pod 공개 IP 와 17000·17001 의 바깥 매핑을 읽어 `gw.sh live <IP> <포트> <포트>`(run-remote) | 작업자가 EC2 에서 세션 주소·포트를 받아 같은 `run` 을 띄운다 |
| 세션이 끝나면 `gw.sh preview`(preview-remote) | 관제 대상이 5분 넘게 안 보이면 `preview` 로 되돌린다 |
| Pod 를 바꾸면 heal 이 옛 주소로 되살릴 수 있어 `gw.sh live` 를 다시 | 새 세션이 준비되면 새 주소로 바로 옮긴다 |

## 조회 경로·주기

**`GET /api/endpoints/tier0` 만으로는 모자란다(조사 결과).**
1. EC2 터널 방식(SSAFY 망은 EC2:22 만 열림)에서는 Tier 0 가 `tier0_endpoint` 를 `public_ip=127.0.0.1`·포트 17000·17001 로 등록한다
   (`docs/design/tier0-endpoint-tunnel-ports.md`, 11~13차). 중계·경보 단말은 이 값으로 로컬 터널에 붙지만, 터널(`ssh -L 127.0.0.1:17000:<Pod IP>:<바깥 포트>`)을
   만들 **Pod 공개 IP·바깥 포트가 이 메시지에 없다.** 그 값은 -699 `gpu_session_register` 의 `public_host`·`ports`(RUNPOD_PUBLIC_IP·RUNPOD_TCP_PORT_*)에만 있다.
2. `tier0_endpoint` 는 추가만 하고 "현재 = 마지막 등록"이라 **끝났다는 신호가 없다.** Pod 를 꺼도 옛 값이 계속 나온다. 세션 끝은 G1 `gpu_session` 의 ENDED 만 안다.

그래서 기기 토큰용 최소 조회를 더했다. 지금 `GET /api/gpu/sessions/current` 는 관리자 JWT 화면용이라 노트북에 열지 않고 경로를 나눴다.

| 경로 | 계약 | 쓰는 값 |
|---|---|---|
| `GET /api/devices/gpu-session`(기기 토큰, LAPTOP 만, 신설) | `gpu_session_status` 1.0.0 | 가장 최근 세션의 `state`·`stage`·`public_host`·CONTROL·VIDEO `public_port`·`tier0_link`·`registered_at_ms`·등록 지문 |
| `GET /api/endpoints/tier0`(기존, 정본) | `tier0_endpoint` | TLS 지문(고정 목록과 대조), 루프백이면 포트가 터널(17000·17001)과 같은지 |
| `POST /api/devices/gpu-session/gateway-state`(신설) | `gateway_switch_state` 1.0.0 | 판단·전환 결과를 EC2 에 알림(아래 "기록") |

- 세션 조회는 노트북 전환에 쓰는 칸만 준다(SSH 포트·공급자·인스턴스 ID·가격·워커 ID 없음). 워치 토큰은 403(`GPU_NOT_A_LAPTOP`), 부팅 비밀값·관리자 JWT 는 기기 체인이 아니라 401.
- 엔드포인트 조회는 세션이 ACTIVE 이고 `tier0_link=CONNECTED` 일 때만 한다(Tier 0 가 I1 에 붙을 때 엔드포인트를 등록하므로 그 전엔 의미가 없다).
- **주기 15 s**: 조회 두 번(세션이 준비된 뒤에만 두 번째)은 EC2 에 무시할 부하다(노트북 하나). Tier 0 가 붙은 뒤 관제 시작까지 최대 15 s + 전환 20~25 s.
  GPU 부팅이 분 단위라 체감 차이가 없고, -693 업로드 작업자의 10 s 조회와 같은 크기다. 상시 소켓은 더하지 않았다(-693 과 같은 이유).

## 전환 규칙(`Get-AutoSwitchPlan`, 순수 함수)

| 순서 | 조건 | 판단(까닭 코드) |
|---|---|---|
| 1 | 꺼짐(`.env SIMAI_GATEWAY_AUTO_SWITCH=off` 또는 `auto off`) | 그대로(`DISABLED`) |
| 2 | 다른 전환·원격 갱신 예약 작업이 도는 중 | 그대로(`BUSY`) |
| 3 | EC2 조회 실패(연결 실패·5xx `EC2_UNREACHABLE`, 401·403 `AUTH_FAILED`, 그 밖 `INVALID_RESPONSE`) | **그대로**(유예가 지나도) |
| 4 | 모드 주인이 manual 이고, 그 뒤 등록된 ACTIVE 세션이 없음 | 그대로(`MANUAL_HOLD`) |
| 5 | 관제 중이고 대상(주소·제어·영상 포트)이 지금 세션과 같고 Tier 0 연결됨 | 그대로(`RUNNING`), 마지막 확인 시각 갱신 |
| 6 | 세션 ACTIVE·Tier 0 연결·엔드포인트 있음·지문 맞음·포트 맞음이고 대상이 지금과 다름 | **관제로**(`READY`) |
| 7 | 관제 중인데 대상이 안 보임(세션 끝·없음·Tier 0 끊김·다른 세션) | 마지막 확인부터 유예 안이면 그대로(`GRACE`), 넘으면 **미리보기로**(`SESSION_ENDED`·`NO_SESSION`·`TIER0_LOST`) |
| 8 | 자동 주인인데 꺼져 있음 | **미리보기로**(`MODE_OFF`, 평상시 상태) |
| 9 | 미리보기에서 새 대상이 아직 없음 | 그대로(`WAITING_TIER0`·`ENDPOINT_MISSING`·`PIN_MISMATCH`·`PORTS_UNSUPPORTED`·`ENDPOINT_PORTS_MISMATCH`·`NO_SESSION`·`SESSION_ENDED`) |
| - | 30분 안에 전환이 3번 실패 | 전환하지 않음(`BACKOFF`) |

- **같은 엔드포인트면 다시 전환하지 않는다.** 비교는 세션 ID 가 아니라 터널 대상(주소·제어·영상 포트)이다. 같은 Pod 가 다시 등록해 세션 ID 만 바뀌어도
  (-699: 재시도는 새 세션) 관제를 끊지 않는다. 지금 대상은 상태 파일이 아니라 모드 파일(`관제 Pod <IP> 제어 <포트> 영상 <포트>`, Do-Run 이 창을 띄운 뒤 씀)에서 읽는다.
- **지문.** `tier0_endpoint.tls_public_key_sha256`(그리고 세션 등록 지문이 있으면 그것도)이 `.env SIMAI_TIER0_PINNED_SPKI_SHA256`(쉼표 목록)에 없으면 전환하지 않고
  `PIN_MISMATCH` 와 받은 지문 앞 12자를 상태에 남긴다. 고정 목록이 비어도 같다. 조회 값만으로 신뢰할 키를 바꾸지 않는다(중계·경보 단말과 같은 규칙).
  지금 고정 키를 그대로 쓰는 전제다(-699 "TLS 키" (a), -700 이 공급자 비밀 저장소의 고정 키를 쓸 예정).
- **명령줄 안전.** 세션의 주소·포트가 `run -PodIp … -ControlPort … -VideoPort …` 명령줄에 들어가므로 주소는 계약 패턴(영숫자·점·하이픈), 포트는 1~65535 정수만 받는다.
  CONTROL 이 하나가 아니거나 VIDEO 가 여러 번호면 `PORTS_UNSUPPORTED`(터널은 영상 포트 하나만 연다).

### 유예 시간 5분(300 s)의 근거

- 짧은 끊김: EC2 재배포 약 1~2분(`deploy/ec2/deploy.sh`, -699 표와 같은 근거), Tier 0 재시작(감독자 재기동) 1~2분, I1 재접속 수 초.
  이때 세션은 ACTIVE 로 남고 `tier0_link` 만 잠깐 DISCONNECTED 이거나 조회가 실패한다. 긴 쪽(2분)의 2.5배라 조회 주기(15 s)·전환 시간(최대 25 s)을 더해도 남는다.
- 반대쪽 비용: 세션이 정말 끝났는데 5분 더 관제 모드에 있어도 해가 작다(경보 단말은 "관제 중단"을 띄우는 실제 상태, 원격 시청·녹화는 관제 모드에서도 돈다).
  반대로 짧은 끊김마다 미리보기로 갔다 오면 녹화 폴더가 갈리고 카메라 RTSP 세션을 다시 연다.
- EC2 무응답 정리(10분, -699)보다 짧다: EC2 가 세션을 STALE 로 끝내기 전이라도 Tier 0 가 5분 넘게 안 붙어 있으면 그 Pod 는 관제를 못 하고 있다.
- 바꾸려면 `.env SIMAI_GATEWAY_AUTO_REVERT_SECONDS`(60 미만은 60). 유예 기준 시각은 상태 파일(`gateway-auto.json` `last_good_at_ms`)에 남아 작업자가 다시 떠도 이어진다.
  기록이 없으면(작업자 첫 실행·수동 관제를 넘겨받음) 그때부터 센다. 자동 전환을 다시 켜거나 수동 우선이 풀리면 새로 센다.

### 조회 실패(모름)는 되돌리지 않는다

EC2 가 응답하지 않으면 세션이 끝났는지 모른다. 터널 방식에서는 터널도 EC2 를 지나 어차피 끊겨 있지만, 직접 접속 망(터널 없이 Pod 에 바로 붙는 망)에서는
EC2 가 죽어도 노트북 ↔ Tier 0 관제·현장 경보가 살아 있다. 모를 때 관제를 내리는 쪽이 더 위험하다(-699 무응답 예외와 같은 판단, 명세 3.2). EC2 가 돌아오면
그때의 세션 상태로 판단한다(끝났고 유예가 지났으면 바로 미리보기).

## 수동 우선 규칙

- 모드 주인 파일(`C:\simai-local\gateway-mode-owner.json`, `auto|manual` + 시각). 사람이 `run`·`preview`·`stop` 을 치면(메뉴 3·4·5, `gw.sh live/preview/stop`,
  `run-remote`·`preview-remote` 가 띄우는 `run`·`preview`) manual, 자동 전환이 띄우는 것은 `-Auto` 로 auto, heal·`update-remote` 가 이전 모드를 되살리는 것은
  `-KeepOwner` 로 그대로 둔다.
- manual 이면 **그 뒤(EC2 등록 시각 > 수동 명령 시각) 새로 등록된 ACTIVE 세션이 나올 때까지** 손대지 않는다. 손으로 켠 관제도, 손으로 끈 것도, 손으로 돌린 미리보기도 같다.
  새 세션은 누군가 GPU 를 새로 켰다는 뜻이라 그때부터 다시 따라간다. 바로 풀려면 `auto on`. 끄고 싶은 동안은 `auto off`(파일, 지금 모드 그대로) 또는 `.env` 끄기.
- 이 기능 전부터 켜 둔 모드(주인 파일 없음): 관제는 사람이 켠 것으로(모드 파일 시각부터 manual), 미리보기·꺼짐은 평상시라 auto 로 넘겨받는다.
- 시계: 수동 명령 시각은 게이트웨이 시계, 등록 시각은 EC2 시계다. 게이트웨이는 설치 때 시계를 맞추고(w32tm), GPU 부팅은 분 단위라 수 초 차이는 판단을 바꾸지 않는다.

## 녹화·업로드와의 관계(-554·-668·-693)

- 전환은 사람이 쓰는 원격 명령과 **같은 길**이다: 로그인 사용자 세션 예약 작업(`simai-gateway-run`, `Start-InSession`) → `run`·`preview` → `Do-Stop`
  (창 프로세스 트리 종료를 최대 10초 확인한 뒤에만 녹화 manifest 확정, !644) → 새 창. 관제 → 미리보기면 `Do-Preview` 가 기존처럼 녹화 업로드를 띄우고,
  관제 → 관제(새 Pod)면 `Do-Run` 이 업로드 작업자를 하나만 유지한다(뮤텍스·PID). 작업자는 업로드 작업자를 건드리지 않는다.
- 끄기가 실패하면(녹화 프로세스가 안 죽음) `Do-Stop` 이 멈추고 새 모드가 시작 기록을 남기지 않는다 → 전환 실패로 세고 다음 판단에 다시(30분 3번이면 `BACKOFF`).
  이때 `Do-Run` 이 새 Pod 주소를 상태 파일에 먼저 써 버려 heal 이 엉뚱한 주소로 되살리던 순서를 고쳤다(이전 창을 끈 뒤에 쓴다).
- 작업자를 관제 창의 부모로 두지 않는다: `update-remote`·설치가 작업자 예약 작업을 멈출 때 작업 스케줄러가 자식(관제 창·녹화)까지 끝내지 않게.
  그래서 작업자는 `simai-gateway-run` 예약 작업을 고칠 수 있게 관리자 권한(RunLevel Highest, 게이트웨이 계정은 관리자)으로 돈다.
- `update-remote`·설치(1)는 시작할 때 작업자를 멈추고 끝나면 다시 띄운다(갱신 중 꺼진 모드를 보고 미리보기를 띄우지 않게, 새 코드로 다시 뜨게).

## 기록

- 상태: `simai-gateway.ps1 status`(9)·`auto status`·`gw.sh auto` — 켬·끔(까닭), 모드 주인, 작업자 예약 작업 상태, 유예, 마지막 판단(까닭·세부·세션 ID 앞 8자·상태/단계·Tier 0·대상),
  마지막 전환(성공·실패). 실패류(`PIN_MISMATCH`·`AUTH_FAILED`·`BACKOFF` 등)는 노란 주의로.
- 로그: `C:\simai-local\gateway-auto.log`(`gw.sh logs auto`) — 판단이 바뀔 때·전환·수동 명령·수동 우선 해제만(15초마다 쌓지 않음, 1 MB 순환).
- 서버: **기존 게이트웨이 보고 경로가 맞지 않아 새로 뒀다.** `laptop_heartbeat` 는 관제 모드의 경보 단말만 보내 미리보기에서 "지문 불일치로 대기"를 못 알리고,
  payload 가 닫혀 있다. 그래서 `POST /api/devices/gpu-session/gateway-state`(`gateway_switch_state`)로 판단이 바뀌거나 전환했을 때, 그대로여도 5분마다 보낸다.
  EC2 는 기기별 마지막 값만 메모리에 두고(재배포로 비면 5분 안에 다시 채워짐) 바뀔 때만 INFO 로그를 남기며, 관리자 `GET /api/gpu/sessions/current` 의 `gateway`
  (`{device_id, received_at_ms, state}`)로 보여 준다. -702 화면이 "게이트웨이 관제 전환됨 / 지문 불일치" 를 띄우는 재료다. 판정·알림에는 쓰지 않는다.
  사건이 아니라 "지금 상태"라 표에 쌓지 않았다(DB 변경 없음, 이력은 서버·게이트웨이 로그).

## 정한 수치

| 값 | 기본 | 근거 |
|---|---|---|
| 조회 주기 | 15 s | 위 "조회 경로·주기" |
| 유예(되돌림 기준) | 300 s(`SIMAI_GATEWAY_AUTO_REVERT_SECONDS`, 최소 60) | 위 "유예 시간" |
| 전환 확인 대기 | 최대 60 s(모드 파일이 새 모드를 가리키면 바로 끝) | `Do-Run` = 이전 창 끄기·확정 ≤10 s + 터널 ≤10 s + 경보 단말 3 s. `Start-InSession` 의 20 s 로는 모자랄 수 있다 |
| 실패 멈춤 | 30분에 3번 | heal 의 창 되살리기 상한(-652)과 같은 수 |
| EC2 재보고 | 300 s | 서버 메모리가 재배포로 비어도 화면이 5분 안에 다시 채워진다 |
| 터널 포트 | 17000·17001 | `Do-Run` 의 고정 로컬 포워드. 루프백 엔드포인트가 다르면 `ENDPOINT_PORTS_MISMATCH` |

## 버린 대안

- **`GET /api/endpoints/tier0` 만 보기**: 터널 방식에서 Pod 주소가 없고 끝 신호가 없다(위 조사).
- **관리자 `GET /api/gpu/sessions/current` 를 기기 토큰에도 열기**: 공급자·인스턴스 ID·가격·워커 ID 까지 노트북에 나간다. 관리자 JWT 체인과 기기 체인을 한 경로에 섞는다.
- **Pod 가 `tier0_endpoint` 에 실제 공개 IP 를 등록하고 노트북이 그 값으로 터널**: 중계·경보 단말은 엔드포인트 IP 로 바로 붙으므로(SSAFY 망에서 막힘) 중계 쪽 변경이 필요하고,
  I1 LIVE 계약·직접 접속 망 동작을 함께 건드린다.
- **heal(5분 주기, SYSTEM)에 넣기**: 관제 시작이 최대 5분 늦고, SYSTEM 이 관제 창을 띄우려면 결국 사용자 세션 예약 작업을 거친다. heal 은 작업자가 살아 있는지만 본다.
- **조회 실패도 유예 뒤 되돌리기**: 직접 접속 망에서 EC2 장애가 현장 경보를 끈다(명세 3.2).
- **세션 ID 로 같은 대상 판단**: 같은 Pod 의 재등록마다 관제를 끊는다.
- **수동 우선을 시간(예: 1시간)으로 풀기**: 손으로 끈 이유(카메라를 다른 도구에 쓰는 중 등)와 무관하게 깨어난다. "새 GPU 세션" 이 사람의 새 의도에 더 가깝다.
- **작업자 자식 프로세스로 run 띄우기**(관리자 권한 불필요): 작업자 예약 작업을 멈출 때 관제 창까지 끝날 수 있다.
- **전환 상태를 DB 표에 쌓기**: 운영 표시라 판정·감사에 쓰지 않고, 이력은 로그로 충분하다. V77 을 쓰지 않았다.

## 남은 위험

- **실기 미검증**: 게이트웨이 노트북(simai-gw)에서 돌려 보지 않았다. 관리자 권한 예약 작업(`Interactive` + `Highest`)이 로그인 때 UAC 없이 뜨는지, heal(SYSTEM)이
  사용자 작업을 등록·시작하는지, 작업자가 `simai-gateway-run` 을 고칠 수 있는지는 첫 적용 때 `gw.sh auto`·`gw.sh logs auto` 로 확인한다.
- **-700 전제**: Pod 가 `gpu_session_register` 에 실제 공개 주소와 17000·17001 의 **바깥** 포트(CONTROL 하나, VIDEO 한 번호)를 싣고, `tier0_endpoint` 는 지금처럼
  127.0.0.1:17000·17001(터널 방식)로, TLS 는 고정 키로 등록해야 한다. 다르면 `PORTS_UNSUPPORTED`·`ENDPOINT_PORTS_MISMATCH`·`PIN_MISMATCH` 로 멈춰 있을 뿐 엉뚱하게 붙지는 않는다.
  -700 이 부팅마다 새 키(b)를 쓰면 지문 갱신 방식을 따로 정해야 한다.
- **손으로 켠 세션(환경 변수 Tier 0 토큰)**: G1 세션 없이 Lead 가 켠 Pod 는 `gpu_session` 이 없어 자동 전환이 따라가지 않는다(`NO_SESSION`). 이때는 지금처럼 `gw.sh live`(수동 우선).
  다만 이전에 끝난 G1 세션이 가장 최근이면 수동 관제를 그대로 둔다(수동 주인) — 사람이 손으로 켰으니 맞다.
- **경합**: 사람이 명령을 치는 바로 그 순간(1초 안) 작업자가 전환을 시작하면 둘 중 늦은 쪽이 남는다. 작업자는 `simai-gateway-run`·`update` 예약 작업이 도는 동안은 건너뛴다.
- **헷갈리는 오래된 엔드포인트**: 새 세션 Tier 0 가 엔드포인트 등록에 실패하면(-543 같은 결함) 옛 세션의 같은 값(127.0.0.1:17000, 같은 고정 지문)을 보고 관제로 간다.
  터널 대상은 새 세션 주소라 실제 접속은 새 Pod 로 가므로 해는 없고, 등록 실패는 Tier 0 쪽 상태로 드러난다.
- **서버 메모리**: 여러 EC2 인스턴스가 되면 `gateway` 표시가 인스턴스마다 다르다(지금은 하나).

## 넘긴 일

- 명세 정리 MR: 12.6 기기 REST 표에 `GET /api/devices/gpu-session`·`POST /api/devices/gpu-session/gateway-state`, 12.2 노트북 → EC2 문단·14 노트북 게이트웨이 행에 자동 전환 조회,
  3.12 엔드포인트 등록 문단에 "세션 생존·터널 대상은 `gpu_session_status`" 를 더한다.
- -702 화면: `GET /api/gpu/sessions/current` 의 `gateway` 로 게이트웨이 상태 표시, 자동 정지 판단에 `reason_code`(`PIN_MISMATCH`·`BACKOFF`) 참고.
- -700: 위 "-700 전제" 대로 등록 값과 `simai-bootstrap lobby` 인자(`--public-host 127.0.0.1 --control-port 17000 --video-ports cam_*:17001`)를 맞춘다.

## 운영 적용

1. 서버(이 MR, 계약 v7.54.0) 배포가 먼저다. 게이트웨이가 먼저 가면 `INVALID_RESPONSE`(404)로 모드를 바꾸지 않는다.
2. `gw.sh update`(git pull + uv sync). 다음 heal(5분 안) 또는 `gw.sh heal` 이 `simai-gateway-auto` 를 등록·시작한다. 현장이면 `1`(관리자).
3. 확인: `gw.sh auto` → `자동 전환: 켬, 모드 주인 …, 작업자=Running`, GPU 가 없으면 `NO_SESSION`·`SESSION_ENDED`. GPU 를 켜면 `WAITING_TIER0` → `READY` → `RUNNING`.
4. 끄기: `gw.sh auto off`(지금 모드 그대로). 늘 끄기: 게이트웨이 `.env` `SIMAI_GATEWAY_AUTO_SWITCH=off`.

## 코드·시험 위치

- 게이트웨이: `gateway/ops/simai-gateway.ps1`(GPU 세션 자동 전환 절: `Get-AutoSwitchPlan`·`ConvertTo-GpuTarget`·`Get-EndpointCheck`·`Invoke-AutoSwitchCycle`·`Do-AutoWorker`·`Do-Auto`,
  주인 기록 `Set-OwnerForCommand`, heal `register-auto-task`·`start-auto-task`), `gateway/ops/gw.sh`(`auto`, `logs auto`), `gateway/ops/make-gateway-usb.ps1`(`.env` 키), `gateway/ops/README.md`.
- 서버: `server/.../domain/gpu/GpuGatewayController.java`(기기 API), `GatewaySwitchStates.java`(기기별 마지막 상태), `GpuSessionService.latestForGateway`·`isLaptop`,
  `GpuSessionController.current`(`gateway`), `ErrorCode.Gpu.GPU_NOT_A_LAPTOP`·`INVALID_GATEWAY_STATE`.
- 계약: `contracts/messages/gpu_session_status.schema.json`·`gateway_switch_state.schema.json`, 예시·반례 `contracts/examples/`.
- 시험: `gateway/ops/simai-gateway.Tests.ps1`(Pester 3.4: 순수 판정 13건, 가짜 EC2 응답으로 한 번 돌리기 9건 — 전환·같은 대상 재전환 없음·유예·조회 실패 유지·지문 불일치·
  수동 우선·실패 멈춤·주인 기록·넘겨받기), `server/.../domain/gpu/GpuGatewayIntegrationTest.java`(계약 검사·포트 거르기·ENDED·권한·전환 상태 → 관리자 조회).
