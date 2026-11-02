# gateway/ops — 로비 게이트웨이 노트북 도우미

로비 카메라 4대 옆의 게이트웨이 노트북에서 영상 중계(`gateway/relay`)와 현장 경보 단말(`gateway/site_alert`)을 설치·점검·실행한다(S15P31A101-393).
개발 노트북과 분리해 둔다. **영상 중계·경보 단말은 한 번에 한 노트북에서만** 돌린다(같은 기기 토큰, C110 RTSP 동시 2세션 한도).

| 파일 | 하는 일 |
|---|---|
| `simai-gateway.cmd` | 더블클릭 → 메뉴. 명령줄 `simai-gateway.cmd run -PodIp … -ControlPort … -VideoPort …` |
| `simai-gateway.ps1` | 1 설치·갱신, 2 점검, 3 관제 시작, 4 끄기, 5 카메라 미리보기만, 6~8 원격(S15P31A101-394) (Windows PowerShell 5.1) |
| `make-gateway-usb.ps1` | 개발 노트북에서 첫 설치용 폴더(도우미 + 비밀 묶음)를 만든다 |
| `../relay/configs/lobby.toml` | 로비 카메라 cam_A~D 주소(공유기 DHCP 예약 사설 IP, 계정 없음) |

## 처음 한 번

1. 게이트웨이 노트북: Git, uv 설치. 공유기에 **유선 LAN** 연결(카메라가 2.4GHz Wi-Fi 를 쓴다), 절전 해제.
2. 개발 노트북: `powershell -ExecutionPolicy Bypass -File gateway\ops\make-gateway-usb.ps1 -Out E:\simai-gateway`
   - 루트 `.env` 에서 게이트웨이에 필요한 키만 `bundle\gateway.env` 로 옮긴다: `RTSP_*`, `SIMAI_EC2_BASE_URL`, `SIMAI_DEVICE_TOKEN`, `SIMAI_TIER0_PINNED_SPKI_SHA256`, `SIMAI_LAPTOP_ID` 등. 값은 출력하지 않는다.
   - EC2 키(pem)를 `bundle\` 에 넣는다. USB 로만 옮긴다(메신저·메일 금지).
3. 게이트웨이: 폴더를 `C:\simai-gateway` 로 복사 → `simai-gateway.cmd` 를 **관리자 권한으로** 실행(시계 동기화 때문) → `1`.
   저장소를 `C:\simai\S15P31A101` 에 받고(dev), `uv sync`, `.env`·키·`~/.ssh/config` 의 `simai-ec2` 를 넣고, 묶음을 지운 뒤, 바탕화면에 `simai 게이트웨이` 바로가기를 만든다.
4. `2` 점검: EC2 SSH, 카메라 RTSP 554, 30초 수신(전송 없음), 경보 단말 시험 소리.

## 그 뒤

- 개발이 진행되면: 관제를 끄고(`4`) → `1`(git pull + uv sync, 도우미 자신도 갱신) → `3`.
  도우미가 바뀌었으면 창을 닫고 바로가기로 다시 열라고 알린다.
- 게이트웨이 저장소에서 파일을 고치지 않는다. 고친 파일이 있으면 갱신을 건너뛴다.

## 관제 시작(3)

Pod 공개 IP 와 17000·17001 의 바깥 매핑 포트를 묻고(`C:\simai-local\gateway-pod.json` 에 남겨 다음에 재사용), 창 3개를 띄운다.

1. 터널: `ssh -N simai-ec2 -L 127.0.0.1:17000:<Pod IP>:<매핑> -L 127.0.0.1:17001:<Pod IP>:<매핑>`, 끊기면 3초 뒤 다시.
   SSAFY 망은 EC2(simai.live:22)만 열려 있어 EC2 를 거친다. EC2→Pod 는 연결마다 따로 TCP 라 카메라별 독립 연결(명세 12.2)을 지키고,
   TLS 는 Tier 0 가 끝낸다(경로 결정은 S15P31A101-392).
2. 현장 경보 단말 `uv run simai-site-alert run`
3. 영상 중계 `uv run simai-relay run --config ../relay/configs/lobby.toml --send --preview-port 47032`
   (CCTV 미리보기는 게이트웨이 노트북 브라우저에서만 보인다, 127.0.0.1)

끄기(`4`)는 세 창의 프로세스 트리를 끝낸다.

## GPU Pod 가 없을 때

관제(3)는 Pod 가 있어야 의미가 있다. Pod 없이 켜 두면 터널·중계가 계속 다시 붙으려 하고, 경보 단말은 "관제 중단" 을 띄우며,
EC2 하트비트로 원격 화면에도 관제 중단이 보인다(실제 상태라 틀린 표시는 아니다). Pod 주소·포트는 Pod 마다 바뀌므로
Pod 가 새로 뜨면 3 을 새 값으로 다시 한다(원격이면 `run-remote`).

Pod 없이 카메라만 보려면 `5`(미리보기만): 영상 중계를 전송 없이 띄우고 관제 앱 CCTV 로컬 미리보기(127.0.0.1:47032)만 준다.
같은 노트북 브라우저의 simai.live 관제 화면에서 본다(원격 데스크톱으로 보면 된다).

## 원격 (S15P31A101-394)

게이트웨이가 EC2 로 역터널을 걸어 두고(EC2 `127.0.0.1:2222` → 게이트웨이 sshd, `127.0.0.1:13389` → 원격 데스크톱),
개발 노트북은 EC2 를 거쳐 들어온다. 개발 노트북이 simai.live:22 에 붙을 수 있으면 어디서든 된다(SSAFY 망·휴대폰 핫스팟 확인, 2026-11-02).
EC2 sshd 의 GatewayPorts 기본값(no) 때문에 EC2 루프백에만 열리고, 게이트웨이 sshd 는 127.0.0.1 에서만 듣고 키 인증만 받는다.

처음 한 번(순서대로):

1. 개발 노트북: `gateway\ops\simai-gateway.cmd dev-remote`(메뉴 7) — `~/.ssh/id_ed25519.pub` 를 EC2 `~/simai-gateway/dev_keys.pub` 에 올리고 `~/.ssh/config` 에 `simai-gw`(ProxyJump simai-ec2) 를 둔다.
2. 게이트웨이: 바로가기를 **관리자 권한으로** 열어 `6` — OpenSSH 서버(127.0.0.1·키만. sshd 가 없으면 공식 Win32-OpenSSH MSI 를 버전·SHA-256 고정으로 설치: Windows 기능 설치는 SSAFY 관리 PC 에서 멈춘다, S15P31A101-398), 공개키 등록, 원격 데스크톱 켜기, 로그인 때 뜨는 역터널 예약 작업 `simai-gateway-remote`(끊기면 다시, 로그 `C:\simai-local\gateway-remote-tunnel.log`).
3. 개발 노트북: 7 을 한 번 더(게이트웨이 사용자 이름을 EC2 에서 받아 `simai-gw` 설정을 만든다) → `ssh simai-gw hostname` 이 되면 끝.
   공개키를 6 뒤에 올렸으면 게이트웨이에서 6 을 다시 한다(공개키를 가져감).

쓰기:

- 원격 데스크톱: 개발 노트북 `simai-gateway.cmd rdp`(메뉴 8). 경보 소리가 로비에서 나게 "로컬 리소스 → 원격 오디오 → 원격 컴퓨터에서 재생".
- 명령만: `ssh simai-gw` 뒤
  - 갱신 `powershell -ExecutionPolicy Bypass -File C:\simai\S15P31A101\gateway\ops\simai-gateway.ps1 setup`
  - 관제 시작 `… simai-gateway.ps1 run-remote -PodIp <IP> -ControlPort <17000 매핑> -VideoPort <17001 매핑>` (SSH 세션의 창은 화면에 안 보이므로 로그인 세션의 예약 작업으로 `run` 을 띄운다)
  - 미리보기만 `… simai-gateway.ps1 preview-remote`
  - 끄기 `… simai-gateway.ps1 stop` (관제·미리보기 공통, 모드를 바꿀 때는 끄지 않고 바로 다른 모드를 시작해도 된다)
  - 상태 `… simai-gateway.ps1 status` (모드·창 생존·역터널 작업·카메라 RTSP 응답 수)
- 조건: 게이트웨이는 전원 연결·절전 해제·Windows 로그인 상태(재부팅 뒤 로그인 전에는 역터널이 없다).
- 끊긴 세션이 EC2 포트를 쥐고 있으면 역터널이 그 포트를 쥔 옛 ubuntu sshd 를 끝내고 다시 붙는다(`~/simai-gateway/free-ports.sh`).
