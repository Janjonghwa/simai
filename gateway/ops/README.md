# gateway/ops — 로비 게이트웨이 노트북 도우미

로비 카메라 4대 옆의 게이트웨이 노트북에서 영상 중계(`gateway/relay`)와 현장 경보 단말(`gateway/site_alert`)을 설치·점검·실행한다(S15P31A101-393).
개발 노트북과 분리해 둔다. **영상 중계·경보 단말은 한 번에 한 노트북에서만** 돌린다(같은 기기 토큰, C110 RTSP 동시 2세션 한도).

| 파일 | 하는 일 |
|---|---|
| `simai-gateway.cmd` | 더블클릭 → 메뉴. 명령줄 `simai-gateway.cmd run -PodIp … -ControlPort … -VideoPort …` |
| `simai-gateway.ps1` | 1 설치·갱신, 2 점검, 3 관제 시작, 4 관제 끄기 (Windows PowerShell 5.1) |
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
