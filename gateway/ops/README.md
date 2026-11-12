# gateway/ops — 로비 게이트웨이 노트북 도우미

로비 카메라 4대 옆의 게이트웨이 노트북에서 영상 중계(`gateway/relay`)와 현장 경보 단말(`gateway/site_alert`)을 설치·점검·실행한다(S15P31A101-393).
개발 노트북과 분리해 둔다. **영상 중계·경보 단말은 한 번에 한 노트북에서만** 돌린다(같은 기기 토큰, C110 RTSP 동시 2세션 한도).

| 파일 | 하는 일 |
|---|---|
| `simai-gateway.cmd` | 더블클릭 → 메뉴. 명령줄 `simai-gateway.cmd run -PodIp … -ControlPort … -VideoPort …` |
| `simai-gateway.ps1` | 1 설치·갱신, 2 점검, 3 관제 시작, 4 끄기, 5 카메라 미리보기만, 6~8 원격(S15P31A101-394), 9 상태, h 자가 복구(`heal`), SSH 전용 `update-remote`·`logs`·`restart-tunnel`(S15P31A101-414·424) (Windows PowerShell 5.1) |
| `gw.sh` | 개발 노트북(Git Bash)에서 한 줄로 게이트웨이 상태·관제·미리보기·갱신·자가 복구·로그·접속 실패 진단을 수행하는 원격 CLI(S15P31A101-424) |
| `make-gateway-usb.ps1` | 개발 노트북에서 첫 설치용 폴더(도우미 + 비밀 묶음)를 만든다 |
| `../relay/configs/lobby.toml` | 로비 카메라 cam_A~D 주소(공유기 DHCP 예약 사설 IP, 계정 없음) |

## 처음 한 번

1. 게이트웨이 노트북: Git, uv 설치. 공유기에 **유선 LAN** 연결(카메라가 2.4GHz Wi-Fi 를 쓴다), 절전 해제.
2. 개발 노트북: `powershell -ExecutionPolicy Bypass -File gateway\ops\make-gateway-usb.ps1 -Out E:\simai-gateway`
   - 루트 `.env` 에서 게이트웨이에 필요한 키만 `bundle\gateway.env` 로 옮긴다: `RTSP_*`, `SIMAI_EC2_BASE_URL`, `SIMAI_DEVICE_TOKEN`, `SIMAI_TIER0_PINNED_SPKI_SHA256`, `SIMAI_LAPTOP_ID` 등. 값은 출력하지 않는다.
   - EC2 키(pem)를 `bundle\` 에 넣는다. USB 로만 옮긴다(메신저·메일 금지).
3. 게이트웨이: 폴더를 `C:\simai-gateway` 로 복사 → `simai-gateway.cmd` 를 **관리자 권한으로** 실행(시계 동기화·예약 작업 등록 때문) → `1`.
   저장소를 `C:\simai\S15P31A101` 에 받고(dev), `uv sync`, `.env`·키·`~/.ssh/config` 의 `simai-ec2` 를 넣고, 묶음을 지운 뒤, 바탕화면에 `simai 게이트웨이` 바로가기를 만든다.
4. `2` 점검: EC2 SSH, 카메라 RTSP 554, 30초 수신(전송 없음), 경보 단말 시험 소리.

## 그 뒤

- 개발이 진행되면:
  - 원격(개발 노트북 Git Bash): `gateway/ops/gw.sh update` (아래 "원격 관리").
  - 현장(게이트웨이 화면): 관제를 끄고(`4`) → `1`(git pull + uv sync, 도우미 자신도 갱신) → `3`. 도우미가 바뀌었으면 창을 닫고 바로가기로 다시 열라고 알린다.
- 게이트웨이 저장소에서 파일을 고치지 않는다. 고친 파일이 있으면 갱신을 건너뛴다.

## 관제 시작(3)

Pod 공개 IP 와 17000·17001 의 바깥 매핑 포트를 묻고(`C:\simai-local\gateway-pod.json` 에 남겨 다음에 재사용), 창 3개를 띄운다.

1. 터널: `ssh -N simai-ec2 -L 127.0.0.1:17000:<Pod IP>:<매핑> -L 127.0.0.1:17001:<Pod IP>:<매핑>`, 끊기면 3초 뒤 다시.
   SSAFY 망은 EC2(simai.live:22)만 열려 있어 EC2 를 거친다. EC2→Pod 는 연결마다 따로 TCP 라 카메라별 독립 연결(명세 12.2)을 지키고,
   TLS 는 Tier 0 가 끝낸다(경로 결정은 S15P31A101-392).
2. 현장 경보 단말 `uv run simai-site-alert run`
3. 영상 중계 `uv run simai-relay run --config ../relay/configs/lobby.toml --send --preview-port 47032 --remote-cctv --record-dir C:\simai-local\recordings\live-<yyyyMMdd-HHmmss>`
   (CCTV 는 게이트웨이 노트북 브라우저는 로컬 127.0.0.1 로, 노트북 밖 관리자는 원격 시청으로 본다. 원격 시청은 EC2 에 늘 붙어 있다가
   보는 사람이 있을 때만 올린다(S15P31A101-400). 관제 중 Tier 0 지연이 늘면 `-NoRemoteCctv` 로 끈다: `simai-gateway.cmd run -NoRemoteCctv …`.
   관제 모드는 기본으로 보내는 프레임을 `C:\simai-local\recordings\live-<yyyyMMdd-HHmmss>` 에 동시 녹화하며(S15P31A101-554), 녹화를 끄려면 `-NoRecord` 또는 `gw.sh live … --no-record` 를 준다. 미리보기 모드는 녹화하지 않는다)

끄기(`4`)는 세 창의 프로세스 트리를 끝낸다.

## GPU Pod 가 없을 때

관제(3)는 Pod 가 있어야 의미가 있다. Pod 없이 켜 두면 터널·중계가 계속 다시 붙으려 하고, 경보 단말은 "관제 중단" 을 띄우며,
EC2 하트비트로 원격 화면에도 관제 중단이 보인다(실제 상태라 틀린 표시는 아니다). Pod 주소·포트는 Pod 마다 바뀌므로
Pod 가 새로 뜨면 3 을 새 값으로 다시 한다(원격이면 `gw.sh live <IP> <17000 매핑> <17001 매핑>`).

Pod 없이 카메라만 보려면 `5`(미리보기만, 원격이면 `gw.sh preview`): 영상 중계를 Tier 0 전송 없이 띄우고 관제 앱 CCTV 로컬 미리보기(127.0.0.1:47032)와
원격 시청 업로드(`--remote-cctv`)를 준다. 같은 노트북 브라우저나 로그인한 관리자의 다른 PC 의 simai.live CCTV 화면에서 본다
(원격 시청을 끄려면 `-NoRemoteCctv`).

## 원격 설정 (S15P31A101-394)

게이트웨이가 EC2 로 역터널을 걸어 두고(EC2 `127.0.0.1:2222` → 게이트웨이 sshd, `127.0.0.1:13389` → 원격 데스크톱),
개발 노트북은 EC2 를 거쳐 들어온다. 개발 노트북이 simai.live:22 에 붙을 수 있으면 어디서든 된다(SSAFY 망·휴대폰 핫스팟 확인, 2026-11-02).
EC2 sshd 의 GatewayPorts 기본값(no) 때문에 EC2 루프백에만 열리고, 게이트웨이 sshd 는 127.0.0.1 에서만 듣고 키 인증만 받는다.

**처음 한 번 현장에서 메뉴 6번 실행 필요(순서대로)**:

1. 개발 노트북: `gateway\ops\simai-gateway.cmd dev-remote`(메뉴 7) — `~/.ssh/id_ed25519.pub` 를 EC2 `~/simai-gateway/dev_keys.pub` 에 올리고 `~/.ssh/config` 에 `simai-gw`(ProxyJump simai-ec2) 를 둔다.
2. 게이트웨이(현장 1회): 바로가기를 **관리자 권한으로** 열어 `6`(`remote-setup`) 실행:
   - OpenSSH 서버(127.0.0.1·키 인증 전용, 공식 Win32-OpenSSH MSI 해시 고정 설치, 실패 시 5s/5s/60s 자동 재시작 옵션 설정)
   - 개발 노트북 공개키 등록(`administrators_authorized_keys`)
   - 원격 데스크톱 활성화 및 AC 절전 해제(화면 0, 절전 0, 덮개 닫기: 아무 것도 안 함)
   - EC2 옛 포트 정리 스크립트(`~/simai-gateway/free-ports.sh`, `sudo -n ss`/`kill`) 업로드
   - 로그인 시 뜨는 역터널 예약 작업 `simai-gateway-remote` 등록·시작(로그 `C:\simai-local\gateway-remote-tunnel.log`)
   - 자가 복구 감시 예약 작업 `simai-gateway-heal`(부팅·로그온·5분마다, `SYSTEM` 최고 권한) 등록
3. 개발 노트북: 7 을 한 번 더(게이트웨이 사용자 이름을 EC2 에서 받아 `simai-gw` 설정을 만든다) → `gateway/ops/gw.sh status` 가 되면 끝.

## 원격 관리 (S15P31A101-424)

### 1. 자가 복구(`heal`) 범위와 주기

- **실행 주기**: 예약 작업 `simai-gateway-heal` 이 **부팅 시, 로그온 시, 5분마다** 자동 실행된다(`Do-RemoteSetup` 6번 및 `Do-Setup` 1번이 등록·갱신). 수동으로는 개발 노트북에서 `gateway/ops/gw.sh heal` 또는 현장 메뉴 `h` 로 즉시 실행할 수 있다.
- **권한 분리**:
  - `sshd` 서비스·실패 복구 옵션·`powercfg` 절전 설정은 `SYSTEM` 권한 예약 작업(`simai-gateway-heal`)에서 복구한다.
  - 중계 창 띄우기는 로그인한 사용자 세션 예약 작업(`simai-gateway-run`, `Start-InSession`)으로 띄운다.
- **점검·복구 항목과 기록**:
  - 실제로 고친 항목만 `C:\simai-local\gateway-heal.log` 에 한 줄씩 남긴다(크기 1 MB 초과 시 `gateway-heal.log.old` 로 1회 순환).
  1. **sshd 서비스**: `Running` 이 아니면 시작하고 시작 유형을 `Automatic` 으로 맞춘다.
  2. **sshd 실패 시 자동 재시작 옵션**: `sc.exe failure sshd reset= 86400 actions= restart/5000/restart/5000/restart/60000` 이 없으면 설정한다.
  3. **원격 역터널(`simai-gateway-remote`)**: 예약 작업이 `Running` 이 아니면 시작한다. 역터널 `ssh` 프로세스가 없거나 터널 로그의 마지막 성공 시각 이후 5분(300초) 넘게 지났으면 예약 작업을 멈추고 EC2 `~/simai-gateway/free-ports.sh` 로 옛 세션을 정리한 뒤 다시 시작한다.
  4. **절전 설정(`powercfg`)**: AC 전원 기준 화면 끄기(`monitor-timeout-ac 0`), 절전(`standby-timeout-ac 0`), 덮개 닫기 동작(`LIDACTION 0`, 아무 것도 안 함)이 바뀌었으면 0으로 되돌린다.
  5. **중계 모드**: 마지막으로 사용자가 고른 모드(`C:\simai-local\gateway-mode.txt`)의 창이 죽어 있으면 그 모드로 다시 띄운다(미리보기, 그리고 상태 파일에 Pod 주소·포트가 있는 `run`(관제), S15P31A101-652). 창 PID 만이 아니라 창 안의 프로그램(uv·simai-relay·simai-site-alert 자식)까지 본다: 창은 `-NoExit` 라 중계가 죽어도 열린 채 남아 11-09 에 heal 이 "이상 없음"이었다(관제 터널 창은 재연결 대기 중 자식이 없어 창만 본다). 30분 안에 3번 되살렸는데 또 죽으면 더 되살리지 않고 heal 로그에 `되살리지 않음` 을 남긴다. Pod 주소 기록이 없는 `run` 은 예전처럼 `"관제가 꺼짐"` 으로만 알린다.

### 2. 개발 노트북 원격 CLI (`gateway/ops/gw.sh`)

개발 노트북 Git Bash 에서 실행한다(인자 없이 실행하면 도움말 출력). `simai-gateway.ps1` 이 콘솔 출력 인코딩을 UTF-8 로 맞추므로 별도 `iconv` 변환 없이 한글이 깨지지 않는다.

| 명령 | 동작 |
|---|---|
| `gateway/ops/gw.sh status` | 한 화면 상태 확인: 현재 모드(관제 창 사망 시 `관제가 꺼짐`), 창 PID 생존, 녹화 폴더·용량·버린 프레임 수, `sshd` 상태·시작 유형·실패 옵션, 역터널 작업·프로세스·마지막 연결 시각, 카메라 RTSP 응답(`n / 4`), 저장소 커밋, 디스크 여유, 절전 설정 이상 여부, `heal` 로그 마지막 3줄 |
| `gateway/ops/gw.sh live <PodIP> <17000 매핑> <17001 매핑> [--no-record] [--no-remote-cctv]` | 로그인 세션에서 관제(창 3개: 터널·경보 단말·영상 중계 + 기본 동시 녹화) 시작 후 `status` 출력 |
| `gateway/ops/gw.sh preview` | 로그인 세션에서 카메라 미리보기(Tier 0 전송 없음, 원격 시청 포함) 시작 후 `status` 출력 |
| `gateway/ops/gw.sh stop` | 관제·미리보기 창 종료 후 `status` 출력 |
| `gateway/ops/gw.sh update` | 현재 모드를 잠시 멈추고 로그인 세션 예약 작업(`simai-gateway-update`)으로 `git pull --ff-only` + `uv sync` 수행 후 이전 모드 복원 및 `status` 출력 |
| `gateway/ops/gw.sh heal` | 자가 복구(`heal`) 즉시 실행 후 `status` 출력 |
| `gateway/ops/gw.sh logs <relay\|tunnel\|update\|heal> [N]` | 지정한 로그(`C:\simai-local\gateway-*.log`)의 마지막 `N`줄(기본 40줄) 출력 |
| `gateway/ops/gw.sh restart-tunnel` | 역터널 중지 → EC2 옛 포트 정리(`free-ports.sh`) → 역터널 예약 작업 재시작 후 `status` 출력 |
| `gateway/ops/gw.sh ssh` | 게이트웨이 대화형 SSH 셸 접속 |
| `gateway/ops/gw.sh help` | 명령 도움말 출력 |

### 3. 장애 유형별 자동 복구 및 대응

`gw.sh` 명령이 SSH 연결 단계(종료 코드 255)에서 실패하면, `gw.sh` 가 자동으로 EC2(`simai-ec2`)에 읽기 전용으로 접속해 `127.0.0.1:2222` 리슨 여부와 해당 `sshd` 세션의 PID·나이(`etime`)를 조회하고 원인별 조치를 안내한다(EC2 에 쓰기나 `kill` 은 하지 않는다).

| 장애 유형 | 증상 및 `gw.sh` 진단 결과 | 자동 복구 및 대응 |
|---|---|---|
| **게이트웨이 `sshd` 꺼짐** | EC2 `127.0.0.1:2222` 리슨은 있으나 `kex_exchange_identification: Connection closed` 등으로 실패 | 1차로 Windows 서비스 실패 복구 옵션(`sc.exe failure sshd`, 5초 뒤 재시작)이 즉시 되살린다. 그래도 꺼져 있으면 `simai-gateway-heal`(5분 주기)이 `Start-Service sshd` 로 복구한다. (처음 6번 적용 전이면 현장에서 메뉴 6 실행) |
| **역터널 끊김** | EC2 `127.0.0.1:2222` 리슨 없음 | 역터널 루프가 5초마다 재접속을 시도하며, 작업이나 프로세스가 죽었으면 `simai-gateway-heal`(5분 주기)이 재시작한다. 5분 뒤에도 안 열리면 현장에서 전원·로그인·유선 LAN 을 확인한다. |
| **EC2 옛 세션이 2222 점유** | 게이트웨이 망 순단 뒤 EC2 에 옛 `sshd` 세션이 남아 새 역터널 포워딩이 거절됨 | 역터널 루프(20초 미만 끊김 시) 또는 `simai-gateway-heal` / `gw.sh restart-tunnel` 이 EC2 `~/simai-gateway/free-ports.sh`(`sudo -n ss`/`kill`)를 호출해 옛 세션을 정리하고 다시 연결한다. |
| **절전(Modern Standby)·덮개 닫기** | 시간 경과나 덮개 닫기로 절전 진입 후 망·중계 끊김 | `Do-RemoteSetup`(6번)과 `simai-gateway-heal`(5분 주기)이 AC 화면·절전·덮개 닫기 동작을 항상 `0`(아무 것도 안 함)으로 되돌린다. |
| **미리보기/관제 창 종료** | 중계 프로세스 비정상 종료(창은 열린 채 안의 프로그램만 죽은 경우 포함) | `simai-gateway-heal`(5분 주기)이 이전 모드(미리보기, 또는 마지막 Pod 주소의 관제)로 로그인 세션에서 다시 띄운다(30분에 3번까지). 그 뒤에도 죽거나 Pod 주소 기록이 없으면 `heal` 로그·`status` 에 남기므로 `gw.sh logs relay` 로 원인을 보고 `gw.sh live ...` 로 다시 켠다. Pod 를 바꿨으면 heal 이 옛 주소로 띄울 수 있으니 새 주소로 `gw.sh live` 를 다시 준다. |
