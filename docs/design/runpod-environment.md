# RunPod 환경: Pod 점검, Network Volume 구조, 접속, GPU 1차 세션 큐

S15P31A101-183(GPU 1차 세션 검증 큐)이 S15P31A101-31(RunPod 점검·Volume 구조)과 S15P31A101-92(강제 종료 복구 실측)의 노트북 준비분을 함께 담았다.
근거는 명세 3.12(리전·GPU·저장소·연결), 19.1(볼륨 구조), 3.2(저널 계약·재시작 복구), 11.5(보관), 15.1 G3·부록 D.4, 20.5(GPU Job Queue), AGENTS.md RunPod 규칙이다.
이미지·템플릿·엔진 캐시는 `deploy/README.md`, 재시작 복구 운영 절차는 `docs/design/tier0-recovery.md` 에 있다.

## 원칙

- GPU·지역은 고정하지 않는다(2026-10-20 결정, S15P31A101-205): 개발·시험은 RunPod 의 그때그때 남은 GPU·지역(볼륨 없음, 컨테이너 디스크 100GB+), 예비는 Vast.ai,
  시연 사이트는 미정(엘리스 등 후보)이고, GPU VM + 블록 스토리지면 `docker run`(deploy/README "GPU VM(엘리스) docker run"). 일본 Network Volume 은 폐기했다. 한 시점에 GPU 하나, Tier 0 가 먼저 쓴다.
  아래 "Network Volume 디렉터리 구조"는 저장 위치(`$SIMAI_ROOT`: 볼륨·블록 스토리지·컨테이너 디스크) 안의 구조로 읽는다. 저장 위치는 `simai-bootstrap` 이 고른다.
- GPU Pod 는 검증할 때만 켠다. 코드·스크립트·큐는 노트북에서 끝내고(이 문서의 드라이런), Pod 에서는 큐를 돌린 뒤 결과를 가져오고 바로 Stop 한다.
- LIVE 관제 시간에는 세션 큐를 돌리지 않는다(세션 실행기가 Tier 0 표시 파일·프로세스를 보고 시작을 거부한다). EXP Job 은 LIVE 보호를 켠 별도 큐로 마지막에 돈다.
- 접속은 Pod 공개 IP:포트 SSH 직접 연결(키 인증만)이 기본이고, SSAFY 망(시연장 포함)에서는 팀 EC2(`simai.live:22`) SSH 경유 + Pod 공개 포트 Direct TCP 를 기본으로 쓰며(S15P31A101-392, `deploy/README.md` "SSAFY 망 기본 경로"), RunPod HTTPS 프록시(443) 위 wstunnel SSH 는 최후 예비다. Tailscale 은 쓰지 않는다.
- HF 토큰은 환경 변수 `HF_TOKEN` 으로만 받는다(RunPod Secret `{{ RUNPOD_SECRET_hf_token }}`, Vast 계정 환경 변수, 엘리스는 블록 스토리지 `.simai-private/hf.env` chmod 600 을 `docker run --env-file`).
  `simai-bootstrap` 이 가중치를 받을 때만 쓰고 Tier 0·큐를 띄우기 전에 지운다. 다른 비밀은 `.simai-private/` 에만 둔다. 이미지·저장소·로그·결과 묶음에 넣지 않는다.

## Network Volume 디렉터리 구조 (`/workspace`)

Network Volume 은 파일 권한(chmod)이 적용되지 않는다(명세 3.12). 아래 "권한"은 파일 모드가 아니라 **누가 쓰고 누가 지우는지**의 운영 규칙이다.
볼륨을 붙인 Pod 에 들어온 사람은 모든 파일을 읽을 수 있으므로, 볼륨은 팀 GPU Pod 에만 붙이고 다른 용도의 Pod·외부 사람에게 붙이지 않는다.

| 경로 | 내용 | 쓰는 것 | 지우는 규칙·보관 | 근거 |
|---|---|---|---|---|
| `simai/weights/` | 모델 가중치(`yolo/yolo26x-pose.pt`, `sam-3d-body-dinov3*`, `map-anything*`) | 사람(설치 스크립트) | 프로젝트 기간 유지. 해시 목록은 GitLab 에도 남겨 다시 받을 수 있게 한다. 덮어쓰지 않고 새 이름으로 | 3.12, deploy/README |
| `simai/datasets/` | 공개 데이터셋(CMU Panoptic 등)·실험 입력 | 사람 | 라이선스·동의 범위 안에서만. 프로젝트 발표 후 30일 안 파기 | 11.5, docs/data/cmu_panoptic.md |
| `simai/recordings/<session_id>/` | 녹화 세션(영상·사이드카·`session.json`·`manifest.json`) | `gateway/recorder` | `consent` 의 보관 기한이 지나면 지우고 저장소 manifest 에 `deleted_at_ms` 를 더한다 | 15.3, docs/data/recording-manifest.md |
| `simai/obs2d/` | 2D 관측 Parquet (`{date}/{hour}/{camera_id}/...`) | Tier 0 (`SIMAI_OBS2D_ROOT`) | 기본 14일(7~14일) 뒤 삭제 | 11.2, 11.5, 19.1 |
| `simai/engines/` | TensorRT 엔진 캐시 `{model}/{sm_xx}/{trt 버전}/` + 메타데이터 | Tier 0 진입점(엔진 준비) | 다시 만들 수 있다. 키가 다른 파일은 지우지 않고 `*.conflict-<ms>` 로 옆에 옮긴다 | 3.12, deploy/README |
| `simai/state/` | Tier 0 상태: `journal/`(세그먼트·`acked_offset.json`·`evidence_index.json`), `checkpoints/`, `config/`(구성 묶음 보관소), `calibration_baselines.json`, `tier0.live`(실행 중 표시) | Tier 0 (단일 작성자, flock) | **손으로 지우지 않는다.** 저널 정리는 Tier 0 가 acked_offset·체크포인트 하한으로만 한다(세그먼트 64 MiB, 보관 24시간). 시작 거부 때 조치는 tier0-recovery.md | 3.2, tier0-recovery.md |
| `simai/queue/` | GPU 큐 잠금(`queue.lock`)·heartbeat·`<queue>/state.json`·`queue.log`·`runs/<run_id>/`(job.log·run_record.json·metrics.json) | `ai/experiments/gpu_queue` | 추가만(시도마다 새 run 폴더). Run 요약을 노트북으로 가져온 뒤에도 남긴다. 프로젝트 발표 후 30일 안 파기 | 20.5, gpu_queue/README |
| `simai/runs/` | 실험·검증 산출물: `exp01/`, `exp02/`, `crash92/<UTC>/`(강제 종료 실측 상태·result.json), `gpu-session1/<세션>/`(Job 파일·묶음·요약) | 실험 Job·세션 실행기 | 추가만. 노트북 사본은 `ai/experiments/runs/`(커밋하지 않음). 프로젝트 발표 후 30일 안 파기 | 19.1, 20.5 |
| `simai/setup/` | Pod 에 올린 저장소 사본(`session1/repo/`, git archive + `git-stamp.json`)·설치 스크립트 사본 | 사람 | 세션마다 새로 풀어도 된다(코드 정본은 GitLab) | 19.1 |
| `simai/logs/` | 수동 실행 로그 | 사람 | 정리 자유 | 19.1 |
| `simai/envs/`, `third_party/`, `tools/` | E-GATE-0 때의 볼륨 환경(예외). Docker 이미지로 옮긴 뒤에는 새로 만들지 않는다 | (예전) 설치 스크립트 | 이미지 다이제스트로 재현되면 정리 | 3.12, 19.1 |
| `.simai-private/` | 비밀: `hf.env`, `worker.env`(`SIMAI_WORKER_TOKEN=`, `simai-wait-tier0 --env-file` 이 읽음), Tier 0 TLS 개인 키, 기기 토큰 | 사람만 | 내용을 로그·결과 묶음·이미지에 넣지 않는다. 점검 스크립트는 있는지·크기만 본다. 볼륨 밖으로 복사하지 않는다 | 3.12, deploy/README |

컨테이너 디스크(Pod 재시작마다 비워짐): 이미지(`/opt/simai`, 풀면 약 19.5GB), 세션 시험 도구(`/tmp/simai-session1-testtools`), 묶음 예비 위치(`/tmp/simai-session1/`).

명세 19.1 목록과 다른 점: `engines`(3.12 에는 있음), `state`(Tier 0 설정 기본값 `$SIMAI_ROOT/state`), `queue`(gpu_queue 기본값)는 19.1 목록에 없다. 명세 갱신은 -172 몫이다(MR '남은 일').

## Pod 점검 (`deploy/scripts/pod_check.py`, -31)

GPU 세션 큐의 첫 항목이다. 표준 라이브러리만 쓰고 결과를 `$SIMAI_RUN_DIR/pod_check.json`(Run 기록 metrics 에 요약)으로 남긴다. 단독 실행: `python deploy/scripts/pod_check.py`.

| 항목 | 통과 | WARN | FAIL |
|---|---|---|---|
| GPU | 최소 사양(Ampere 이상, VRAM 24GB급) GPU 하나. Blackwell 은 Tier 0·실험 이미지 모두 커널이 있다(-207) | 여러 개, compute_cap·메모리를 읽지 못함 | nvidia-smi 실패, 최소 사양 미만 |
| 드라이버 | 580 이상(CUDA 13 요구) | 580 미만: 이미지의 순방향 호환에 기댄다. 실제 판정은 b 항목 `torch.cuda.is_available()` | 버전을 읽지 못함 |
| CUDA | 드라이버 최대 CUDA 13.0 이상 | 13.0 미만(Pod CUDA 필터를 13.0 이상으로) | |
| 마운트 | `/workspace` 가 마운트 지점(장치·파일시스템 기록). 볼륨 없는 Pod(`--no-volume`, 세션 큐가 마운트 지점이 아니면 붙인다)는 '볼륨 없음' SKIP | | 마운트 지점이 아님(`--no-volume` 없이) |
| 데이터센터 | `RUNPOD_DC_ID` 기록(어느 지역이든 PASS) | | |
| 용량 | 볼륨 50GB 이상 남음(조정 `--min-free-gb`), 컨테이너 10GB 이상 | 컨테이너 10GB 미만 | 볼륨 하한 미만 |
| 볼륨 디렉터리·가중치·`hf.env` | 있음 | 없음(처음이면 만든다. 가중치가 없으면 b 가 실패) | |
| 쓰기 속도 | 1GB 쓰기+fsync 시간·MB/s 기록(볼륨 없음: SKIP) | | 쓰기 오류 |
| fsync | 400B 추가+fsync 200회 p50·p95·p99·max 기록, p99 50ms 이하(볼륨 없음: SKIP) | p99 50ms 초과(저널 쓰기 정체·체크포인트 지연 위험) | |
| rename·flock | `os.replace` 원자성, 같은 파일 두 번째 flock 거부(저널 단일 작성자 잠금) | | 실패(볼륨이 잠금을 무시) |

## 접속

### SSH 직접 (기본)

- RunPod 템플릿은 TCP 22 를 공개하고, `simai-start` 가 `PUBLIC_KEY`(RunPod 계정 SSH 키)로 키 인증만 받는 sshd 를 띄운다(비밀번호·키보드 인증 끔, deploy/README).
- 주소: RunPod 콘솔 Pod → Connect → "SSH over exposed TCP" 의 `ssh root@<IP> -p <포트>`. Pod 안에서는 `RUNPOD_PUBLIC_IP`, `RUNPOD_TCP_PORT_22`(pod_check 가 출력. Tier 0 는 `SIMAI_PUBLIC_*` 가 있으면 그것을 먼저 읽는다, 아래 "공개 주소 환경 변수").
- 노트북 `~/.ssh/config` (IP·포트는 매번 바뀔 수 있어 값만 고친다):

```
Host simai-pod
  HostName <공개 IP>
  Port <22 매핑 포트>
  User root
  IdentityFile ~/.ssh/<RunPod 에 등록한 키>
  ServerAliveInterval 30
```

**Pod 재시작으로 포트·IP 가 바뀐 경우** (RunPod 문서: 외부 포트 매핑은 Pod 가 재설정될 때마다 바뀐다, 명세 3.12):

1. 콘솔 Connect 에서 새 `IP:포트`를 확인하고 `~/.ssh/config` 의 HostName·Port 를 고친다(EC2 엔드포인트 등록은 Tier 0 서비스 몫이고 SSH 는 해당 없음).
2. 호스트 키는 컨테이너마다 새로 만든다(이미지에 넣지 않음). 그래서 같은 `IP:포트`가 다시 배정되면 `REMOTE HOST IDENTIFICATION HAS CHANGED` 가 날 수 있다. 콘솔에서 Pod 가 재시작된 것을 확인한 경우에만 `ssh-keygen -R "[<IP>]:<포트>"` 로 지우고 다시 접속해 새 지문을 받는다. 재시작하지 않았는데 지문이 바뀌면 접속하지 않는다.
3. 큐가 돌던 중이었다면: Pod 재시작은 컨테이너 디스크(시험 도구)를 비우지만 볼륨의 큐 상태는 남는다. 같은 `--session` 으로 다시 `run` 하면 끝난 항목은 건너뛰고, `running` 으로 남은 항목은 ABORTED 기록 뒤 다시 돈다(gpu_queue 재시작 복구).
4. Pod 를 Stop 했다가 다시 켜면 같은 H100 을 다시 못 잡을 수 있다(볼륨은 남는다, 명세 3.12). 세션 도중에는 Stop 하지 않는다.

### 443 최후 예비 경로 (EC2 경유·직접 연결이 모두 막힌 망, S15P31A101-286·-392)

- SSAFY 망(시연장 포함)에서는 팀 EC2(`simai.live:22`)가 열려(2026-11-02 실측: 노트북→EC2 약 5 ms, EC2→일본 Pod 약 41 ms) EC2 SSH 경유 + Pod 공개 포트 Direct TCP 를 기본으로 쓴다(`deploy/README.md` "SSAFY 망 기본 경로"). RunPod HTTPS 프록시 `https://<POD_ID>-<포트>.proxy.runpod.net`(443, Cloudflare)는 HTTP·WebSocket 만 통과하며 요청당 약 0.58 s 지연이 추가되고(9차 세션) 연결이 최대 100초로 끊길 수 있어, EC2 경유와 LTE 테더링이 모두 안 될 때의 최후 예비로 둔다(명세 3.12).
- 3차 세션 실측(2026-10-26): SSAFY 노트북 망은 나가는 포트 80·443·8000·8080 만 열려(portquiz.net) Direct TCP SSH(19592·11680 등)가 시간 초과였다.
  Pod 에 HTTP 포트 8080 을 더하고 그 프록시 주소로 WebSocket 을 열어 wstunnel 로 sshd 에 닿았다. SSH·scp 모두 됐다(30 MB 32 s).
- 이제 공통 이미지에 wstunnel 10.7.1(sha256 고정)이 있고, `simai-start` 가 sshd 를 띄운 경우에만 RunPod Secret `SIMAI_TUNNEL_PREFIX` 를 경로 비밀값으로 써서
  `wstunnel server --restrict-to 127.0.0.1:22 --restrict-to localhost:22 ws://0.0.0.0:8080` 을 띄운다. 템플릿 값·노트북 클라이언트·`~/.ssh/config` 는 deploy/README "막힌 망 최후 예비 SSH".
- 경로 비밀값을 토큰처럼 쓰는 이유: `--restrict-to` 만 주면 서버 로그가 `Allow All` 로 떠 제한이 걸리지 않은 것으로 보였고, 공개 주소에 인증 없는 터널을 둘 수 없다.
  비밀값은 wstunnel 에만 환경 변수로 넘기고(명령줄·출력·다른 자식·SSH 세션 환경 파일에 없음), wstunnel 로그는 규칙에 비밀값을 찍으므로 끈다. 비밀값이 없거나 32자 미만·허용 밖 문자면 터널을 띄우지 않는다.
- 3차 세션에서 겪은 것과 이번 처리: `/tmp` 는 실행이 막혀(noexec) 실행 파일을 `/usr/local/bin` 에 둔다. 이미지에 curl 이 없어 빌드 때 넣는다.
  `sleep infinity` Pod 에서 sshd 가 안 뜬 원인은 템플릿 오류(빈 `PUBLIC_KEY`, 이름 칸에 이미지 다이제스트가 든 `SSH_PUBLIC_KEY`)로 유효한 공개키가 없던 것이다(deploy/README 템플릿 "공개키" 줄).
  SSH 세션에 컨테이너 ENV 가 없어 `/proc/1/environ` 을 수동으로 source 했던 절차는 `simai-start` 가 적는 `/etc/profile.d/simai-env.sh`(비밀 아닌 이름만)로 바꿨다.
- 막힌 망에서 세션 큐는 이 경로의 SSH 로 `nohup` 으로 띄우고(연결이 끊겨도 돈다), 결과 묶음은 이 경로의 scp 로 가져온다. 이전의 웹 터미널 + 다른 망 scp 절차는 쓰지 않는다. 묶음을 HTTP 로 공개 서빙하지 않는다(토큰 없는 공개 주소가 된다).
- I0 도 같은 터널로 보낼 수 있다(S15P31A101-288): 템플릿 `SIMAI_TUNNEL_ALLOW=17000,17001` 이면 `simai-start` 가 `--restrict-to 127.0.0.1:17000·17001` 을 더하고(22 는 늘, 틀린 목록이면 22 만),
  Tier 0 는 `SIMAI_PUBLIC_HOST=127.0.0.1`·`SIMAI_PUBLIC_PORT_70000/70001=17000/17001`·듣기 주소 127.0.0.1 로 띄운다. 노트북 wstunnel 이 같은 번호 로컬 포트를 전달한다(명령은 deploy/README "I0 터널").
  노트북은 Tier 0 를 공개키 지문으로만 확인하므로(호스트 이름 검사 없음) 주소가 127.0.0.1 로 바뀌어도 TLS 검증을 바꿀 것이 없다.
- wstunnel 클라이언트는 기본으로 바깥 TLS(Cloudflare) 인증서를 검증하지 않는다. 안쪽 SSH·I0 는 따로 서버를 확인하지만 경로 비밀값은 바깥 TLS 만 지키므로, 클라이언트 명령에 늘 `--tls-verify-certificate` 를 붙인다(deploy/README).
  SSAFY 망에서 검증을 켜도 붙는 것을 확인했다(2026-10-26: 핸드셰이크 통과 → HTTP 404, TLS 가로채기 없음). 시연장 망에서는 다시 확인하고, 실패하면 검증을 끄지 않고 보고한다.
- 남은 확인: 10분 이상 연속 연결(100초 끊김과 keepalive·WebSocket ping), 큰 결과 묶음(수 GB) scp 처리량, I0 영상 지연(아래 "I0 443 예비 경로 측정").

### I0 443 예비 경로 측정 (S15P31A101-288, 4차 세션 ④)

명세 3.12 시연장 망 위험·19.1 미결 "I0 프록시 지연". 같은 입력으로 (a) 443 프록시 + wstunnel, (b) LTE 테더링 Direct TCP 를 각 10분 잰다. 도구는 `deploy/scripts/i0_link_measure.py`(시험 `ai/tests/deploy/test_i0_link_measure.py`).

| 결정 | 값 | 근거 |
|---|---|---|
| 입력 | 녹화 재생(`rec-20261021-031045z-classroom-dev-test`, 교실 4대 2304x1296 25 fps 180 s, relay `kind = "file"`·`loop`·`fps = 25`) | 두 경로에 같은 영상이 들어간다. 테더링 때 카메라 공유기 LAN 과 휴대폰 망을 같이 잡는 이중 망 구성이 필요 없고, C110 RTSP 2세션 제한·교실 장면 변화와 무관하다. 실카메라 relay 는 (a) 만 덧붙여 볼 수 있다 |
| 포트 | 제어 17000, 영상 17001(4대 공용) | 대칭 매핑 요청 번호 70000·70001 의 실제 값. 영상 포트를 같이 써도 연결은 카메라마다 따로라(TCP 연결마다 WebSocket 하나) 독립성은 같고 허용 대상이 줄어든다. Windows 동적 포트 예약(49152~) 밖 |
| 노트북 대상 | Pod 가 쓴 `endpoint.json`(주소·포트·지문) + 사람이 옮긴 `--pin` | 측정용 Tier 0 는 EC2 없이 돌아 등록하지 않는다(EC2 `GET /api/endpoints/tier0` 는 S15P31A101-290 으로 생겼다). 신뢰 규칙은 제품과 같다(고정값에 없는 지문은 붙지 않음, 실제 서버 키가 다르면 토큰 전에 끊음) |
| 노트북 제어 채널 | 현장 경보 단말 코드(SiteAlertApp·WebSocketControlChannel·SiteLinkMonitor) | 시계 교환(`clock_sync_request`)은 단말이 받은 즉시 답한다(S15P31A101-289). 처음(-288)에는 단말이 답하지 않아 도구가 대신 답했다(답이 없으면 T_lag 가 null 이고 frame_age 에 두 컴퓨터 시계 차이가 섞인다) |
| Pod Tier 0 | 시뮬레이터 교실 보정(cam1~cam4), 그 자리 자체 서명 키(임시 폴더, 끝나면 지움), EC2 없음 | 지연·끊김만 본다. 위치·판정은 의미가 없고 결과는 제품 성능 지표가 아니다(AGENTS 규칙 7) |
| 지표 | frame_age_ms_p95(표시 파일 5초 창), 보정 T_lag(표시 파일 `gpu_priority.load.lag_ms` 와 노트북이 받은 health `pipeline_lag_ms`), 노트북 송신 Mbps·fps, Pod 수신 fps·`gateway_to_runpod_loss`, 영상·제어 재연결 수와 시각, 90~110 s 로 끝난 연결 구간 수(100초 끊김), 최장 연결 | 표시 파일은 5초마다, 노트북 relay 는 1초마다 기록. 측정 시간이 끝나며 닫는 끊김은 세지 않는다 |

노트북 로컬 대역 왕복(2026-10-26): `pod --link tunnel --tier0-cmd <대역 Tier 0>`(실제 Tier0Service + 검출 대역) ↔ `laptop`(합성 카메라 4대) 30 s 에서 공개키 고정·기기 토큰·제어 채널·시계 교환·영상 4대가 붙고
T_lag p50 78·p95 122 ms(같은 컴퓨터), health lag null 0/27, 끊김 0 이었다. 시계 교환 응답을 넣기 전에는 health lag 가 모두 null 이었다. wstunnel·RunPod 프록시 구간은 노트북에서 재지 않았다.

버린 대안: SSH 포트 전달(`ssh -L`, 이미지 변경 없음)은 카메라·제어가 TCP 하나에 묶여 한 카메라의 지연·끊김이 번지므로 이미지가 늦을 때의 예비로만 둔다. 노트북 HTTP 조회 대역(loopback http)은 relay 는 받지만
현장 경보 단말의 EC2 하트비트가 https 만 받아 제어 채널을 띄울 수 없고, 인증서 검증을 끄는 우회는 하지 않는다.

남은 위험: RunPod 프록시의 연결 시간 상한이 영상처럼 계속 흐르는 WebSocket 에도 걸리는지(걸리면 relay·제어가 재연결로 버티는지 측정이 답한다), 테더링 망의 CGNAT·지터,
시연장 망의 TLS 가로채기(SSAFY 망은 2026-10-26 검증 켠 채 접속 확인, 시연장은 다시 확인), EC2 엔드포인트 조회 미구현. 현장 경보 단말의 시계 교환은 S15P31A101-289 에서 넣었다.

## GPU 1차 세션 큐 (`deploy/scripts/gpu_session1.py`)

`ai/experiments/gpu_queue`(S15P31A101-36)를 같은 프로세스에서 그대로 돌린다. 항목별 명령·제한 시간·Run 기록(`contracts/experiments/run_record.schema.json`)·잠금·heartbeat·재시작 복구는 gpu_queue 가 맡고,
세션 실행기는 Job 파일 생성, 사전 점검, 전체 시간 상한, 결과 묶음, scp 명령을 더한다.

| 항목 | Jira | 내용 | H100 예상 (2차 세션 실측) | 제한 | 상태 |
|---|---|---|---|---|---|
| a | -31 | Pod 점검(위 표) | 1분 (0.2초, 볼륨 없음) | 15분 | 큐 |
| b | -32 | Tier 0 이미지 GPU·torch CUDA·TensorRT 확인, 엔진 첫 빌드·캐시 재사용·batch 4 추론, 진입점(설정이 있으면), 가중치 반례(종료 코드 2), sm_86 경로 분리 (`session1_engine.sh`, deploy/README "Pod 검증 큐" 1~7) | 12분 (561.2초, 엔진 빌드 550.6초) | 45분 | 큐 |
| t | -183 | 시험 도구(pytest·cryptography, `ai/uv.lock` 버전)를 컨테이너 디스크에 `uv pip install --target` (Tier 0 이미지에는 시험 의존성이 없다) | 1분 (0.8초) | 10분 | 큐(Pod 만) |
| c | -182 | LIVE 입력 Pod 확인 (`session1_live.py all`, 아래) | 5분 (207.2초) | 40분 | 큐 |
| d | -60 | Tier 0 GPU 우선 보호 측정 P0~P7 (`session1_gpu_priority.py`, 아래) | 10분 (446.8초) | 45분 | 큐 |
| e | -92 | Network Volume 강제 종료 복구 (아래) | 5분 | 30분 | 제외: 엘리스 블록 스토리지 세션으로 옮김(-205) |
| f | -45·-175 | `test_i1_tls.py`·`test_serial_tls.py` 를 Linux 에서 5회 반복 (`repeat_pytest.py`) | 10분 | 40분 | 제외: GPU 가 필요 없어 GPU 세션에서 뺌(Linux CPU 에서) |
| g0 | -207 | EXP-02 이미지 GPU 점검(`exp_image_check.py`: CUDA·세대 커널·Detectron2 LazyConfig(-238 부터 CUDA 확장 없음)·pyrender EGL) | 1분 (6.6초) | 10분 | exp02 이미지 Pod 에서 |
| g1~g3 | -207 | EXP-02 E-GATE-0 재현: H6 검출 시간, H0 스모크 1장, H1 공식 경로 14장 (아래 "2차 세션") | 1·1·2분 (10.0·35.9·58.3초) | 20·20·45분 | exp02 이미지 Pod 에서 |
| g4 | -238·-243 | EXP-02 보정 K 입력 경로: 교실 사진 20장 + 교실 보정 기록, `check_calib_k.py` 판정 (`docs/design/exp-images.md` "3차 세션 항목") | 3분 (추정, 아래) | 30분 | exp02 이미지 Pod 에서 |
| m0 | -207 | MapAnything 이미지 GPU 점검 | 1분 (5.0초) | 10분 | mapanything 이미지 Pod 에서 |
| m1·m2 | -207 | EXP-01 E-GATE-0 재현: C1(map-anything)·C2(map-anything-apache), room4 4장 | 2·2분 (47.1·37.5초) | 20·20분 | mapanything 이미지 Pod 에서 |
| v4 | -278 | 통합 시험 재확인(3차 v-int 재현, `vlm_int.py`, 아래 "4차 세션") | 15분 (3차 v-int 13.4분) | 60분 | unified 이미지 Pod 에서 |
| (`--exp-jobs`) | -87·-291 | 추가 EXP Job 파일(다시점 집계, 4차 세션 `session4_jobs.py` 등). LIVE 보호를 켠 별도 큐로 마지막에 | - | - | 사람이 줄 때만 |
| (`--only`) | -291 | 이 키의 항목만 큐에 넣는다(예 `a,t,v4`). 이 이미지에서 돌지 않는 키·모르는 키는 종료 4(빈 큐로 성공하지 않음) | - | - | 사람이 줄 때만 |

- 순서: Pod 점검 → Tier 0 이미지·엔진(Tier 0 가 먼저, c·d 가 이 엔진 캐시를 쓴다) → 시험 도구 → LIVE·자원 보호 → 복구·TLS → EXP 는 맨 끝. 한 항목이 실패해도 다음 항목으로 간다(`continue_on_failure`).
- 항목마다 돌 이미지가 정해져 있다(S15P31A101-207): Pod 하나는 이미지 하나라 큐는 지금 이미지(`--image`, 기본 이미지 ENV `SIMAI_IMAGE_KIND`, 없으면 tier0)의 항목과 a(모든 이미지)만 넣고, 나머지는 표에 '다른 이미지' 로 남긴다. 모르는 이미지 이름이면 시작하지 않는다(종료 코드 4).
- c·d 는 -182·-60 이 머지된 뒤의 경로(`simai_ai.io.PyAvDecoder`·`NvdecDecoder`, `tests/services/tier0/live_video.py`, `experiments/gpu_queue/tier0_priority_probe.py` 등)를 부른다. 머지 전이면 각 단계가 '없음(머지 필요)'으로 실패하고 다음 항목으로 간다. PyAV 는 -182 가 `io` extra 에 넣었으므로 그 뒤에 빌드한 이미지여야 c 가 돈다.
- **예상 시간 근거 (S15P31A101-243).** 2차 세션 실측: 2026-10-22, RunPod AP-JP-1, NVIDIA H100 80GB HBM3(드라이버 595.91.07, CUDA 13.2), 볼륨 없음(`/srv/simai`).
  이미지 Tier 0 `jonghwa1492/tier0:3926a8772649`(`sha256:7fe58115…`), HMR `jonghwa1492/tier0:exp02-fs3db-3926a8772649`(`sha256:da77b601…`, -238 전 CUDA 확장 이미지),
  MapAnything `jonghwa1492/tier0:mapanything-3926a8772649`(`sha256:f171420c…`). 결과 묶음은 노트북 `ai/experiments/runs/gpu-session1/20261022-s2-{tier0,exp02,mapanything}/`(커밋 안 함).
  표의 예상은 항목 Run 기록(started_at~finished_at) 실측에 약 1.3~2배를 더해 분 단위로 올렸다(`gpu_session1.py` 의 `expected_s` 와 같은 값). 제한(`timeout_s`)은 처음 값 그대로다(다른 GPU·첫 받기 여유).
  - b: 엔진 빌드 550.6초(YOLO26x-pose e2e FP16, imgsz 1280, batch 4), 캐시 재사용 3.0초, batch 4 추론 4.2초. 1차 세션 문서의 "수 분" 을 실측으로 바꿨다.
  - c: c1 1.1초, c2 2.9초(통과 2), c3 40.6초, c4 131.8초, c5a 30.5초. c3 은 -212 판(NVDEC 한 스레드 p95 9.665 ms·112.2 fps, CPU 한 스레드 492.3 fps, CPU 카메라별 스레드 5초 변형 p95 3.05 ms·1,575.7 fps), c4 frame_age p95 최대 79 ms·pipeline_lag p50 117·p95 143 ms(NVDEC), c5a 10명 p95 57.38 ms(FAIL, 호스트 CPU, `docs/design/tier0-geometry-budget.md`).
  - d: P0 23.1초(시험 3개가 Pod 의 RUNPOD_DC_ID·큐 표식을 물려받아 실패 → -243 에서 시험 환경 분리), P1 즉시(예약 8,194 MB), P2·P3·P5·P6 120.2·135.3·90.2·60.1초, watch lag p95 170·168·166·168 ms 모두 passed.
  - g·m: g0 6.6초, g1 10.0초(H6 ms_per_frame p50 8.11 ms), g2 35.9초, g3 58.3초(H1 12장 중앙값 232.1 ms, `docs/design/exp-images.md` "2차 세션 HMR HOLD 원인과 새 기준"), m0 5.0초, m1 47.1초(적재 33.6초, warm 추론 795.7 ms), m2 37.5초(적재 23.3초, warm 457.4 ms).
  - g4 는 아직 돌리지 않았다. g3(14장, 적재 포함 58.3초)에 그림 수(20장)·다인원(노트북 원본 93명)·펴기(CPU, 그림당 수십 ms)를 더해 3분으로 잡았다(3차 세션 실측으로 바꾼다).
  - e·f 는 GPU 세션에서 빠져 1차 때 값(노트북 실측 e 약 40초, f 1회 약 35초에 여유)을 그대로 둔다.
- **H100 총 예상**: Tier 0 Pod 큐 약 29분(실측 1,216.6초 ≈ 20분) + Pod 기동·이미지 받기(압축 약 7.9GB) 약 10분 + 가중치 받기(볼륨 없음, 수십 초) + 사본 올리기·결과 회수 약 5분 ≈ **45분**. d 를 G0 판정용(`--full`, watch 600·600·180·120초)으로 돌리면 +약 19분(watch 합 1,500초 − 390초). 전체 시간 상한 기본값은 9000초(2.5시간). 다른 GPU 는 엔진 빌드·추론 시간이 다르다(H100 실측만 적었다).
- 볼륨 없는 Pod(-205): 저장 위치는 `simai-bootstrap` 과 같은 규칙(`SIMAI_ROOT` → `/workspace` 가 마운트 지점이고 쓸 수 있을 때만 `/workspace/simai` → `/srv/simai`, S15P31A101-311)이고, 마운트 지점이 아니면 a 에 `--no-volume`(볼륨 측정 SKIP)을 붙이고 Job 파일에 볼륨·데이터센터를 적지 않는다(`provider`·`datacenter` 는 run_record 가 `RUNPOD_POD_ID`·`RUNPOD_DC_ID` 등 사이트 신호로 채움, 신호가 없으면 LOCAL). 결과 묶음은 컨테이너 디스크에 있으므로 **Pod 를 지우기(Terminate) 전에** scp 로 가져온다(실행기가 출력).
- 결과 기록은 run_record 계약을 따른다. `experiment_id` 는 운영 경로 검증 Run 값 `VAL-01`(run_record 2.1.0, `e_gate` 없음, S15P31A101-197), `experiment_state` 는 `BENCHMARK`, 입력 출처는 `SIM`·`is_upper_bound: true` 다(제품 성능 지표로 쓰지 않는다). 종료 코드는 `notes`, 지표는 각 스크립트가 `metrics.json` 에 남긴다.

### 실행 순서

```sh
# 0) 노트북: 큐 계획과 드라이런 (GPU 없음)
uv run --project ai python deploy/scripts/gpu_session1.py plan --mode pod
uv run --project ai python deploy/scripts/gpu_session1.py run --mode dryrun --root <짧은 로컬 폴더>
# 1) 노트북: 커밋된 HEAD 의 저장소 사본과 git 정보 → Pod 로 올리는 명령이 출력된다 (볼륨 없는 Pod 는 --pod-simai /srv/simai)
#    Git Bash 는 /srv/simai → C:/Program Files/Git/srv/simai, /tmp/simai → %TEMP%/simai 처럼 바꿔 넘긴다. prepare 는 Git Bash 에서 드라이브 경로를 거절하므로 MSYS_NO_PATHCONV=1 을 붙인다
MSYS_NO_PATHCONV=1 uv run --project ai python deploy/scripts/gpu_session1.py prepare --out <폴더> --pod-simai /srv/simai
# 2) Pod 켜기: Tier 0 템플릿(deploy/README, 볼륨 없음), 시작 명령 sleep infinity. 1) 이 출력한 scp·ssh 명령으로 사본을 풀고
#    simai-bootstrap queue 로 띄운다(GPU 세대 감지·가중치 받기·해시 확인 → 큐)
ssh simai-pod 'nohup simai-bootstrap queue > /srv/simai/setup/session1/session1.log 2>&1 &'
ssh simai-pod 'tail -f /srv/simai/setup/session1/session1.log'      # 끊겨도 큐는 계속 돈다
# 3) 끝나면 로그 끝에 나온 scp 명령으로 결과 묶음을 가져온 뒤(볼륨 없음: 지우기 전에) RunPod 콘솔에서 Pod Stop·Terminate
```

- `python` 은 이미지 venv(`/opt/simai/ai/.venv`, jsonschema 포함)다. 저장소 사본의 `ai/src` 를 `PYTHONPATH` 앞에 두어 사본 코드로 돈다(b 의 엔진 준비는 이미지에 설치된 모듈).
  Job 환경의 `SIMAI_CONTRACTS_DIR` 는 사본의 `contracts/` 로 덮는다(이미지 값 `/opt/simai/contracts` 는 이미지를 만들 때의 옛 계약). 사본에는 `.git` 이 없으므로 gpu_queue 는 Job 파일에 git 값이 없으면 사본 최상위의 `git-stamp.json` 을 읽는다(둘 다 없으면 지금처럼 시작하지 않음, S15P31A101-213).
- 같은 `--session` 으로 다시 돌리면 끝난 항목은 건너뛴다(중단·Pod 재시작 뒤 이어 돌기). 처음부터 다시 돌리려면 다른 세션 이름을 쓴다.
- 결과 묶음: `/workspace/simai/runs/gpu-session1/<세션>/session1-<세션>-<UTC>.tar.gz` = `session.json`(항목별 상태·종료 코드·자리만 항목·시간), `bundle-manifest.json`(파일 sha256, 뺀 파일과 이유), 큐 상태·로그·Run 폴더 전체, `jobs.json`. 볼륨에 못 쓰면 `/tmp/simai-session1/` 로 물러서고 그 경로를 출력한다.
- 노트북에서는 `ai/experiments/runs/gpu-session1/<세션>/` 에 풀어 둔다(`.gitignore` 의 `runs/`, 커밋하지 않음). Run 요약은 MR·Jira 로 남긴다.

### 2차 세션 (S15P31A101-207)

1차 세션과 같은 실행기를 이미지 셋으로 나눠 돈다. 세션 이름은 1차와 다르게 준다(같은 이름이면 끝난 항목을 건너뛴다).

| 순서 | Pod 이미지 | 큐 항목 | 확인 |
|---|---|---|---|
| 1 | `tier0`(-207 재빌드: 층 분할 + modelopt·onnxruntime-gpu·cupy 잠금) | a, b, t, c, d | b 엔진 빌드(modelopt 경로), **c 의 c3 재측정(-212 의 디코더별 색 변환 문맥 + 비교 변형)은 `session1_live.py all` 안에 있어 c 항목으로 함께 돈다** |
| 2 | `exp02-fs3db` | a, g0~g3 (3차 세션부터 g4) | g0 통과 뒤 g1~g3 를 E-GATE-0 H6·H0·H1 과 비교. g4 는 `check_calib_k.py` 판정(`docs/design/exp-images.md` "3차 세션 항목") |
| 3 | `mapanything` | a, m0~m2 | m0 통과 뒤 m1·m2 를 E-GATE-0 C1·C2 와 비교(`ai/experiments/static_scene/compare_runs.py`) |

- 실험 이미지 큐는 LIVE 보호를 켠다(`live_guard.enabled=true`, `on_live=stop`, 저장 위치의 `state/tier0.live` 가 30초 안에 갱신됐거나 `services.tier0` 프로세스가 있으면 멈춤).
  볼륨 없는 Pod 끼리는 표시 파일을 나누지 않으므로, 관제 시간에 실험 Pod 를 켜지 않는 것은 사람이 지킨다(AGENTS.md RunPod 규칙).
- Run 기록: g1~g3 는 `experiment_id` EXP-02, m1·m2 는 EXP-01, `experiment_state` BENCHMARK, `e_gate` 없음(새 이미지 환경 확인이지 채택 근거가 아니다), 입력 출처 FILE(`hmr_poc14`·`room4`, `is_upper_bound: false`).
  Run id 는 `<E-GATE-0 이름>_img_<세션>` 이라 E-GATE-0·다른 세션 결과를 덮어쓰지 않고, `exp_job.py` 가 끝난 결과 폴더를 다시 쓰지 않는다. 가중치·입력·결과는 저장 위치 절대 경로로 기록되고(weights_hash·dataset_hash·results), 실험 지표는 Run 기록 metrics 로 넘어간다. 시작 전에 Run 기록 검증기(jsonschema)를 컨테이너 디스크에 깔고 없으면 시작하지 않는다. `env.lock_files` 는 이미지의 잠금(`deploy/exp02/requirements.lock.txt` 등)이고 `SIMAI_IMAGE_DIGEST` 가 있으면 다이제스트가 `env_hash` 다.
- 결과 묶음에 각 Run 폴더(`runs/exp02/<run_id>`·`runs/exp01/<run_id>`: metrics.json·메시·패널, 50MB 넘는 파일은 빠짐)도 넣는다.
- 데이터(`datasets/hmr_poc14` 14장, `datasets/room4` 4장)는 저장소에 없어 Pod 에 scp 로 올린다(deploy/README "Pod 검증 큐"). 없으면 g1~g3·m1·m2 가 실패하고 다음 항목으로 간다.
  g4 입력(교실 사진 20장 + 교실 보정 기록)은 사람이 찍혀 있어 `prepare` 가 저장소 밖 `--out` 에 `g4-inputs.tar` 로 묶고 그 출력대로 올린다. 없으면 g4 Run 기록의 calibration 이 비고 판정이 실패한다.
- H100 예상(2차 세션 실측 기준, 위 "예상 시간 근거"): exp02 Pod 큐 약 9분(g4 포함. 2차 실측 g0~g3 115.5초) + 이미지 받기·가중치(약 4.3GB) 받기, mapanything Pod 큐 약 6분(실측 100.1초) + 가중치(약 9.8GB) 받기. `sam-3d-body-dinov3-mhr` 해시를 채우기 전에는 exp02 Pod 의 `simai-bootstrap queue` 가 받기 전에 종료 코드 2 로 멈춘다(docs/data/weights.md).

### 4차 세션 (S15P31A101-291)

통합 이미지 Pod 하나(H100 80GB)에서 **LIVE 루프 연결 시험(별도 티켓) → I0 443 지연(-288) → 큐(-278 재확인·-282·-283)** 순서로 돈다(Lead 결정).
LIVE 구간(Tier 0 가 영상을 받는 시험)이 끝난 뒤에만 큐를 띄운다. 세션 실행기는 Tier 0 표시 파일(30 s 안 갱신)·`services.tier0` 프로세스가 있으면 시작하지 않고(종료 3),
EXP 큐는 LIVE 보호(on_live=stop)로 멈춘다. 이미지 `jonghwa1492/tier0:unified-37c7ae51773d`(dev 37c7ae5 빌드: -278 감시 프로세스 기본값·-286 wstunnel·-288 터널 포트 포함)는 다시 빌드하지 않는다.

| 항목 | 무엇 | 어디 | 판정·기록 |
|---|---|---|---|
| I0 | -288 443 프록시 + wstunnel / LTE 테더링 Direct TCP 각 10분(`i0_link_measure.py`, 위 "I0 443 예비 경로 측정") | LIVE 구간(측정 도구가 Tier 0 LIVE 를 띄우고 노트북이 녹화 재생 영상을 보낸다) | `summarize` 표. 기록만 |
| a·t·v4 | Pod 점검·시험 도구·-278 재확인(`deploy/scripts/vlm_int.py`): 3차 v-int 단계 A~D 를 감시 프로세스가 만드는 vLLM 인자·환경·CPU 묶기 그대로 | 검증 큐(`--only a,t,v4`). v4 는 자기 Tier 0 하네스를 띄우므로 EXP 큐가 아니다 | 기본값 그대로, /health 시한 안, 세 요청 단계 오류 0·p95 ≤ 5 s, A~C 카메라 4대 ready·reserve OK(표본 없음은 실패), vLLM 이 할당량 기준 CPU(이번 Pod 예상 12개)에 묶임, kill -9 뒤 그룹 정리로 그룹이 빔. 환경이 기본값을 덮었거나 vlm 가중치가 없으면 GPU 를 쓰기 전에 종료 2 |
| r518~rfull | -282 MapAnything 해상도 4 Run(`session3_jobs.build`, `simai_bootstrap.py scene` 으로 감싸 static 가중치를 받고 Tier 0 우선 감시) | EXP 큐(`ai/experiments/static_scene/session4_jobs.py`) | 노트북 `world_frame.cli fix` → `compare_resolutions.py` |
| h0~h6 | -283 DA3 가상환경(`/root/da3`)·가중치(`ai/experiments/static_scene/da3_weights.json`: 리비전 b2359bd·sha256 고정, simai-bootstrap 받기)·predict 504·1008·1512·2304·자세 없이 504 | EXP 큐 | h2~h7 은 시작 때 가중치 확인을 다시 거쳐(검증 스탬프) 해시가 다르면 predict 전에 종료 2. 깊이 폴더 `depth.json` 이 지표(`exp_job --metrics depth.json`, OOM 도 기록) |
| h7 | -283 ray 헤드 자세, h2~h5 를 보고 사람이 고른 해상도 | 큐가 끝난 뒤 Job 파일 하나 더(아래 7) | 같은 기준 |

h8(MoGe-2)은 넣지 않는다(-287 결정). 생성기는 `--only h8` 을 거절한다.

**0) 노트북 준비 (GPU 꺼짐).** 입력·결과 묶음은 저장소 밖 임시 폴더(`<T>`)에 둔다(사람이 찍힌 사진, 커밋 안 함).

```sh
MSYS_NO_PATHCONV=1 uv run --project ai python deploy/scripts/gpu_session1.py prepare --out <T> --pod-simai /srv/simai   # 사본·git-stamp (g4 묶음 줄은 무시)
uv run --project ai python deploy/scripts/gpu_session1.py plan --mode pod --root /srv --image unified --only a,t,v4     # v4 가 '큐' 인지
MSYS_NO_PATHCONV=1 python ai/experiments/static_scene/session4_jobs.py --repo /srv/simai/setup/session1/repo --session <YYYYMMDD>-s4 -o <T>/exp-jobs-s4.json   # Git Bash 경로 변환이 남으면 거절
(cd ai && uv run python experiments/gpu_queue/gpu_queue.py check <T>/exp-jobs-s4.json)                                 # Job 11개, Run 기록 형식 통과
# 입력: w1 MapAnything Run(3차, cameras.json·raw.npz·metrics.json 만)과 입력 폴더. 노트북 위치는 아래 경로 그대로(메인 checkout, 커밋 안 함)
tar -cf <T>/s4-prepared.tar -C ai/experiments/recordings/mapanything-inputs classroom4-prepared-20261026
W1=CAL102_w1_map_anything_known_k_20261026-s3
tar -cf <T>/s4-w1.tar -C ai/experiments/runs/gpu-session1/20261026-s3/runs/exp01 $W1/cameras.json $W1/raw.npz $W1/metrics.json
python deploy/scripts/i0_link_measure.py token-hash     # SIMAI_DEVICE_TOKEN(.env)의 sha256 → Pod 의 SIMAI_LAPTOP_TOKEN_SHA256
```

- I0 relay 설정(`<T>/i0-relay.toml`, 저장소 밖): 녹화 `ai/experiments/recordings/rec-20261021-031045z-classroom-dev-test/cam_{A,B,C,D}/epoch_0000.h264` 를 Pod 시뮬레이터 카메라 `cam1`~`cam4` 로
  `[[cameras]]` `camera_id = "cam1"`, `kind = "file"`, `file = "<그 경로>"`, `loop = true`, `fps = 25` 네 벌. wstunnel 클라이언트·비밀값 파일은 deploy/README "막힌 망 최후 예비 SSH".

**1) Pod 켜기 (약 10~15분).** RunPod 템플릿(deploy/README "RunPod 템플릿", "통합 이미지"의 Pod형): 이미지 `jonghwa1492/tier0:unified-37c7ae51773d`(다이제스트로 고정하면 `SIMAI_IMAGE_DIGEST` 에 같은 값),
H100 80GB·CUDA 13.0 이상, Container Disk 150GB 이상, 볼륨 없음, 공개 HTTP 8080, 공개 TCP 22·70000·70001(LTE 비교의 Direct TCP), 시작 명령 `sleep infinity`(감시 프로세스·Tier 0 를 띄우지 않는다).
환경 변수: `HF_TOKEN={{ RUNPOD_SECRET_hf_token }}`, `SIMAI_TUNNEL_PREFIX={{ RUNPOD_SECRET_simai_tunnel_prefix }}`, `SIMAI_TUNNEL_ALLOW=17000,17001`, `SIMAI_TIER0_GPU_RESERVE_MB=8192`, `PUBLIC_KEY=<계정 공개키 값>`.
`SIMAI_VLLM_EXTRA_ARGS`·`SIMAI_WORKER_VLLM_MAX_MODEL_LEN`·`VLLM_USE_FLASHINFER_SAMPLER`·`SIMAI_SIDECAR_CPUS` 는 넣지 않는다(v4 는 기본값을 잰다. 넣으면 v4 가 종료 2).

**2) 접속·올리기 (약 5분).** SSAFY 망은 443 경로: `WSTUNNEL_HTTP_UPGRADE_PATH_PREFIX` 를 그 셸에만 두고 `ssh simai-pod-443`(deploy/README). scp 는 같은 Host 로 약 1 MB/s(3차 30 MB 32 s).

```sh
ssh simai-pod-443 'mkdir -p /srv/simai/setup/session1 /srv/simai/datasets /srv/simai/runs/exp01'
scp <T>/simai-repo.tar <T>/git-stamp.json <T>/exp-jobs-s4.json <T>/s4-prepared.tar <T>/s4-w1.tar simai-pod-443:/srv/simai/setup/session1/
ssh simai-pod-443 'cd /srv/simai/setup/session1 && rm -rf repo && mkdir repo && tar --no-same-owner -xf simai-repo.tar -C repo && cp git-stamp.json repo/ && tar --no-same-owner -xf s4-prepared.tar -C /srv/simai/datasets && tar --no-same-owner -xf s4-w1.tar -C /srv/simai/runs/exp01'
```

**3) 미리 받기·시험 도구 (GPU 안 씀, 약 5~10분, LIVE 측정이 시작되기 전에 끝낸다).** v4 의 Cosmos3-Nano(31.5GB, 검증 스탬프)와 I0 측정 도구가 import 하는 시험 도구(pytest·cryptography, `ai/uv.lock` 판).
I0 측정(`i0_link_measure.py pod`)은 `tests/services/tier0` 의 TLS 도우미(`test_i1_tls`, 모듈 첫 줄에서 pytest import)를 쓰는데 이미지에 pytest 가 없어, 큐의 t 항목보다 먼저 여기서 깐다.
Pod 로그인 셸(`ssh simai-pod-443`)에서:

```sh
HF_TOKEN="$(tr '\0' '\n' </proc/1/environ | sed -n 's/^HF_TOKEN=//p')" SIMAI_WEIGHTS_SET=vlm simai-bootstrap check > /srv/simai/setup/session1/vlm-check.log 2>&1
uv pip install --python /opt/simai/ai/.venv/bin/python --target /tmp/simai-session1-testtools pytest==9.1.1 cryptography==50.0.2
```

**4) LIVE 루프 연결 시험 슬롯 (별도 티켓).** 그 티켓의 실행서대로. 끝나면 그 Tier 0 를 내린다.

**5) I0 443 지연 (-288, 약 30분: 경로마다 기동 1~2분 + 10분 + 옮기기).** Pod 로그인 셸에서((b) 는 `--link direct --work …/b-lte`):

```sh
mkdir -p /srv/simai/runs/i0-link && cd /srv/simai/setup/session1/repo
export PYTHONPATH=$PWD/ai/src:$PWD/ai:/tmp/simai-session1-testtools
SIMAI_LAPTOP_TOKEN_SHA256=sha256:<hex 64> SIMAI_WEIGHTS_SET=tier0 nohup simai-bootstrap run /opt/simai/ai/.venv/bin/python \
  deploy/scripts/i0_link_measure.py pod --link tunnel --seconds 600 --work /srv/simai/runs/i0-link/a-tunnel > /srv/simai/runs/i0-link/a-tunnel.log 2>&1 &
cat /srv/simai/runs/i0-link/a-tunnel/endpoint.json      # tls_public_key_sha256 를 노트북 --pin 으로 옮긴다
```

```powershell
# 노트북 (a) SSAFY 망: wstunnel 로 17000·17001 전달(deploy/README "I0 터널") 뒤
scp simai-pod-443:/srv/simai/runs/i0-link/a-tunnel/endpoint.json <T>/a-endpoint.json
uv run --project gateway/relay python deploy/scripts/i0_link_measure.py laptop --endpoint <T>/a-endpoint.json --pin sha256:<hex 64> `
  --config <T>/i0-relay.toml --seconds 600 --out <T>/a-laptop.jsonl
# (b) 휴대폰 LTE 테더링으로 망을 바꾸고(wstunnel 은 끈다) Pod 를 --link direct --work /srv/simai/runs/i0-link/b-lte 로 다시 띄운 뒤 같은 명령(b-endpoint, b-laptop.jsonl)
# 회수(8) 뒤
python deploy/scripts/i0_link_measure.py summarize --run tunnel=<a-tunnel/pod.jsonl>,<T>/a-laptop.jsonl --run lte=<b-lte/pod.jsonl>,<T>/b-laptop.jsonl --out <T>/i0-summary.json
```

측정 도구의 시계 교환 대리 응답은 -289 머지로 지워졌다(현장 경보 단말이 답한다, dev b74288b 의 `cmd_laptop` 에서 확인).

**6) 큐 (LIVE 종료 확인 뒤, 약 50~60분).** `pgrep -af services.tier0` 가 비어 있고 표시 파일이 30 s 넘게 갱신되지 않았는지 본 뒤:

```sh
ssh simai-pod-443 'nohup simai-bootstrap queue --session <YYYYMMDD>-s4 --only a,t,v4 --exp-jobs /srv/simai/setup/session1/exp-jobs-s4.json > /srv/simai/setup/session1/session4.log 2>&1 &'
ssh simai-pod-443 'tail -f /srv/simai/setup/session1/session4.log'       # 끊겨도 큐는 돈다
```

| 순서 | 항목 | 예상 | 근거 |
|---|---|---|---|
| 1 | a·t | 1분 | 2차 실측 0.2·0.8초 |
| 2 | v4 | 15분 | 3차 v-int 13.4분(A 90 s, vLLM 70 s, 요청, 지속 600 s, kill -9) + Tier 0 하네스 기동(LIVE 시험이 만든 엔진 캐시) |
| 3 | r518·r1036·r1554·rfull | 3+1+2+3분 | 첫 Run 이 static 가중치 9.8GB 를 받는다(u3 실측 약 1.7분). 3차 w1 29초, rfull 은 토큰 19배라 추정 |
| 4 | h0 | 5~10분 | torch cu130 휠 받기(설계) |
| 5 | h1 | 2~5분 | 6.76GB 받기 + 해시 |
| 6 | h2~h6 | 2+2+3+5+2분 | 설계 추정(적재 30~60 s + 추론 2회) |
| 합 | | 약 50~60분 | Job 파일 `expected_s` 는 넉넉한 값(검증 큐 22분 + EXP 62분, 시한 계산용) |

**7) h7 (선택, 약 10분).** h2~h5 중 고르는 기준은 설계(`exp01-hybrid-depth.md` "채택 기준": 카메라 간 |Δd|/d 중앙값·왕복 재투영·바닥 rms·충돌 비율). Pod CPU 로 빠르게 본다(고르는 데만 쓰고 정식 fuse 는 노트북). Pod 로그인 셸에서:

```sh
cd /srv/simai/setup/session1/repo && export PYTHONPATH=$PWD/ai/src:$PWD/ai S=<YYYYMMDD>-s4
for r in 504 1008 1512 2304; do /opt/simai/ai/.venv/bin/python ai/experiments/static_scene/run_hybrid_depth.py fuse --no-mesh --no-color \
  --ma-run /srv/simai/runs/exp01/CAL102_w1_map_anything_known_k_20261026-s3 --prepared /srv/simai/datasets/classroom4-prepared-20261026 \
  --depth /srv/simai/runs/exp01/HYB283_da3_pose_${r}_$S --out /tmp/s4-pick/$r; done
/opt/simai/ai/.venv/bin/python ai/experiments/static_scene/session4_jobs.py --repo $PWD --session $S --only h7 --h7-res <고른 값> -o /srv/simai/setup/session1/exp-jobs-s4-h7.json
nohup simai-bootstrap queue --session $S --only a --exp-jobs /srv/simai/setup/session1/exp-jobs-s4-h7.json > /srv/simai/setup/session1/session4-h7.log 2>&1 &
```

같은 `--session` 이라 끝난 a 는 건너뛰고 h7 만 돈다. 시간이 모자라면 h7 은 건너뛰고 '넘긴 일'로 둔다.

**8) 결과 회수 (약 5~10분, 443 경로 약 1 MB/s).** 세션 묶음(로그 끝의 scp 명령: 큐 상태·로그·v4 Run 폴더의 `summary.json`·`samples.json`)과, 묶음에 들어가지 않는 실험 결과(50MB 넘는 rfull `raw.npz` 등)·I0 기록:

```sh
ssh simai-pod-443 'cd /srv/simai/runs && tar -czf s4-results.tar.gz exp01/*_<YYYYMMDD>-s4 i0-link'
scp simai-pod-443:/srv/simai/runs/s4-results.tar.gz ai/experiments/runs/gpu-session1/<YYYYMMDD>-s4/      # 노트북, 커밋 안 함
```

노트북에서 묶음 sha256 을 `bundle-manifest.json` 과 대조하고, 깊이 폴더마다 `run_hybrid_depth.py fuse`(설계 "다음 GPU 세션 큐" 아래 줄), MapAnything Run 마다 `world_frame.cli fix` → `compare_resolutions.py`.

**9) Pod Stop → Terminate.** 볼륨이 없어 회수를 확인한 뒤에만 지운다(GPU 과금).

| 구간 | 예상 |
|---|---|
| 켜기·올리기·미리 받기 | 20~30분 |
| LIVE 루프 연결 시험 | 별도 티켓 |
| I0 443·LTE | 30분 |
| 큐 + h7 | 60~70분 |
| 회수·Stop | 10분 |
| **합(LIVE 슬롯 제외)** | **약 2~2.5시간** |

재현: v4 는 큐 밖에서도 `python deploy/scripts/vlm_int.py --images /srv/simai/datasets/classroom4-prepared-20261026 --out <폴더>` 로 돈다. 3차의 v-int 는 손으로 돌려 스크립트가 남지 않아,
노트북 `ai/experiments/runs/gpu-session1/20261026-s3/v-int/summary.json`(단계·인자)과 `pod-logs/v-int.log` 를 따라 다시 썼다. 3차와 다른 점: vLLM 인자·환경·CPU 묶기를 손으로 적지 않고 감시 프로세스 코드에서 받는다,
요청마다 질문 앞에 다른 표식(prefix 캐시로 지연이 낙관적으로 나오지 않게), 영상 입력 시도는 뺐다(3차에서 됨), kill -9 뒤 감시 프로세스와 같은 그룹 정리로 그룹이 비는지 판정한다.

#### 4차 세션 교훈·GPU 세션 공통 주의 (S15P31A101-311)

- 4차 결과 요약은 문서에 숫자를 옮기지 않고 **Jira S15P31A101-293 2026-10-28 댓글**을 본다.
- 볼륨 없는 RunPod 에도 마운트되지 않은 `/workspace` 폴더가 있으므로 `choose_root()` 는 `/workspace` 가 마운트 지점(`os.path.ismount`)이고 쓸 수 있을 때만 `/workspace/simai` 를 고르고, 아니면 `/srv/simai` 하나로 모은다(4차: `SIMAI_ROOT` 없는 스크립트·큐가 `/workspace/simai` 를 골라 가중치 경로가 갈림).
- Pod 를 켠 직후 RunPod Logs 에서 `simai-start: sshd 시작 (키 인증만, 키 N개)` 와 `wstunnel 시작` 줄을 먼저 본다. `공개키 형식이 아닌 줄 … 버렸다` / `sshd 를 띄우지 않아 443 예비 터널도 띄우지 않는다` 가 보이면 템플릿 `PUBLIC_KEY` 값(한 줄, `ssh-ed25519 …`)을 고친다(4차: 이 때문에 약 45분 접속 불가).
- Pod 셸 명령은 `bash -lc '…'` 로 부른다(비로그인 셸은 PATH 에 `python3`·`simai-bootstrap` 이 없다). 백그라운드 스크립트도 `SIMAI_ROOT=/srv/simai` 를 명시한다(이 티켓 수정 뒤에도 습관으로).
- 노트북(Windows)에서 만든 tar 는 Pod 에서 `tar --no-same-owner -xf` 로 푼다(소유자 uid 변경 실패로 tar 가 실패 종료).
- 저장소 사본 코드를 이미지에 깔린 패키지보다 먼저 쓰려면 Job `env` 에 `PYTHONPATH=<repo>/ai/src:<repo>/ai`(4차: 이미지 37c7ae5 에 없는 `simai_ai.hmr.prompt` 로 HMR 실패).
- `gpu_queue` 는 실패한 Job 도 끝난 것으로 기억하고, `exp_job` 은 결과 폴더(metrics.json)가 있으면 덮어쓰지 않는다(종료 4) → 다시 돌릴 때는 큐 이름과 Run 이름을 새로 준다.
- Tier 0 file 모드(녹화 재생)는 저장소 `ai/` 에서 `simai-bootstrap run simai-tier0 --config <json>` 으로(다른 위치면 `services` 모듈을 못 찾는다).
- 공개 TCP 70000 대칭 포트 요청은 새 RunPod 템플릿 화면이 1~65535 만 받아 할 수 없다 → -288 (b) LTE Direct 비교는 그 방법을 찾기 전까지 뺀다. 443 터널 LIVE 는 공개 TCP 포트 없이 된다(`SIMAI_TUNNEL_ALLOW=17000,17001`).
- 4차 실제 순서(다음 세션 기준안): 촬영(빈 장소 25 s ×2: 배경용·축척 정물용, 보정 걷기 180 s) → Pod: 장소 MapAnything·걷기 관절 추출(GPU 수 분) → EXP 큐 동안 노트북에서 World 고정·정물 축척·보정 문서·R1 묶음 → LIVE(+동시 녹화) → 검증 큐(v4)·남은 EXP → 녹화 재생 → 결과 받기(4차 1.85 GB 가 443 터널로 약 8분) → sha256 확인 → Terminate.

#### 10차 세션 교훈 (S15P31A101-414, 2026-11-02)

- 워커 ID: `lobby --worker-token-file …/worker-s10.token` 만 주고 ID 를 안 넘겨 워커가 기본 `worker-01` 로 떠 EC2 대여 401 → 빠른 실패 5번 뒤 포기(아바타는 서버가 토큰으로 ID 를 찾아 괜찮았다). 이제 기동기가 `--worker-id` → `SIMAI_WORKER_ID` → 토큰 파일 이름 순서로 정해 워커·아바타에 넘기고 로그에 남긴다. 토큰은 `<워커 ID>.token` 이름으로 올린다.
- 아바타 빠짐: 템플릿 `SIMAI_SERVICES=tier0,vllm,worker`(통합 이미지 ENV 기본값과 같음)가 lobby 기본값을 덮었다. 이제 lobby·preflight 는 `SIMAI_SERVICES` 를 보지 않는다(줄이려면 `--services`). 템플릿에 둔다면 `tier0,vllm,worker,avatar`.
- 작업 폴더: `/srv/simai/setup/...` 에서 기동하니 `python -m services.tier0` 가 ModuleNotFoundError(이미지 WORKDIR `/opt/simai/ai` 밖). 이제 기동기가 이미지 ai 폴더를 자식 `PYTHONPATH` 끝에 더한다(cwd 는 그대로).
- SSH 셸 환경: 비로그인 셸은 컨테이너 PID 1 환경(PATH·SIMAI_*·RUNPOD_*)이 없어 `python3` 를 못 찾았다. `bash -lc` 또는 `. /etc/profile.d/simai-env.sh`, 그 파일이 없으면 `/proc/1/environ` 에서 비밀 아닌 이름만 싣는다(deploy/README "SSH 세션의 환경 변수").
- 인자 없는 `simai-bootstrap` 이 예전 교실 기본값으로 Tier 0 를 띄웠다 → 이제 사용법만 보이고 64(이미지 CMD 는 `tier0` 명시, 시작 명령 `sleep infinity` 경로는 그대로).
- py-spy: RunPod 컨테이너에는 SYS_PTRACE 가 없어 Permission Denied. preflight 가 `diagnostics.py_spy=unavailable` 로 안내만 하고(실패 아님) 내장 계측(-383 인지 단계 wall/thread_time)을 쓴다.
- 게이트웨이 원격 갱신: `ssh simai-gw` 의 `setup`(git pull)은 wincredman 을 못 써 실패 → `update-remote`(로그인 세션 예약 작업으로 pull·uv sync, 끝나면 이전 모드 복구, gateway/ops/README "원격"). EC2 `free-ports.sh` 는 `sudo -n ss`·`sudo -n kill` 로 다른 sshd 세션의 PID 를 찾는다.

### 실행기 규칙

| 상황 | 동작 | 종료 코드 |
|---|---|---|
| 모든 항목 성공 / 실패 항목 있음 | 묶음·scp 출력 | 0 / 1 |
| 항목 제한 시간 초과 | 그 항목 FAILED(타임아웃), 프로세스 트리 정리, 다음 항목 진행 | 1 |
| 전체 시간 상한 | 도는 항목을 끝내 ABORTED 기록, 남은 항목은 돌리지 않음(다음 실행 때 이어서) | 8 |
| Ctrl-C·SIGTERM | 같은 방식으로 멈추고 묶음은 만든다 | 130 |
| LIVE 관제 중(볼륨 `state/tier0.live` 가 30초 안에 갱신됐거나 `services.tier0` 프로세스) | 세션을 시작하지 않음. 실험 이미지 큐(g·m)·`--exp-jobs` 는 gpu_queue LIVE 보호(on_live=stop)로 멈춤 | 3 |
| 결과 폴더에 쓸 수 없음·남은 용량 하한 미만(기본 2GB) | 항목을 돌리지 않음(GPU 작업 전) | 5 |
| 실행 중 큐 예외(예: 디스크 가득 참으로 상태 파일 쓰기 실패) | 잠금은 gpu_queue 가 풀고, 이 큐의 Job 프로세스(`SIMAI_QUEUE_NAME`)를 찾아 끝낸 뒤(Linux) 묶음을 만든다 | 6 |
| 묶음을 볼륨·임시 폴더 어디에도 못 씀 | 큐 폴더 경로를 출력 | 7 |
| 다른 큐가 잠금을 쥠 / Job 정의 오류 | gpu_queue 와 같다 | 2 / 4 |

비밀이 묶음에 섞이지 않게: 시작할 때 이름에 TOKEN·SECRET·PASSWORD·API_KEY·PRIVATE 가 든 환경 변수를 이 프로세스에서 지워 Job 에 넘기지 않는다.
묶음은 큐·세션 폴더만 모으고, 심볼릭 링크·`.simai-private` 아래·비밀 이름(`hf.env`, `*.env`, `*.key`, `*.pem`, `*token*` 등)·대형 산출물(`*.engine`, `*.pt`)·50MB 초과 파일과
내용에 개인 키·HF 토큰·토큰 할당이 보이는 파일을 뺀다(뺀 목록과 이유는 manifest 에, 내용은 남기지 않음).

### c: LIVE 입력 Pod 확인 (`deploy/scripts/session1_live.py`, -182 'Pod 확인' 1~5)

| 단계 | 내용 | 통과 기준 |
|---|---|---|
| c0 | 컨테이너 CPU 기록: 자기 cgroup(`/proc/self/cgroup`·`mountinfo`, v2 하위 그룹 `cpu.max`·v1 공동 마운트 `cfs_quota_us`, 조상 중 최소) 할당량·`os.sched_getaffinity`·호스트 `os.cpu_count()`·`/proc/cpuinfo` 모델(-236) | 기록만(선택). `usable_vcpu` = 셋 중 작은 값. 호스트 값(H100 Pod 224)을 할당량으로 보지 않는다 |
| c1 | `NvdecDecoder()` 열기 | 선택(-236, NVDEC 은 선택 디코더). "nvdec ok". 실패면 이미지 `NVIDIA_DRIVER_CAPABILITIES` 에 video 가 있는지 |
| c2 | `pytest -m gpu tests/io/test_pyav_decoder.py` (자식 환경에 `SIMAI_TEST_GPU=1`) | 선택(-236). 통과 1 이상, 건너뜀 0 |
| c3 | 2304x1296 H.264 4스트림(10초, 25 fps) CPU 디코딩(기본 디코더) + BGR, 카메라마다 스레드 하나(LIVE 구조). 참고(판정 안 함): CPU 한 스레드 번갈아, NVDEC 한 스레드·카메라별 스레드·변환 스레드 0·1·nv12 (`docs/design/tier0-nvdec-throughput.md`) | 그림 한 장 p95 ≤ 10 ms, 4대 합계 ≥ 100 fps, 넣은 수 = 나온 수 (-236 에서 NVDEC 한 스레드 → CPU 카메라별 스레드로 바꿈) |
| c4 | LIVE 하네스 120초: 진입점(엔진 준비 → Tier 0 LIVE, 기본 디코더 cpu) + 노트북 흉내(I0 제어 채널·시계 교환·하트비트, `tier0_site.Laptop`) + 영상 송신 4대(`live_video` 프레임 번호 무늬 H.264, 2304x1296, 25 fps, 127.0.0.1) | 종료 코드 0, 카메라 4대 ready, decode_failed 0, detector_errors 0, mismatched 0, 검출 호출 > 0. `frame_age_ms_p95`·`pipeline_lag_ms` p50·p95 기록 |
| c5a | 기하 단계 4대 × 1·5·10명 (`tests/geometry/bench_tier0_geometry.py`, Pod CPU) | 10명 pipeline p95 ≤ 50 ms (명세 6.6 단계 목표) |
| c5b | 10명 녹화 LIVE 재생 | 큐에서는 SKIP(선택). 아래 수동 절차 |

한계: 송신 영상은 사람이 없는 무늬라 압축이 쉬워 디코딩 비용이 실제 카메라 영상보다 작다(하한). 검출은 사람 수와 거의 무관하게 묶음 단위로 돌지만,
추적·연결·판정 부하는 c5a(기하)로만 본다. 하네스는 노트북·EC2 없이 Pod 안 127.0.0.1 로 돌아 망 구간(I0 Direct TCP)은 재지 않는다.

**c5 수동 (10명 처리 시간, 녹화 재생):** 10명 이상이 보이는 A0 녹화(또는 같은 조건 영상)가 있을 때.
1. Pod 에서 Tier 0 를 LIVE 설정(`input.mode live`, 디코더는 기본 cpu, 영상 포트 `SIMAI_TIER0_VIDEO_PORTS`)으로 띄우고, 노트북에서 `gateway/relay` 를
   녹화 파일 입력(`kind = "file"`)과 `--send` 로 붙인다(EC2 엔드포인트 조회·공개키 고정). 사람이 없는 영상으로 경로만 볼 때는 `session1_live.py serve` 를 쓴다.
2. 표시 파일 `state/tier0.live` 의 `status.live_input` 에서 `frame_age_ms_p95 ≤ 150 + 검출 시간`, health `pipeline_lag_ms` 를 10분 기록하고, 검출 4대 묶음 38 ms(명세 5.1 기준선)와 비교한다.
3. 결과는 `runs/live10/` 에 두고 MR·Jira 에 요약한다(제품 성능 지표로 쓰기 전 G0 조건 확인).

### d: Tier 0 GPU 우선 보호 측정 (`deploy/scripts/session1_gpu_priority.py`, -60 'Pod 측정' P0~P7)

Tier 0 는 c4 와 같은 LIVE 하네스로 띄우고(측정 동안 계속), `ai/experiments/gpu_queue/tier0_priority_probe.py` 의 `load`(더미 분석 부하, 상한 0.70)와
`watch`(표시 파일 `gpu_priority` 판정)로 잰다. 결과는 Run 폴더와 `$SIMAI_ROOT/runs/g0-tier0-priority/<UTC>/` 에 남는다.
watch 마다 probe 의 `metrics.json` 은 Run 폴더 아래 `.watch-<단계>/` 에 따로 쓴다(실행기가 폴더를 먼저 만든다. 1차 세션에서는 폴더가 없어 측정 통과인데 FAIL 이었다, S15P31A101-213).

부하 크기는 감지한 GPU 메모리(`nvidia-smi memory.total`) 비율이다(-205): H100 80GB(81,559 MiB)에서 아래 40·20·70 GB 그대로, 다른 GPU 는 같은 비율(1,000 MB 단위).
상한 0.70 은 비율 그대로다. 예: L40S 48GB(46,068 MiB) 23·11·40 GB, RTX 4090 24GB 는 Tier 0 전용 사양(48GB급 미만)이라 분석 부하를 같은 GPU 에 올리지 않으므로
P3·P5·P6 을 SKIP + 이유로 남긴다(선택 단계로 바뀌어 필수 실패로 세지 않음). GPU 메모리를 읽지 못하면 SKIP 이 아니라 FAIL 이다.

| # | 내용 | 통과 기준 | 시간 (1차 → 2차 실측) |
|---|---|---|---|
| P0 | `pytest tests/experiments/gpu_queue tests/gpu tests/services/worker/test_runner_tier0_priority.py` (Linux 전용 시험 포함) | 모두 통과 | 약 3분 → 23.1초 |
| P1 | 표시 파일 `gpu_priority`, `nvidia-smi --query-compute-apps` 기록 | phase=running, reserve.state=OK, reserved_mb ≥ 8192 | 즉시 |
| P2 | 부하 없는 기준선 watch | passed (lag p95 ≤ 600 ms, unknown 0, 판정 오류 증가 0) | 120초 → 120.2초 |
| P3 | 더미 부하 40 GB 동안 watch | passed, Tier 0 로그 `out of memory` 0건 | 120초 → 135.3초 |
| P4 | VLM(vLLM) 부하 | SKIP(선택): 처리기가 아직 없다 | - |
| P5 | 더미 부하 20 GB 를 watch 30초째에 kill -9 | passed (D-G0-08 판정 지속) | 90초 → 90.2초 |
| P6 | 상한(0.70 = 56 GB)을 넘는 70 GB 부하 | 부하는 메모리 부족으로 끝나고 watch 는 passed | 60초 → 60.1초 |
| P7 | 분석 워커 접수 문 | SKIP(선택, 수동): 처리기·EC2 가 있어야 한다 | - |

`--full` 은 -60 MR 의 G0 판정용 시간(watch 600·600·180·120초)이다. 1차 세션은 경로 확인용으로 줄인 시간을 쓰고, G0 판정은 `--full` 결과로 한다.
분석 워커를 Pod 에서 띄울 때의 순서(Tier 0 준비 뒤)는 deploy/README "분석 워커"(`simai-wait-tier0`)에 있다.

### e: Network Volume 강제 종료 복구 (`deploy/scripts/crash_recovery_g3.py`, -92)

`--volume-dir`(Pod: `/workspace/simai/runs/crash92`, 노트북: 로컬 폴더) 아래 `<UTC>/` 에서 GPU 없이 돈다(SIM 2D keypoint).

1. 실제 Tier 0 프로세스(-171 `python -m services.tier0`, 로컬 wss EC2 흉내·노트북 시험 상대)를 같은 상태 폴더로 kill -9 하고 다시 띄운다.
   전이 직후(R1 `local_alert` RAISED 수신 직후, fsync 전일 수 있음) → ACK 직후(`site_ack` → `alarm_stop` 수신 직후) → 체크포인트 기록 중(주기 50ms, `checkpoints/*.tmp` 가 보이면, 3회) → 정상 종료.
2. -51 결정적 지점(`tests/recovery/test_crash_recovery.py` 시험 함수를 볼륨 경로로 호출): 전이 직후, 체크포인트 임시 파일 fsync 뒤·rename 전, EC2 ACK → 저널 정리 직후, 해소 뒤 과거 RAISED 재기록, 바쁜 임의 시점(seed 1~4).

매 강제 종료 뒤 비교: 종료 시점 저널을 payload 로만 접은 정답(`site_driver.journal_truth`) vs (a) 상태 폴더 사본에서 recover 한 결과(`assert_matches_journal`), (b) 다시 띄운 서비스가 쓴 RESTORED 전이(sequence +1)·`event_state_snapshot`, (c) 재시작 첫 health.

| G3 항목 | 세는 것 (0건이어야 PASS) |
|---|---|
| D-G3-11 | 사본 recover 또는 재시작 서비스의 복원이 저널 정답과 다름 |
| D-G3-13 | EC2 ACK → 정리 직후 종료 뒤 해소 이벤트가 되살아남 |
| D-G3-14 | 종료 시점 저널 끝까지의 전이가 재시작 뒤에도 EC2 에 전달되지 않음(같은 msg_id) |
| D-G3-18 | EC2 로 나간 전이가 저널(fsync)에 없음 |
| D-G3-09 | 재시작 첫 health 가 UNSAFE_UNKNOWN 이 아님, 첫 health 현장 경보 목록에 복원 현장 경보(CRITICAL·HIGH) 빠짐, 사람 대상 복원 이벤트가 복원 뒤 해소됨(사람이 그대로 서 있음), 구역 대상(R7) 복원 이벤트가 복원 뒤 VERIFIED 가 해소 유지 시간 동안 이어지지 않았는데 해소됨(-198) |

D-G3-09 의 구역 대상 기준(S15P31A101-198): 복원 보호(-174)대로 구역 대상 복원 이벤트는 감시 확보 근거가 있는 판정에서만 VERIFIED 가 되고 그
상태가 해소 유지 시간 동안 이어져야 해소된다. 하네스는 EC2 로 나간 전이 열(복원 뒤 sequence)로 그 구간을 확인하고, 전이가 빠졌으면 위반으로 센다.
-192 리뷰의 부하 중 1건은 이 경로였다: 시동 첫 판정에 프레임이 아직 없는 카메라를 정상으로 세지 않아(모르면 저하) R7 이 잠깐 나고, 해소(시험 설정 유지 시간 1.5 s)
전에 첫 kill(R1 발생 직후)이 와 복원된 뒤 다음 프로세스에서 근거로 해소됐다. 예전 하네스는 모든 복원 이벤트의 해소를 위반으로 셌다. 결정적 재현은
`ai/tests/deploy/test_crash_recovery_g3.py::test_late_cameras_at_start_restore_r7_and_the_run_still_passes`(cam2~4 첫 1.2 s 프레임 제거)다.

판정: 위반이 있으면 FAIL(종료 1). 시나리오가 돌지 못하면(서비스 기동 실패·자식이 지점에 닿기 전에 죽음·아무것도 안 돎) 위반 0건으로 보지 않고 ERROR(종료 2).
체크포인트 기록 중 종료는 종료 뒤 `*.tmp` 가 남은 횟수(`checkpoint_tmp_caught`)로 실제로 기록 도중이었는지 확인한다. 0 이면 서비스 경로에서는 확인하지 못한 것이고 결정적 지점 결과로 본다.
강제 종료 직후 저널 작성자 잠금(flock)이 늦게 풀려 재시작이 실패하면 다시 띄우고 횟수(`lock_retries`)를 남긴다(Network Volume 잠금 해제 지연 실측).
시험용 TLS 인증서·개인 키는 볼륨이 아니라 임시 폴더에 만들고 끝나면 지운다.

### 노트북 드라이런과 반례 시험

- 드라이런: `--mode dryrun --root <로컬 폴더>` 는 가짜 nvidia-smi(gpu_queue 시험의 `fake_nvidia_smi.py`, H100·드라이버 550)와 로컬 폴더를 쓴다. a·e·f 는 통과하고, b(GPU·torch 없음)·c(-182 전: NVDEC·live_video 없음, c5a 기하 측정만 돎)·d(-60 전: 측정 도구 없음)는 이유를 남기고 실패한 뒤 다음 항목이 이어 돈다(Windows 약 1분 40초).
  Windows 는 경로 길이 한도(260자)에 걸리지 않게 짧은 폴더를 쓴다(구성 묶음 보관 파일 이름이 길다). b 의 bash 는 Git Bash 를 찾아 쓴다(System32 의 WSL bash 는 Windows 환경 변수를 받지 못한다).
- 반례 시험 `ai/tests/deploy/`: c 의 LIVE 하네스(-182 머지 뒤에 돈다: 실제 Tier0Service + 검출·추적 대역 `stub_tier0_live.py`, CPU 디코더 — 4대 ready PASS, 틀린 영상 토큰은 ready 안 됨 FAIL, 기동 실패는 빨리 FAIL),
  `simai-wait-tier0`(엔진 준비 중·기동 중·PARTIAL·오래됨·깨짐은 띄우지 않음, 시간 초과 코드 3, `--no-tier0` 인데 Tier 0 있음 코드 4, env 파일 비밀 비노출), 항목 제한 시간 초과, 실패 항목 뒤 진행, 자리만 항목 미등록, 전체 시간 상한, 중간 Ctrl-C 와 이어 돌기, 용량 부족·결과 폴더 쓰기 실패·실행 중 디스크 가득 참·묶음 폴더 실패, 비밀 파일·값·환경 변수 차단, LIVE 중 시작 거부·EXP 큐 정지, Pod Job 파일의 gpu_queue·run_record 검증, -92 줄인 실행 PASS·음성 대조(저널에 없는 이벤트 → D-G3-11 FAIL)·판정 불가 분류.

## 임의 GPU·지역·사이트 실행 환경 (S15P31A101-205)

이미지 시작 순서·명령은 deploy/README "simai-bootstrap", 사이트별 절차는 같은 문서의 "RunPod 템플릿(볼륨 없음)"·"Vast.ai"·"GPU VM(엘리스) docker run" 에 있다.
코드: `deploy/scripts/simai_bootstrap.py`(이미지 `simai-bootstrap`), `deploy/weights.json`, `deploy/base/simai-start.sh`. 시험: `ai/tests/deploy/test_simai_bootstrap.py`, `test_session1_portable.py`.

명세에 없어 정한 수치와 근거:

| 값 | 근거 |
|---|---|
| 최소 VRAM 22,500 MiB(24GB급), compute_cap 8.0 | 결정 "Tier 0 만 24GB·Ampere 이후". A10·L4 는 24GB 라도 nvidia-smi 에 약 23,000 MiB 로 보인다 |
| Tier 0 + 분석 공유 하한 45,000 MiB(48GB급) | 결정 "Tier 0+실험 48GB". L40S 46,068·A6000 49,140 MiB 를 포함한다. 이 아래는 d 의 P3·P5·P6 SKIP |
| d 부하 기준 81,559 MiB | H100 80GB 의 nvidia-smi memory.total. 이 값에서 -60 MR 의 40·20·70 GB 와 같다 |
| 부트스트랩 남은 용량 하한 20GB(`SIMAI_MIN_FREE_GB`) | 가중치(약 0.1GB)·엔진(수백 MB)·obs2d·결과 여유. 컨테이너 디스크 100GB 에서 이미지(약 19.5GB)를 빼도 넉넉하다 |
| 볼륨 없을 때 저장 위치 `/srv/simai` | 이미지의 `/opt/simai`(코드)와 나누고, `/workspace` 는 이미지에 없어야 한다(check_image). RunPod 은 볼륨이 없어도 마운트되지 않은 `/workspace` 폴더를 두므로 `os.path.ismount('/workspace')` 가 참일 때만 `/workspace/simai` 를 고른다(S15P31A101-311) |
| yolo26x-pose sha256 `08ed9e01…` | E-GATE-0 일본 볼륨의 `weights/hmr_weights.sha256`(ultralytics 릴리스 v8.4.0 에서 받은 파일) |

버린 대안: 가중치 받기를 GPU 확인보다 먼저(GPU 없는 곳에서 받기만 하고 실패), 목록에 해시가 없을 때 첫 다운로드를 믿고 기록(TOFU, '모르면 비움'과 어긋남),
queue 모드에서도 엔진 준비(세션 b 항목의 첫 빌드 시간 측정이 캐시 사용으로 바뀜), 컨테이너 안에서 HF 토큰 파일 읽기(엘리스는 `--env-file` 로 호스트 docker 가 읽게 해 경로가 하나뿐).

잔여 위험: Vast 의 공개키 변수(`SSH_PUBLIC_KEY`)는 Vast 문서의 환경 변수 목록으로만 확인했고 실제 인스턴스에서 보지 않았다. 엘리스 VM 의 드라이버·Container Toolkit·블록 스토리지 마운트는
첫 시연 준비 때 확인한다. Vast 의 공개 IP·포트 환경 변수 이름(`PUBLIC_IPADDR`·`VAST_TCP_PORT_*` 로 알고 있음)은 docs.vast.ai 문서 주소를 찾지 못해(404) 확인하지 못했다. 그래서 Tier 0 는 읽지 않고, 사람이 사이트 무관 이름으로 넣는다(아래 표). run_record `gpu.provider` 는 2.2.0 에서 `VAST`·`OTHER` 가 생겼다(-208). 사이트 신호는 `RUNPOD_POD_ID`→RUNPOD, `VAST_TCP_PORT_22`→VAST 뿐이고 그 밖은 LOCAL 이며, 사이트 미정 클라우드는 Job 의 `gpu.provider` 에 OTHER 를 적는다.

공개 주소 환경 변수(S15P31A101-208, 앞이 우선, 값이 있는 첫 이름만 읽고 틀린 값이면 다음 이름으로 넘어가지 않는다):

| 값 | 먼저 읽는 이름 | 그다음 | 둘 다 없으면 |
|---|---|---|---|
| 공개 IPv4 | `SIMAI_PUBLIC_HOST` | `RUNPOD_PUBLIC_IP` | 엔드포인트 등록 오류(이름 둘을 모두 적음) |
| 공개 포트(요청 번호별) | `SIMAI_PUBLIC_PORT_<번호>` | `RUNPOD_TCP_PORT_<번호>` | 등록·영상 수신·제어 채널 설정 오류 |

이미지 줄바꿈: Windows 체크아웃의 CRLF 가 shebang 을 깨뜨리지 않게 deploy/base·deploy/tier0 Dockerfile 이 COPY 한 실행 스크립트의 CR 을 빌드에서 뺀다(`sed -i 's/
$//'`). `check_image.sh` 의 CR 검사는 결과를 다시 확인한다(Docker 를 켜지 않아 빌드는 하지 않았다).

## 첫 세션 수동 확인 목록 (큐 밖)

- SSH 키 접속과 비밀번호 접속 거부(deploy/README Pod 검증 큐 8).
- Pod 재시작 뒤 포트 변경과 위 재접속 절차, 같은 `--session` 이어 돌기.
- 443 예비: wstunnel 경유 SSH·scp(3차 세션에서 확인, 이미지 반영 뒤 다시 확인), 10분 이상 연속 연결·100초 끊김, `--tls-verify-certificate`(SSAFY 망 확인됨 2026-10-26, 시연장 망은 다시), I0 터널 지연(위 "I0 443 예비 경로 측정").
- Network Volume 을 붙인 Pod 의 Stop 가능 여부(명세 19.1 미확인 항목).
- 끝나면 Pod Stop, 결과 묶음 sha256 을 manifest 와 대조.
