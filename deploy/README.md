# deploy: 모델별 Docker 이미지 (S15P31A101-32)

모델별 Python·CUDA·Torch 환경을 Docker 이미지로 고정하고, 이미지 다이제스트를 Run 기록의 `env_hash` 로 쓴다(명세 3.12 저장소 규칙, 20.5, 21.5).
저장 위치(`$SIMAI_ROOT`: 볼륨·블록 스토리지, 없으면 컨테이너 디스크)에는 가중치, 데이터, 실험 산출물, TensorRT 엔진 캐시만 둔다. 환경은 볼륨에 설치하지 않는다(E-GATE-0 은 예외였다).
GPU·지역은 고정하지 않는다(2026-10-20 결정, S15P31A101-205): 개발·시험은 RunPod 의 그때그때 남은 GPU·지역(볼륨 없음), 예비는 Vast.ai,
시연은 엘리스 클라우드 A100 VM + 블록 스토리지(`docker run`). 일본 Network Volume 은 쓰지 않는다.
최소 사양: Tier 0 만은 VRAM 24GB급(RTX 4090 이상), Tier 0 + 실험은 48GB(80GB 권장). Ampere 이후, 드라이버 최대 CUDA 13.0 이상, 볼륨이 없으면 컨테이너 디스크 100GB 이상.

## 파일

| 파일 | 하는 일 |
|---|---|
| `docker-bake.hcl` | 빌드 정의. 외부 이미지(CUDA·uv) 다이제스트 고정, 대상 `tier0`·`exp02`(베이스를 함께 빌드) |
| `base/Dockerfile`, `base/simai-start.sh` | 공통 베이스: CUDA 이미지 + uv + uv 관리 Python 3.11.15 + SSH(키 인증만: RunPod `PUBLIC_KEY`·Vast `SSH_PUBLIC_KEY`·`SIMAI_AUTHORIZED_KEYS_FILE`, `SIMAI_SSHD=off` 면 안 띄움. 한 줄에 공개키 하나만 받고 옵션·제어 문자 줄은 버린다. ssh-keygen·sshd 에는 HF 토큰을 넘기지 않는다) 시작 스크립트 |
| `tier0/Dockerfile`, `tier0/entrypoint.sh` | Tier 0 서비스 이미지(YOLO26x-pose, TensorRT FP16). `ai/uv.lock` 으로 의존성 고정, 첫 기동 때 엔진 준비 |
| `scripts/simai_bootstrap.py`, `weights.json` | 이미지의 `simai-bootstrap`(기본 진입점): 저장 위치·GPU 세대 감지 → 가중치 목록대로 받고 sha256 확인 → Tier 0 또는 세션 큐 (아래 "simai-bootstrap") |
| `exp02/Dockerfile`, `exp02/entrypoint.sh`, `exp02/requirements.{in,lock.txt}` | EXP-02 Fast SAM 3D Body 이미지(골격). 체크포인트는 볼륨에서 읽는다 |
| `*/Dockerfile.dockerignore` | 이미지별 빌드 컨텍스트 **허용 목록**(나열한 파일만 들어간다) |
| `compose.yaml` | 로컬 점검용(RunPod 은 Compose 를 쓰지 않는다) |
| `scripts/check_image.sh` | 이미지 점검(GPU 불필요): 비밀·가중치·엔진·SSH 호스트 키 없음, import, 진입점 오류 코드, GPU 별 엔진 경로, SSH 비밀번호 접속 거부 |
| `scripts/gpu_session1.py` | GPU 1차 세션 검증 큐(S15P31A101-183): gpu_queue 로 아래 스크립트를 차례로 돌리고 결과 묶음·scp 명령을 낸다. `prepare`(노트북 사본)·`plan`·`run`(Pod 또는 `--mode dryrun`) |
| `scripts/pod_check.py` | Pod 점검(-31): GPU(최소 사양)·드라이버·CUDA·마운트·용량·쓰기 속도·fsync·flock → JSON. `--no-volume` 은 볼륨 측정 SKIP |
| `scripts/session1_engine.sh` | 아래 "Pod 검증 큐" 1)~7) 을 한 Job 으로(단계별 종료 코드·시간을 steps.tsv·metrics.json 에) |
| `scripts/crash_recovery_g3.py` | Network Volume 강제 종료 복구 실측(-92): 실제 Tier 0 kill -9 ·재시작 + -51 결정적 지점, G3 항목 0건 판정 |
| `scripts/repeat_pytest.py` | pytest 반복(Linux TLS 시험 -45·-175) |
| `scripts/session1_live.py` | LIVE 입력 Pod 확인(-182): NVDEC 열림·gpu 시험·4스트림 디코딩 처리량·LIVE 기동 하네스·10명 기하 처리 시간 |
| `scripts/session1_gpu_priority.py` | Tier 0 GPU 우선 보호 Pod 측정(-60 P0~P7): LIVE 하네스 + `tier0_priority_probe` |
| `scripts/wait_tier0_ready.py` | 이미지의 `simai-wait-tier0`: 분석 워커를 Tier 0 표시 파일 `phase=running`·`reserve.state=OK` 뒤에 띄운다(아래 "분석 워커") |

Network Volume 구조·권한·보관, Pod 점검 기준, SSH 재접속·443 예비 경로, 세션 큐 항목·예상 시간은 `docs/design/runpod-environment.md` 에 있다.

엔진 캐시 코드는 `ai/src/simai_ai/detection/engine_cache.py`(시험 `ai/tests/detection/test_engine_cache.py`)다.

## 이미지에 넣지 않는 것

| 무엇 | 어디에 | 비고 |
|---|---|---|
| 가중치 (YOLO26x-pose `.pt`, sam-3d-body-dinov3 등) | `$SIMAI_ROOT/weights/` | 해시 목록은 저장소 `deploy/weights.json`(3.12). `simai-bootstrap` 이 없으면 받고 다르면 시작하지 않는다. Tier 0 는 `SIMAI_TIER0_WEIGHTS_SHA256`(부트스트랩이 목록 값으로 채움)과 다르면 시작하지 않는다 |
| TensorRT 엔진 | `$SIMAI_ROOT/engines/` | 그 GPU 에서 첫 기동 때 만든다(아래). 세대별 경로(`sm_XX`) |
| 데이터·녹화·2D 관측·Run 산출물 | `$SIMAI_ROOT/{datasets,recordings,obs2d,runs}` | 19.1 볼륨 구조 |
| HF 토큰 | 환경 변수 `HF_TOKEN` 만: RunPod Secret `{{ RUNPOD_SECRET_hf_token }}`, Vast 계정 환경 변수, 엘리스는 블록 스토리지 `.simai-private/hf.env`(chmod 600)를 `docker run --env-file` 로 | `simai-bootstrap` 이 가중치를 받을 때만 쓰고 Tier 0·큐를 띄우기 전에 환경에서 지운다. 이미지·저장소·로그·결과 묶음에 남기지 않는다 |
| 비밀 (Tier 0 TLS 개인키, 기기·워커 토큰) | `$SIMAI_ROOT/../.simai-private/` | 이미지·저장소·템플릿 환경 변수에 넣지 않는다. 볼륨은 chmod 가 안 먹으므로 다른 Pod 에 붙일 때 주의 |
| SSH 호스트 키 | 컨테이너 시작 때 `ssh-keygen -A` | 이미지에 남으면 모든 Pod 가 같은 호스트 키를 쓴다 |

빌드 컨텍스트는 저장소 최상위지만 `Dockerfile.dockerignore` 가 허용 목록이라 `.env`, `*.pt`, `recordings/` 같은 파일은 컨텍스트에 들어갈 수 없다.
`scripts/check_image.sh` 가 결과 이미지에서 다시 확인한다.

## 버전 고정

| 대상 | 고정 방법 |
|---|---|
| CUDA·uv 이미지 | `docker-bake.hcl` 의 태그 + `@sha256:` 다이제스트 |
| Python | uv 관리 CPython 3.11.15 (우분투 기본 Python 을 쓰지 않음) |
| Tier 0 Python 의존성 | `ai/uv.lock` 을 `uv sync --locked --no-dev --extra tier0` 로. pyproject 와 잠금이 어긋나면 **빌드가 실패**한다 |
| EXP-02 Python 의존성 | `exp02/requirements.lock.txt`(해시 포함). 잠금 밖 3개(chumpy·Detectron2·MoGe)는 커밋 고정 + `--no-deps` |
| 이미지 자체 | 레지스트리에 올린 이미지의 다이제스트. RunPod 템플릿은 태그가 아니라 다이제스트로 이미지를 가리킨다 |

**Tier 0 가 CUDA 13 인 이유.** `ai/uv.lock` 의 torch 2.14.1(ultralytics 의존성)은 리눅스에서 CUDA 13.0 런타임 휠(`nvidia-cuda-runtime` 13.0.96)을 직접 가져온다.
그래서 베이스는 같은 판의 `nvidia/cuda:13.0.3-base-ubuntu24.04` 이고 TensorRT 도 `tensorrt-cu13` 이다. CUDA 라이브러리는 휠이 가져오므로 `runtime` 이 아니라 `base` 변형을 쓴다(중복 수 GB 절약).
CUDA 13.0 은 드라이버 580 이상이 필요하다. 이 베이스에는 순방향 호환 라이브러리가 있어 데이터센터 GPU(H100)는 535·550·565·570·575 계열 드라이버에서도 돌 수 있다(`NVIDIA_REQUIRE_CUDA`).
RunPod 에서 Pod 를 고를 때 CUDA 버전 필터를 13.0 이상으로 두는 것이 가장 확실하다(Pod 검증 큐 1번에서 확인).

**TensorRT.** `tensorrt-cu13` 의 libs 휠(약 3.8GB)은 pypi.nvidia.com 에만 있어 `ai/pyproject.toml` 에 그 인덱스를 `explicit` 으로 두고 libs 패키지만 그 인덱스에서 받는다.
TensorRT·onnx 는 `tier0` extra 에 `sys_platform == 'linux'` 로만 들어가 Windows 노트북 개발 환경에는 설치되지 않는다.

## 빌드 (노트북, GPU 불필요)

RunPod Pod 안에서는 Docker 를 쓸 수 없으므로 이미지는 노트북에서 빌드해 레지스트리에 올린다. 저장소 최상위에서:

```sh
TAG=$(git rev-parse --short=12 HEAD)
TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl tier0      # → simai/tier0:$TAG (로컬)
bash deploy/scripts/check_image.sh simai/tier0:$TAG               # 모두 통과해야 올린다
TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl exp02      # → simai/exp02-fs3db:$TAG (Detectron2 CPU 빌드, 수십 분)
```

- Tier 0 이미지는 레지스트리 기준(압축) 약 7.4GB, 풀면 약 19.5GB 다(torch CUDA 13 휠 묶음과 TensorRT libs 가 대부분. 2026-10-18 노트북 빌드 실측). 처음 빌드는 받기만 수 분, 이후에는 uv 캐시 마운트로 소스 층만 다시 만든다.
- Docker Hub 받기가 `httpReadSeeker ... EOF` 로 끊기면(SSAFY 망에서 자주 봤다) 같은 명령을 다시 실행한다. 받은 층은 남는다.
- `git status` 가 깨끗할 때만 올릴 이미지를 빌드한다(`TAG` 가 커밋을 가리키므로).
- EXP-02 의 Detectron2 아키텍처는 `EXP02_CUDA_ARCH=9.0`(H100) 기본이다. 다른 GPU 면 이 값을 바꿔 다시 빌드한다.
- EXP-02 는 Detectron2 CUDA 확장을 CPU 로 컴파일하므로 Docker Desktop 메모리를 8GB 이상으로 올리고 빌드한다(메모리 부족이면 bake 에 `--set exp02.args.MAX_JOBS=2`). 아직 노트북에서 빌드해 보지 않았다(Pod 검증 큐 앞 단계).

정적 점검: `docker run --rm -i hadolint/hadolint:v2.14.0 < deploy/tier0/Dockerfile` (세 Dockerfile 모두), 셸 스크립트는 shellcheck. 셸 스크립트는 LF 다(`.gitattributes`).

## 레지스트리 푸시 (사람이 확인한 뒤에만)

푸시는 외부로 내보내는 작업이라 사용자 확인 뒤에 한다. 레지스트리는 비공개여야 한다(SSAFY GitLab 컨테이너 레지스트리가 열려 있으면 그것, 아니면 Docker Hub 비공개 저장소. 결정 필요).

```sh
docker login <레지스트리>                                         # 토큰은 docker 자격 증명 저장소에만
REGISTRY=<레지스트리>/<프로젝트> TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl --push tier0
docker buildx imagetools inspect <레지스트리>/<프로젝트>/tier0:$TAG --format '{{.Manifest.Digest}}'
#  → sha256:...  이 값이 이미지 다이제스트다. RunPod 템플릿 이미지는 <레지스트리>/<프로젝트>/tier0@sha256:... 로 쓴다
```

RunPod 에는 레지스트리 자격 증명(읽기 전용 토큰)을 RunPod 계정의 Container Registry Credentials 로 등록한다. 템플릿 환경 변수에 넣지 않는다.

## env_hash (Run 기록)

`run_record.env_hash` 는 레지스트리에 올라간 이미지의 다이제스트(`sha256:...`)다(contracts/experiments/run_record.schema.json, 명세 21.5).

1. 위 푸시 절차의 `imagetools inspect` 로 다이제스트를 얻는다. 로컬 이미지 ID(`docker images` 의 IMAGE ID)는 다이제스트가 아니므로 쓰지 않는다.
2. RunPod 템플릿 이미지를 `...@sha256:<다이제스트>` 로 지정하고, 같은 값을 템플릿 환경 변수 `SIMAI_IMAGE_DIGEST` 에도 넣는다(컨테이너 안에서는 자기 다이제스트를 알 수 없다).
3. GPU 큐 Job 파일의 `record.env.image_digest` 에 같은 값을 적는다. `run_record.py` 는 이미지 다이제스트만 있으면 그 값을 그대로 `env_hash` 로 쓴다(`ai/experiments/gpu_queue/README.md`).
4. 다이제스트를 모르는 Run(로컬 이미지, 볼륨 환경)은 `env_hash` 를 다이제스트로 채우지 않는다. 볼륨 환경은 기존처럼 잠금 파일 해시(`lock_files`)를 쓴다.

## simai-bootstrap (이미지 기본 진입점, S15P31A101-205)

어느 GPU·지역·사이트에서든 같은 순서로 시작한다. 진입점은 `tini → simai-start(SSH) → simai-bootstrap` 이고 기본 인자는 `tier0` 이다.

| 명령 | 하는 일 |
|---|---|
| (인자 없음) / `tier0 [Tier 0 인자]` / `--config ...` | 준비 → `simai-tier0`(엔진 준비 `engine_cache ensure` → `python -m services.tier0`) |
| `queue [gpu_session1 run 인자]` | 준비 → GPU 1차 세션 큐(저장소 사본 `$SIMAI_SESSION_REPO`, 기본 `$SIMAI_ROOT/setup/session1/repo`). 엔진 첫 빌드 시간은 큐 b 항목이 재므로 엔진 준비는 하지 않는다 |
| `check` | 준비만 하고 결과 JSON 출력(사이트 첫 확인) |
| 그 밖의 명령(`sleep infinity`, `bash`, `simai-wait-tier0`, `python ...`) | 준비 없이 그대로 실행 |

준비 순서와 종료 코드(simai-tier0·engine_cache 와 같은 규칙: 2 저장·가중치, 3 GPU):

1. 저장 위치: `SIMAI_ROOT` → `/workspace` 가 있고 쓸 수 있으면 `/workspace/simai`(RunPod 볼륨·Pod 볼륨·`docker run -v`) → 컨테이너 디스크 `/srv/simai`. 남은 용량 하한 `SIMAI_MIN_FREE_GB`(기본 20) 미만이면 2.
2. GPU: `nvidia-smi` compute_cap → `sm_XX`(`SIMAI_GPU_ARCH` 로 넘김), 메모리·드라이버 기록. GPU 없음, compute_cap·메모리를 읽지 못함, Ampere(8.0) 미만, VRAM 24GB급(22,500 MiB) 미만이면 3. 가중치를 받기 전에 끝난다.
3. 가중치: `weights.json`(이미지 `/opt/simai/deploy/weights.json`)의 `tier0` 항목. 있고 sha256 이 맞으면 건너뜀(엘리스 블록 스토리지 재사용), 없으면 옆 임시 파일로 받아 sha256 이 맞을 때만 이름을 바꾼다. 있는데 다르면 지우지 않고 2, 받은 것이 다르면 버리고 2, 목록에 해시가 없으면 2. HF 원본(`source.hf`)은 `HF_TOKEN` 을 첫 요청에만 싣는다(다른 호스트 리다이렉트에는 안 실림). 해시 근거는 `docs/data/weights.md`. `SIMAI_TIER0_WEIGHTS`·`SIMAI_TIER0_WEIGHTS_SHA256` 이 비어 있으면 목록 값으로 채운다.
4. 기록 `$SIMAI_ROOT/runs/bootstrap/<UTC>-<pid>.json`(추가만, 토큰 값 없음, `hf_token: present|absent` 만) → exec.

HF 토큰은 시작하자마자 환경에서 꺼내 부트스트랩 프로세스 안에만 둔다(가중치 받기에만 씀). nvidia-smi 조회·도움말·그대로 넘기는 명령·Tier 0·큐 어느 자식에도 넘어가지 않는다. `simai-start` 도 ssh-keygen·sshd 를 토큰 없는 환경으로 띄운다. `HUGGING_FACE_HUB_TOKEN` 은 읽지 않고 지울 이름으로만 다룬다.

HMR(torch 2.5.1 cu124, Detectron2)·MapAnything(torch 2.7.1 cu126) 가중치는 `weights.json` 의 `placeholders` 에 자리만 있다(실험 이미지 티켓에서 채운다).

## RunPod 템플릿 (Tier 0, 볼륨 없음)

| 항목 | 값 |
|---|---|
| 데이터센터·GPU | 그때그때 남은 지역·GPU. 최소 Tier 0 만 24GB급(RTX 4090 이상), Tier 0 + 실험 48GB(80GB 권장), Ampere 이후. GPU 하나(Tier 0 가 먼저 쓴다) |
| 이미지 | `<레지스트리>/<프로젝트>/tier0@sha256:<다이제스트>` |
| CUDA 버전 필터 | 13.0 이상(위 "CUDA 13 인 이유") |
| Container Disk | 100GB 이상(이미지 풀면 약 19.5GB + 가중치·엔진·결과. 볼륨이 없으므로 여기가 `$SIMAI_ROOT=/srv/simai`) |
| Volume | 붙이지 않는다(Network Volume 은 데이터센터에 묶인다). Pod 볼륨을 쓰면 `/workspace/simai` 가 된다 |
| 공개 TCP 포트 | 22(SSH, 키 인증만), I0 수신 포트(70000 이상 대칭 매핑, 실제 포트는 `RUNPOD_TCP_PORT_*`, 명세 3.12) |
| 시작 명령 | 비움(이미지 기본: `simai-bootstrap tier0`). 검증 세션은 `sleep infinity` 로 띄우고 SSH 로 `simai-bootstrap queue` |
| GPU 기능 | 이미지 `NVIDIA_DRIVER_CAPABILITIES=compute,utility,video`(video 는 LIVE 입력 NVDEC, -182). 템플릿에서 덮어쓰지 않는다 |
| 환경 변수 | `HF_TOKEN={{ RUNPOD_SECRET_hf_token }}`(RunPod Secret, 값은 템플릿에 쓰지 않음), `SIMAI_TIER0_GPU_RESERVE_MB=8192`(Tier 0 GPU 선확보, -60, G0 실측 전 초기값), `SIMAI_IMAGE_DIGEST=sha256:...`. `SIMAI_ROOT`·가중치 경로·해시·엔진 캐시는 부트스트랩이 정한다(`SIMAI_ENGINE_CACHE` 기본 `$SIMAI_ROOT/engines`, `SIMAI_OBS2D_ROOT` 는 필요하면 `$SIMAI_ROOT/obs2d`). 선택: `SIMAI_TIER0_MODE`(e2e), `SIMAI_TIER0_IMGSZ`(1280), `SIMAI_TIER0_MAX_BATCH`(4), `SIMAI_TIER0_CAMERA`(2304x1296) |
| 분석 워커 환경 변수 | 같은 Pod 에서 워커를 띄울 때(아래 "분석 워커"): `SIMAI_EC2_BASE_URL`, `SIMAI_WORKER_ID=worker-01`, `SIMAI_WORKER_GPU_MEMORY_FRACTION=0.70`, `SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION=0.55`, `SIMAI_WORKER_VLLM_MAX_NUM_SEQS=4`, `SIMAI_GPU_TIER0_FRACTION=0.20`, `SIMAI_WORKER_LAG_BUDGET_MS=600`. 선택(기본값): `SIMAI_WORKER_MARKER_MAX_AGE_S`(15), `SIMAI_WORKER_RESUME_AFTER_S`(30), `SIMAI_WORKER_LIVE_BLOCKED_TYPES`(static_reconstruct,hmr_refine,hmr_appearance_update), `SIMAI_WORKER_LIVE_WAIT_MS`(5000), `SIMAI_WORKER_HOLD_MAX_S`(60), `SIMAI_TIER0_MARKER`. 값의 뜻은 docs/design/tier0-gpu-priority.md |
| 비밀 | 템플릿에 값으로 넣지 않는다(HF 토큰만 Secret 참조). 워커 토큰은 `.simai-private/worker.env` 의 `SIMAI_WORKER_TOKEN=`. `PUBLIC_KEY` 는 RunPod 이 계정 SSH 키로 채운다 |

결과는 컨테이너 디스크에 있으므로 Pod 를 지우기(Terminate) 전에 scp 로 가져온다(세션 큐가 명령을 출력한다). Stop 만 해도 같은 GPU 를 다시 못 잡을 수 있다.

### Vast.ai (예비)

Docker 실행 방식은 **Entrypoint** 로 둔다(SSH·Jupyter 방식은 이미지 진입점을 Vast 스크립트로 바꿔 `simai-start`·`simai-bootstrap` 이 돌지 않는다).
`SSH_PUBLIC_KEY`(계정 키, Vast 가 넣는다)로 `simai-start` 가 sshd 를 띄운다. 공개 주소·포트는 `PUBLIC_IPADDR`·`VAST_TCP_PORT_22`.
`HF_TOKEN` 은 Vast 계정 환경 변수로, 디스크는 100GB 이상, CUDA 13.0 이상 필터. 볼륨이 없으면 `/srv/simai` 다.

## GPU VM(엘리스) docker run

엘리스 클라우드 A100 VM + 블록 스토리지(시연). VM 의 호스트 sshd 로 들어가므로 컨테이너 sshd 는 끈다(`SIMAI_SSHD=off`).

```sh
# 0) 호스트 확인: 드라이버(최대 CUDA 13.0 이상)·Container Toolkit
nvidia-smi --query-gpu=name,driver_version,compute_cap,memory.total --format=csv
nvidia-smi | grep "CUDA Version"
nvidia-ctk --version && docker info --format '{{json .Runtimes}}' | grep -q nvidia && echo "toolkit ok"
docker run --rm --gpus all --entrypoint nvidia-smi <레지스트리>/<프로젝트>/tier0@sha256:<다이제스트> -L   # 컨테이너에서 GPU 가 보이는지
# 1) 블록 스토리지를 호스트에 마운트(예: /mnt/simai-block). 컨테이너에는 /workspace 로 붙이고 SIMAI_ROOT 로 명시한다
#    HF 토큰은 블록 스토리지 .simai-private/hf.env (HF_TOKEN=... 한 줄, chmod 600). 저장소·이미지·명령 기록에 쓰지 않는다
sudo mkdir -p /mnt/simai-block/.simai-private && sudo chmod 700 /mnt/simai-block/.simai-private
# 2) 준비만 확인 (GPU sm_80, 가중치 받기·해시, 저장 위치 /workspace/simai)
docker run --rm --gpus all -v /mnt/simai-block:/workspace -e SIMAI_ROOT=/workspace/simai -e SIMAI_SSHD=off \
  --env-file /mnt/simai-block/.simai-private/hf.env <이미지> check
# 3) Tier 0 기동: I0 수신 포트(제어·영상)를 같은 번호로 연다. Tier 0 는 RunPod 이름의 변수로 공개 IP·포트를 읽는다(명세 3.12)
docker run -d --name simai-tier0 --restart unless-stopped --gpus all \
  -v /mnt/simai-block:/workspace -e SIMAI_ROOT=/workspace/simai -e SIMAI_SSHD=off \
  --env-file /mnt/simai-block/.simai-private/hf.env \
  -p <제어 포트>:<제어 포트> -p <영상 포트들>:<영상 포트들> \
  -e RUNPOD_PUBLIC_IP=<VM 공인 IP> -e SIMAI_TIER0_CONTROL_PORT=70000 -e RUNPOD_TCP_PORT_70000=<제어 포트> \
  -e SIMAI_TIER0_GPU_RESERVE_MB=8192 -e SIMAI_IMAGE_DIGEST=sha256:<다이제스트> \
  <이미지> --config /workspace/simai/tier0.json
docker logs -f simai-tier0      # simai-bootstrap: 저장·GPU·가중치 한 줄 → simai-tier0: 엔진 → Tier 0
```

- 블록 스토리지에 가중치·엔진이 이미 있으면 받지 않고(해시만 확인) 엔진 캐시를 쓴다. VM 을 바꿔도 같은 블록 스토리지를 붙이면 된다(A100 은 `sm_80` 경로).
- `--env-file` 은 호스트 docker 가 읽어 컨테이너 환경에만 넣는다. 부트스트랩이 가중치를 받은 뒤 Tier 0 를 띄우기 전에 지운다.
- TLS 키·기기 토큰 파일은 `/mnt/simai-block/.simai-private/` 에 두고 `SIMAI_TIER0_TLS_*_FILE=/workspace/.simai-private/...` 로 가리킨다.
- `RUNPOD_*` 이름은 Tier 0 설정이 RunPod 기준으로 읽기 때문이다(엔드포인트 등록). 이름을 일반화하는 것은 별도 티켓이다.

### 분석 워커 (같은 Pod, Tier 0 뒤)

GPU Pod 는 하나이고 Tier 0 가 GPU 를 먼저 쓴다(명세 3.2, S15P31A101-60). 진입점이 TensorRT 엔진을 준비하는 동안(첫 빌드는 수 분)에는
Tier 0 표시 파일 `state/tier0.live` 가 아직 없어, 분석 워커 접수 문이 '관제 없음'으로 보고 GPU 메모리를 먼저 잡을 수 있다.
그래서 워커는 `simai-wait-tier0` 로 띄운다. 표시 파일의 `gpu_priority` 가 `phase=running`·`reserve.state=OK` 이고 15초 안에 갱신된 것을
본 뒤에만 워커를 exec 하고, 모르면(파일 없음·기동 중·선확보 PARTIAL·오래됨) 띄우지 않고 기다린다(기본 30분 뒤 종료 코드 3).

```sh
# Pod 안 (SSH). Tier 0 는 템플릿 시작 명령(이미지 기본)으로 이미 떠 있다
nohup simai-wait-tier0 --env-file /workspace/.simai-private/worker.env -- python -m services.worker > /workspace/simai/logs/worker.log 2>&1 &
# 관제가 없는 개발 시간(Tier 0 없음): 표시 파일이 없을 때만 띄운다
simai-wait-tier0 --no-tier0 --env-file /workspace/.simai-private/worker.env -- python -m services.worker
```

진입점이 엔진 준비 전에 기동 중 표시 파일을 쓰는 안은 쓰지 않았다: 표시 파일은 Tier 0 서비스가 단독으로 쓰고(기동 첫 단계 `phase=starting`,
비정상 종료 감지 `start_failed`), 진입점이 같은 파일을 쓰면 다음 시작이 '이전 실행이 정상 종료하지 않음'으로 잘못 본다.
Tier 0 재시작(엔진 캐시 사용, 1초대) 때는 워커 접수 문이 표시 파일이 사라진 뒤 60초를 재시작 틈으로 보고 받지 않는다(-60).
`compose.yaml` 의 `worker` 서비스가 같은 순서의 로컬 점검용이다.

EXP-02 는 같은 볼륨에 `exp02-fs3db@sha256:...` 이미지로 Pod 를 띄우고(Tier 0 와 동시에 띄우지 않는다, LIVE 관제 시간 금지) 시작 명령에 GPU 큐를 준다.

## TensorRT 엔진 캐시

진입점 `simai-tier0` 이 Tier 0 시작 전에 별도 프로세스로 `python -m simai_ai.detection.engine_cache ensure` 를 부른다.

```
/workspace/simai/engines/yolo26x-pose/sm_90/trt11.4.0.106/
  yolo26x-pose-<가중치 sha256 앞 12자>_e2e_i1280_b4_c2304x1296_fp16_ul8.4.178.engine
  yolo26x-pose-<...>.json      키 전부(가중치 해시, GPU 아키텍처, TensorRT·ultralytics 버전, 입력 설정, 프로파일) + 엔진 sha256 + 빌드 GPU 이름·시각
```

- 메타데이터의 키가 지금 것과 모두 같고 엔진 해시가 맞을 때만 쓴다. 다른 GPU(예: 예전 A40 sm_86)의 엔진은 경로부터 달라 고르지 않는다.
- 같은 경로에 키가 다른 파일(손으로 복사 등)이 있으면 쓰지 않고 `*.conflict-<ms>` 로 옆에 옮긴 뒤(지우지 않음) 다시 만든다. 메타데이터가 없는 엔진(빌드 도중 종료)도 쓰지 않는다.
- 엔진 경로는 Tier 0 프로세스에 `SIMAI_TIER0_ENGINE` 환경 변수로 넘긴다. Tier 0 설정(`services/tier0/settings.py`)이 이 값을 읽고 LIVE 입력의 검출기(`yolo_detector`)가 연다. 실행 중 상태는 `$SIMAI_ROOT/state/tier0.live`(정상 종료하면 지운다)에 5초마다 쓴다.
- 종료 코드: 2 볼륨·가중치 없음·해시 불일치, 3 GPU·TensorRT 없음, 4 캐시 위치를 쓸 수 없음, 5 빌드 실패. 실패하면 Tier 0 를 띄우지 않는다.
- 동적 프로파일은 `detection_baseline` 과 같다(min 1×3×32×32, opt 4×3×736×1280, max 4×3×1280×1280). 빌드는 `simplify=False`(onnxslim·onnxruntime 을 이미지에 넣지 않음).

## EXP-02 이미지 (골격)

`ai/experiments/hmr/setup_runpod.sh` 의 볼륨 환경을 옮겼다. 차이:

- conda(micromamba) 대신 uv. CUDA toolkit 은 `nvidia/cuda:12.4.1-cudnn-devel` 베이스의 nvcc 를 쓴다.
- MoGe @74fbce0 는 `--no-deps` 로 넣고 의존성 중 flex-gemm(triton>=3.2 요구, torch 2.5.1 과 충돌)·gradio(데모 앱)를 뺐다. 볼륨 환경도 MoGe 뒤에 torch 를 강제로 되돌려 같은 충돌을 안고 있었다.
- Fast-SAM-3D-Body @d72aa36 코드는 이미지 안(`/opt/exp02/Fast-SAM-3D-Body`)에 두고 `SIMAI_FS3DB_REPO` 로 가리킨다(`fs3db_common.py` 는 없으면 볼륨 위치를 쓴다).
- 진입점은 체크포인트(`weights/sam-3d-body-dinov3/model.ckpt`, `...-fastcfg/model_config.yaml`, `weights/yolo/yolo26x-pose.pt`)가 없으면 종료 코드 2 로 멈춘다.

잠금 갱신(`requirements.in` 을 고쳤을 때, `deploy/exp02` 에서):

```sh
uv pip compile requirements.in --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes -o requirements.lock.txt
```

## Pod 검증 큐

GPU Pod 는 이 큐를 돌릴 때만 켜고 끝나면 바로 Stop 한다. LIVE 관제 시간에는 돌리지 않는다. 앞 단계가 실패하면 멈추고 결과를 기록한다.
아래 Tier 0 이미지 1)~7) 은 `scripts/session1_engine.sh` 가 한 Job 으로 묶었고, GPU 1차 세션 큐(`scripts/gpu_session1.py`, docs/design/runpod-environment.md)의 b 항목으로 돈다. 8)·9) 는 사람이 한다.
노트북 준비: 이미지 빌드 → `check_image.sh` 통과 → 사용자 확인 뒤 푸시 → 다이제스트로 템플릿 작성. 가중치는 `simai-bootstrap` 이 `weights.json` 대로 받는다.

Tier 0 이미지 (그때그때 남은 GPU, 볼륨 없음, 시작 명령 `sleep infinity` 로 띄우고 SSH 로 들어가 실행. 아래 `/workspace/simai` 는 `simai-bootstrap check` 가 출력한 `storage.root`(볼륨 없으면 `/srv/simai`)로 바꿔 읽는다. 세션 큐는 `simai-bootstrap queue` 가 그 값을 넘긴다):

```sh
# 1) GPU·드라이버·CUDA 호환 (드라이버 580 미만이면 순방향 호환이 동작하는지 2) 에서 판정)
nvidia-smi --query-gpu=name,driver_version,compute_cap,memory.total --format=csv
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_capability(0))"
python -c "import tensorrt as t; print(t.__version__)"
# 2) 엔진 첫 빌드 (H100 에서 수 분 예상) → 경로·메타데이터·빌드 시간 기록
time python -m simai_ai.detection.engine_cache ensure --weights /workspace/simai/weights/yolo/yolo26x-pose.pt \
  --cache-root /workspace/simai/engines --expect-sha256 "$SIMAI_TIER0_WEIGHTS_SHA256"
cat /workspace/simai/engines/yolo26x-pose/sm_90/trt*/*.json
# 3) 다시 실행하면 빌드 없이 캐시를 쓴다 ("엔진 캐시 사용", 1초대)
time python -m simai_ai.detection.engine_cache ensure --weights /workspace/simai/weights/yolo/yolo26x-pose.pt --cache-root /workspace/simai/engines
# 4) 엔진으로 추론 1회 (batch 4, 2304x1296) — 엔진이 실제로 열리고 사람이 잡히는지
python - <<'PY'
import os, numpy as np
from ultralytics import YOLO
eng = sorted(__import__("glob").glob("/workspace/simai/engines/yolo26x-pose/sm_90/trt*/*.engine"))[-1]
m = YOLO(eng, task="pose"); frames = [np.zeros((1296, 2304, 3), np.uint8)] * 4
r = m.predict(frames, imgsz=1280, half=True, verbose=False); print(eng, len(r), r[0].orig_shape)
PY
# 5) 진입점 전체 (엔진 준비 로그가 먼저 나와야 한다. -182 머지 전에는 LIVE 디코더가 없어 종료 코드 4, 머지 뒤 LIVE 확인은 세션 큐 c 항목)
simai-tier0 --config /workspace/simai/tier0.json; echo "exit=$?"
# 6) 반례: 가중치 해시 불일치·가중치 없음은 GPU 를 쓰지 않고 종료 코드 2
SIMAI_TIER0_WEIGHTS_SHA256=$(printf '0%.0s' {1..64}) simai-tier0; echo "exit=$? (2 기대)"
SIMAI_TIER0_WEIGHTS=/workspace/simai/weights/none.pt simai-tier0; echo "exit=$? (2 기대)"
# 7) 다른 GPU 캐시 무시 확인: sm_86 경로에 가짜 캐시가 있어도 sm_90 을 고른다
python -m simai_ai.detection.engine_cache plan --weights /workspace/simai/weights/yolo/yolo26x-pose.pt \
  --cache-root /workspace/simai/engines --gpu-arch sm_86 --tensorrt "$(python -c 'import tensorrt as t; print(t.__version__)')" --ultralytics 8.4.178
# 8) SSH: 노트북에서 ssh -p <22 매핑 포트> root@<공개 IP> 로 키 접속, 비밀번호 접속은 거부되는지
# 9) 끝: 결과(엔진 json, 시간, nvidia-smi)를 노트북으로 가져오고 Pod Stop
```

EXP-02 이미지 (Tier 0 Pod 를 Stop 한 뒤, 같은 볼륨):

```sh
# 먼저 노트북에서 ai/experiments/hmr/ 를 볼륨 /workspace/simai/setup/hmr/ 로 복사한다(SIMAI_FS3DB_REPO 를 읽는 fs3db_common.py)
SIMAI_EXP02_SKIP_WEIGHTS_CHECK=1 simai-exp02 python -c "import torch, detectron2, moge, ultralytics, pyrender, smplx; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_capability(0))"
python -c "from moge.model.v2 import MoGeModel; print('moge v2 ok')"          # flex-gemm 없이 MoGe2 를 쓸 수 있는지
python -c "import detectron2._C" && cat /opt/exp02/.detectron2_arch               # CUDA 확장과 빌드 아키텍처(9.0)
diff <(/opt/exp02/.venv/bin/python -m pip freeze 2>/dev/null || uv pip freeze --python /opt/exp02/.venv/bin/python) /workspace/simai/envs/fast_sam3db.lock.txt | head -50   # 볼륨 환경과 버전 차이
python /workspace/simai/setup/hmr/run_image.py --run-id H0_smoke_image --images /workspace/simai/datasets/hmr_poc14/t1_front_taichi.jpg --mode official --preset opt
```

E-GATE-0 H1 결과와 수치가 맞는지 비교한 뒤, 이후 EXP-02 Run 은 이 이미지 다이제스트를 `env_hash` 로 기록한다.
