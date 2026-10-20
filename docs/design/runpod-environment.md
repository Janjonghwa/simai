# RunPod 환경: Pod 점검, Network Volume 구조, 접속, GPU 1차 세션 큐

S15P31A101-183(GPU 1차 세션 검증 큐)이 S15P31A101-31(RunPod 점검·Volume 구조)과 S15P31A101-92(강제 종료 복구 실측)의 노트북 준비분을 함께 담았다.
근거는 명세 3.12(리전·GPU·저장소·연결), 19.1(볼륨 구조), 3.2(저널 계약·재시작 복구), 11.5(보관), 15.1 G3·부록 D.4, 20.5(GPU Job Queue), AGENTS.md RunPod 규칙이다.
이미지·템플릿·엔진 캐시는 `deploy/README.md`, 재시작 복구 운영 절차는 `docs/design/tier0-recovery.md` 에 있다.

## 원칙

- GPU·지역은 고정하지 않는다(2026-10-20 결정, S15P31A101-205): 개발·시험은 RunPod 의 그때그때 남은 GPU·지역(볼륨 없음, 컨테이너 디스크 100GB+), 예비는 Vast.ai,
  시연은 엘리스 A100 VM + 블록 스토리지(`docker run`, deploy/README "GPU VM(엘리스) docker run"). 일본 Network Volume 은 폐기했다. 한 시점에 GPU 하나, Tier 0 가 먼저 쓴다.
  아래 "Network Volume 디렉터리 구조"는 저장 위치(`$SIMAI_ROOT`: 볼륨·블록 스토리지·컨테이너 디스크) 안의 구조로 읽는다. 저장 위치는 `simai-bootstrap` 이 고른다.
- GPU Pod 는 검증할 때만 켠다. 코드·스크립트·큐는 노트북에서 끝내고(이 문서의 드라이런), Pod 에서는 큐를 돌린 뒤 결과를 가져오고 바로 Stop 한다.
- LIVE 관제 시간에는 세션 큐를 돌리지 않는다(세션 실행기가 Tier 0 표시 파일·프로세스를 보고 시작을 거부한다). EXP Job 은 LIVE 보호를 켠 별도 큐로 마지막에 돈다.
- 접속은 Pod 공개 IP:포트 SSH 직접 연결(키 인증만)이 기본, RunPod HTTPS 프록시(443)가 예비다. Tailscale 은 쓰지 않는다.
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
| GPU | 최소 사양(Ampere 이상, VRAM 24GB급) GPU 하나 | 여러 개, compute_cap·메모리를 읽지 못함 | nvidia-smi 실패, 최소 사양 미만 |
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
- 주소: RunPod 콘솔 Pod → Connect → "SSH over exposed TCP" 의 `ssh root@<IP> -p <포트>`. Pod 안에서는 `RUNPOD_PUBLIC_IP`, `RUNPOD_TCP_PORT_22`(pod_check 가 출력).
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

### 443 예비 경로 (SSH·임의 포트가 막힌 망)

- RunPod HTTPS 프록시 `https://<POD_ID>-<포트>.proxy.runpod.net`(443)는 HTTP·WebSocket 서비스용이다. 주소가 공개이므로 토큰 인증이 필수이고, 연결은 최대 100초로 끊긴다(명세 3.12). I0·I1 의 예비 경로이며 SSH·scp 는 이 프록시로 가지 않는다.
- 막힌 망에서 세션 큐를 운영할 때: RunPod 콘솔의 웹 터미널(브라우저, 443)로 들어가 `run` 을 `nohup` 으로 띄우고, 결과 묶음은 SSH 가 되는 망(LTE 테더링 등)으로 옮겨 scp 로 가져온다. 묶음을 HTTP 로 공개 서빙하지 않는다(토큰 없는 공개 주소가 된다).
- 웹 터미널·`runpodctl send/receive` 가 이 이미지에서 되는지는 아직 확인하지 않았다(첫 세션 수동 확인 목록).

## GPU 1차 세션 큐 (`deploy/scripts/gpu_session1.py`)

`ai/experiments/gpu_queue`(S15P31A101-36)를 같은 프로세스에서 그대로 돌린다. 항목별 명령·제한 시간·Run 기록(`contracts/experiments/run_record.schema.json`)·잠금·heartbeat·재시작 복구는 gpu_queue 가 맡고,
세션 실행기는 Job 파일 생성, 사전 점검, 전체 시간 상한, 결과 묶음, scp 명령을 더한다.

| 항목 | Jira | 내용 | H100 예상 | 제한 | 상태 |
|---|---|---|---|---|---|
| a | -31 | Pod 점검(위 표) | 2분 | 15분 | 큐 |
| b | -32 | Tier 0 이미지 GPU·torch CUDA·TensorRT 확인, 엔진 첫 빌드·캐시 재사용·batch 4 추론, 진입점(설정이 있으면), 가중치 반례(종료 코드 2), sm_86 경로 분리 (`session1_engine.sh`, deploy/README "Pod 검증 큐" 1~7) | 15분 | 45분 | 큐 |
| t | -183 | 시험 도구(pytest·cryptography, `ai/uv.lock` 버전)를 컨테이너 디스크에 `uv pip install --target` (Tier 0 이미지에는 시험 의존성이 없다) | 1분 | 10분 | 큐(Pod 만) |
| c | -182 | LIVE 입력 Pod 확인 (`session1_live.py all`, 아래) | 10분 | 40분 | 큐, **-182 머지·이미지 재빌드 필요** |
| d | -60 | Tier 0 GPU 우선 보호 측정 P0~P7 (`session1_gpu_priority.py`, 아래) | 15분 | 45분 | 큐, **-60 머지 필요** |
| e | -92 | Network Volume 강제 종료 복구 (아래) | 5분 | 30분 | 제외: 엘리스 블록 스토리지 세션으로 옮김(-205) |
| f | -45·-175 | `test_i1_tls.py`·`test_serial_tls.py` 를 Linux 에서 5회 반복 (`repeat_pytest.py`) | 10분 | 40분 | 제외: GPU 가 필요 없어 GPU 세션에서 뺌(Linux CPU 에서) |
| g | -87 | EXP-02 다시점 집계(선택). LIVE 가 없을 때만, 마지막 | 30분(가정) | 60분 | 자리만: 실험 이미지 티켓 후 `--exp-jobs` 로 |
| m | - | MapAnything 정적 재구성 (torch 2.7.1 cu126) | 15분(가정) | 30분 | 자리만: 실험 이미지 티켓 후 |

- 순서: Pod 점검 → Tier 0 이미지·엔진(Tier 0 가 먼저, c·d 가 이 엔진 캐시를 쓴다) → 시험 도구 → LIVE·자원 보호 → 복구·TLS → EXP 는 맨 끝. 한 항목이 실패해도 다음 항목으로 간다(`continue_on_failure`).
- c·d 는 -182·-60 이 머지된 뒤의 경로(`simai_ai.io.NvdecDecoder`, `tests/services/tier0/live_video.py`, `experiments/gpu_queue/tier0_priority_probe.py` 등)를 부른다. 머지 전이면 각 단계가 '없음(머지 필요)'으로 실패하고 다음 항목으로 간다. PyAV 는 -182 가 `io` extra 에 넣었으므로 그 뒤에 빌드한 이미지여야 c 가 돈다.
- 예상 시간 근거: a 는 1GB 쓰기+fsync 200회, b 는 deploy/README 의 "H100 에서 엔진 첫 빌드 수 분" + 캐시·추론, c 는 c4 LIVE 120초 + 엔진 캐시 기동 + 인코딩·디코딩 측정·기하 측정, d 는 줄인 시간(watch 120·120·90·60초) + P0 시험, e·f 는 노트북 실측(e 약 40초, f 1회 약 35초)에 Network Volume fsync 지연과 5회 반복을 넉넉히 더했다. g 는 -87 이 정할 때까지의 가정이다.
- **H100 총 예상**: 지금 큐 약 43분(e·f 제외 뒤) + Pod 기동·이미지 받기(압축 약 7.4GB) 약 10분 + 가중치 받기(볼륨 없음, 수십 초) + 사본 올리기·결과 회수 약 5분 ≈ **1시간**. d 를 G0 판정용(`--full`, watch 600초)으로 돌리면 +20분. 전체 시간 상한 기본값은 9000초(2.5시간). 다른 GPU 는 엔진 빌드·추론 시간이 다르다(H100 예상만 적었다).
- 볼륨 없는 Pod(-205): 저장 위치는 `simai-bootstrap` 과 같은 규칙(`SIMAI_ROOT` → `/workspace/simai` → `/srv/simai`)이고, 마운트 지점이 아니면 a 에 `--no-volume`(볼륨 측정 SKIP)을 붙이고 Job 파일에 볼륨·데이터센터를 적지 않는다(`provider`·`datacenter` 는 run_record 가 `RUNPOD_POD_ID`·`RUNPOD_DC_ID` 로 채움, Vast·엘리스는 계약 값이 없어 LOCAL). 결과 묶음은 컨테이너 디스크에 있으므로 **Pod 를 지우기(Terminate) 전에** scp 로 가져온다(실행기가 출력).
- 결과 기록은 run_record 계약을 따른다. `experiment_id` 는 운영 경로 검증 Run 값 `VAL-01`(run_record 2.1.0, `e_gate` 없음, S15P31A101-197), `experiment_state` 는 `BENCHMARK`, 입력 출처는 `SIM`·`is_upper_bound: true` 다(제품 성능 지표로 쓰지 않는다). 종료 코드는 `notes`, 지표는 각 스크립트가 `metrics.json` 에 남긴다.

### 실행 순서

```sh
# 0) 노트북: 큐 계획과 드라이런 (GPU 없음)
uv run --project ai python deploy/scripts/gpu_session1.py plan --mode pod
uv run --project ai python deploy/scripts/gpu_session1.py run --mode dryrun --root <짧은 로컬 폴더>
# 1) 노트북: 커밋된 HEAD 의 저장소 사본과 git 정보 → Pod 로 올리는 명령이 출력된다 (볼륨 없는 Pod 는 --pod-simai /srv/simai)
uv run --project ai python deploy/scripts/gpu_session1.py prepare --out <폴더> --pod-simai /srv/simai
# 2) Pod 켜기: Tier 0 템플릿(deploy/README, 볼륨 없음), 시작 명령 sleep infinity. 1) 이 출력한 scp·ssh 명령으로 사본을 풀고
#    simai-bootstrap queue 로 띄운다(GPU 세대 감지·가중치 받기·해시 확인 → 큐)
ssh simai-pod 'nohup simai-bootstrap queue > /srv/simai/setup/session1/session1.log 2>&1 &'
ssh simai-pod 'tail -f /srv/simai/setup/session1/session1.log'      # 끊겨도 큐는 계속 돈다
# 3) 끝나면 로그 끝에 나온 scp 명령으로 결과 묶음을 가져온 뒤(볼륨 없음: 지우기 전에) RunPod 콘솔에서 Pod Stop·Terminate
```

- `python` 은 이미지 venv(`/opt/simai/ai/.venv`, jsonschema 포함)다. 저장소 사본의 `ai/src` 를 `PYTHONPATH` 앞에 두어 사본 코드로 돈다(b 의 엔진 준비는 이미지에 설치된 모듈).
- 같은 `--session` 으로 다시 돌리면 끝난 항목은 건너뛴다(중단·Pod 재시작 뒤 이어 돌기). 처음부터 다시 돌리려면 다른 세션 이름을 쓴다.
- 결과 묶음: `/workspace/simai/runs/gpu-session1/<세션>/session1-<세션>-<UTC>.tar.gz` = `session.json`(항목별 상태·종료 코드·자리만 항목·시간), `bundle-manifest.json`(파일 sha256, 뺀 파일과 이유), 큐 상태·로그·Run 폴더 전체, `jobs.json`. 볼륨에 못 쓰면 `/tmp/simai-session1/` 로 물러서고 그 경로를 출력한다.
- 노트북에서는 `ai/experiments/runs/gpu-session1/<세션>/` 에 풀어 둔다(`.gitignore` 의 `runs/`, 커밋하지 않음). Run 요약은 MR·Jira 로 남긴다.

### 실행기 규칙

| 상황 | 동작 | 종료 코드 |
|---|---|---|
| 모든 항목 성공 / 실패 항목 있음 | 묶음·scp 출력 | 0 / 1 |
| 항목 제한 시간 초과 | 그 항목 FAILED(타임아웃), 프로세스 트리 정리, 다음 항목 진행 | 1 |
| 전체 시간 상한 | 도는 항목을 끝내 ABORTED 기록, 남은 항목은 돌리지 않음(다음 실행 때 이어서) | 8 |
| Ctrl-C·SIGTERM | 같은 방식으로 멈추고 묶음은 만든다 | 130 |
| LIVE 관제 중(볼륨 `state/tier0.live` 가 30초 안에 갱신됐거나 `services.tier0` 프로세스) | 세션을 시작하지 않음. EXP 큐(g)는 gpu_queue LIVE 보호(on_live=stop)로 멈춤 | 3 |
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
| c1 | `NvdecDecoder()` 열기 | "nvdec ok". 실패면 이미지 `NVIDIA_DRIVER_CAPABILITIES` 에 video 가 있는지(이 MR 에서 넣었다) |
| c2 | `pytest -m gpu tests/io/test_pyav_decoder.py` | 통과 1 이상, 건너뜀 0 |
| c3 | 2304x1296 H.264 4스트림(10초, 25 fps) NVDEC 디코딩 + BGR 내려받기, 한 스레드에서 번갈아(보수적). CPU 도 같은 측정(비교만) | 그림 한 장 p95 ≤ 10 ms, 4대 합계 ≥ 100 fps, 넣은 수 = 나온 수 |
| c4 | LIVE 하네스 120초: 진입점(엔진 준비 → Tier 0 LIVE, decoder nvdec) + 노트북 흉내(I0 제어 채널·시계 교환·하트비트, `tier0_site.Laptop`) + 영상 송신 4대(`live_video` 프레임 번호 무늬 H.264, 2304x1296, 25 fps, 127.0.0.1) | 종료 코드 0, 카메라 4대 ready, decode_failed 0, detector_errors 0, mismatched 0, 검출 호출 > 0. `frame_age_ms_p95`·`pipeline_lag_ms` p50·p95 기록 |
| c5a | 기하 단계 4대 × 1·5·10명 (`tests/geometry/bench_tier0_geometry.py`, Pod CPU) | 10명 pipeline p95 ≤ 50 ms (명세 6.6 단계 목표) |
| c5b | 10명 녹화 LIVE 재생 | 큐에서는 SKIP(선택). 아래 수동 절차 |

한계: 송신 영상은 사람이 없는 무늬라 압축이 쉬워 디코딩 비용이 실제 카메라 영상보다 작다(하한). 검출은 사람 수와 거의 무관하게 묶음 단위로 돌지만,
추적·연결·판정 부하는 c5a(기하)로만 본다. 하네스는 노트북·EC2 없이 Pod 안 127.0.0.1 로 돌아 망 구간(I0 Direct TCP)은 재지 않는다.

**c5 수동 (10명 처리 시간, 녹화 재생):** 10명 이상이 보이는 A0 녹화(또는 같은 조건 영상)가 있을 때.
1. Pod 에서 Tier 0 를 LIVE 설정(`input.mode live`, `decoder nvdec`, 영상 포트 `SIMAI_TIER0_VIDEO_PORTS`)으로 띄우고, 노트북에서 `gateway/relay` 를
   녹화 파일 입력(`kind = "file"`)과 `--send` 로 붙인다(EC2 엔드포인트 조회·공개키 고정). 사람이 없는 영상으로 경로만 볼 때는 `session1_live.py serve` 를 쓴다.
2. 표시 파일 `state/tier0.live` 의 `status.live_input` 에서 `frame_age_ms_p95 ≤ 150 + 검출 시간`, health `pipeline_lag_ms` 를 10분 기록하고, 검출 4대 묶음 38 ms(명세 5.1 기준선)와 비교한다.
3. 결과는 `runs/live10/` 에 두고 MR·Jira 에 요약한다(제품 성능 지표로 쓰기 전 G0 조건 확인).

### d: Tier 0 GPU 우선 보호 측정 (`deploy/scripts/session1_gpu_priority.py`, -60 'Pod 측정' P0~P7)

Tier 0 는 c4 와 같은 LIVE 하네스로 띄우고(측정 동안 계속), `ai/experiments/gpu_queue/tier0_priority_probe.py` 의 `load`(더미 분석 부하, 상한 0.70)와
`watch`(표시 파일 `gpu_priority` 판정)로 잰다. 결과는 Run 폴더와 `$SIMAI_ROOT/runs/g0-tier0-priority/<UTC>/` 에 남는다.

부하 크기는 감지한 GPU 메모리(`nvidia-smi memory.total`) 비율이다(-205): H100 80GB(81,559 MiB)에서 아래 40·20·70 GB 그대로, 다른 GPU 는 같은 비율(1,000 MB 단위).
상한 0.70 은 비율 그대로다. 예: L40S 48GB(46,068 MiB) 23·11·40 GB, RTX 4090 24GB 는 Tier 0 전용 사양(48GB급 미만)이라 분석 부하를 같은 GPU 에 올리지 않으므로
P3·P5·P6 을 SKIP + 이유로 남긴다(선택 단계로 바뀌어 필수 실패로 세지 않음). GPU 메모리를 읽지 못하면 SKIP 이 아니라 FAIL 이다.

| # | 내용 | 통과 기준 | 1차 세션 시간 |
|---|---|---|---|
| P0 | `pytest tests/experiments/gpu_queue tests/gpu tests/services/worker/test_runner_tier0_priority.py` (Linux 전용 시험 포함) | 모두 통과 | 약 3분 |
| P1 | 표시 파일 `gpu_priority`, `nvidia-smi --query-compute-apps` 기록 | phase=running, reserve.state=OK, reserved_mb ≥ 8192 | 즉시 |
| P2 | 부하 없는 기준선 watch | passed (lag p95 ≤ 600 ms, unknown 0, 판정 오류 증가 0) | 120초 |
| P3 | 더미 부하 40 GB 동안 watch | passed, Tier 0 로그 `out of memory` 0건 | 120초 |
| P4 | VLM(vLLM) 부하 | SKIP(선택): 처리기가 아직 없다 | - |
| P5 | 더미 부하 20 GB 를 watch 30초째에 kill -9 | passed (D-G0-08 판정 지속) | 90초 |
| P6 | 상한(0.70 = 56 GB)을 넘는 70 GB 부하 | 부하는 메모리 부족으로 끝나고 watch 는 passed | 60초 |
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
| 볼륨 없을 때 저장 위치 `/srv/simai` | 이미지의 `/opt/simai`(코드)와 나누고, `/workspace` 는 이미지에 없어야 한다(check_image) |
| yolo26x-pose sha256 `08ed9e01…` | E-GATE-0 일본 볼륨의 `weights/hmr_weights.sha256`(ultralytics 릴리스 v8.4.0 에서 받은 파일) |

버린 대안: 가중치 받기를 GPU 확인보다 먼저(GPU 없는 곳에서 받기만 하고 실패), 목록에 해시가 없을 때 첫 다운로드를 믿고 기록(TOFU, '모르면 비움'과 어긋남),
queue 모드에서도 엔진 준비(세션 b 항목의 첫 빌드 시간 측정이 캐시 사용으로 바뀜), 컨테이너 안에서 HF 토큰 파일 읽기(엘리스는 `--env-file` 로 호스트 docker 가 읽게 해 경로가 하나뿐).

잔여 위험: Vast 의 공개키 변수(`SSH_PUBLIC_KEY`)는 Vast 문서의 환경 변수 목록으로만 확인했고 실제 인스턴스에서 보지 않았다. 엘리스 VM 의 드라이버·Container Toolkit·블록 스토리지 마운트는
첫 시연 준비 때 확인한다. Tier 0 는 공개 IP·포트를 `RUNPOD_*` 이름으로 읽어 Vast·엘리스에서는 사람이 넣는다. run_record `gpu.provider` 계약 값이 RUNPOD·LOCAL 뿐이라 Vast·엘리스 Run 은 LOCAL 로 기록된다.

## 첫 세션 수동 확인 목록 (큐 밖)

- SSH 키 접속과 비밀번호 접속 거부(deploy/README Pod 검증 큐 8).
- Pod 재시작 뒤 포트 변경과 위 재접속 절차, 같은 `--session` 이어 돌기.
- 443 예비: 웹 터미널 접속, 프록시 주소로 WebSocket 접속(토큰 필요)·100초 끊김.
- Network Volume 을 붙인 Pod 의 Stop 가능 여부(명세 19.1 미확인 항목).
- 끝나면 Pod Stop, 결과 묶음 sha256 을 manifest 와 대조.
