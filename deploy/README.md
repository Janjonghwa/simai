# deploy: 모델별 Docker 이미지 (S15P31A101-32)

모델별 Python·CUDA·Torch 환경을 Docker 이미지로 고정하고, 이미지 다이제스트를 Run 기록의 `env_hash` 로 쓴다(명세 3.12 저장소 규칙, 20.5, 21.5).
저장 위치(`$SIMAI_ROOT`: 볼륨·블록 스토리지, 없으면 컨테이너 디스크)에는 가중치, 데이터, 실험 산출물, TensorRT 엔진 캐시만 둔다. 환경은 볼륨에 설치하지 않는다(E-GATE-0 은 예외였다).
GPU·지역은 고정하지 않는다(2026-10-20 결정, S15P31A101-205): 개발·시험은 RunPod 의 그때그때 남은 GPU·지역(볼륨 없음), 예비는 Vast.ai,
시연 사이트는 미정(엘리스 등 후보). 사이트가 GPU VM + 블록 스토리지면 `docker run` 으로 띄운다. 일본 Network Volume 은 쓰지 않는다.
최소 사양: Tier 0 만은 VRAM 24GB급(RTX 4090 이상), Tier 0 + 실험은 48GB(80GB 권장). Ampere 이후, 드라이버 최대 CUDA 13.0 이상, 볼륨이 없으면 컨테이너 디스크 100GB 이상.
Blackwell(sm_100·sm_120)도 세 이미지 모두 커널이 있다(Tier 0 cu130, 실험 이미지 cu128 torch 휠, S15P31A101-207. Detectron2 는 CUDA 확장 없는 CPU 전용 빌드라 GPU 세대와 상관없다, S15P31A101-238). 그 GPU 에서 실제로 도는지는 2차 세션 b·g0·m0 로 확인한다. 통합 이미지의 vLLM 은 cc 12.x(sm_120, RTX PRO 6000·AWS g7e)에서 FlashAttention·FlashInfer 커널이 없어 부트스트랩이 Triton attention·SDPA 로 바꿔 띄운다(아래 "통합 이미지" 3, `docs/design/S15P31A101-720-blackwell-sm120.md`). 드라이버는 R580 이상으로 고른다(CUDA 13 순방향 호환 라이브러리가 이 GPU 에서 되는지 확인하지 못했다).

## 파일

| 파일 | 하는 일 |
|---|---|
| `docker-bake.hcl` | 빌드 정의. 외부 이미지(CUDA·uv) 다이제스트 고정, 대상 `tier0`·`exp02`·`mapanything`(베이스를 함께 빌드, 묶음 `exp` = 실험 이미지 둘) |
| `base/Dockerfile`, `base/simai-start.sh` | 공통 베이스: CUDA 이미지 + uv + uv 관리 Python 3.11.15 + SSH(키 인증만: RunPod `PUBLIC_KEY`·Vast `SSH_PUBLIC_KEY`·`SIMAI_AUTHORIZED_KEYS_FILE`, `SIMAI_SSHD=off` 면 안 띄움. 한 줄에 공개키 하나만 받고 옵션·제어 문자 줄은 버린다. ssh-keygen·sshd 에는 HF 토큰을 넘기지 않는다) 시작 스크립트. sshd 를 띄우면 SSH 세션용 비밀 아닌 환경 변수 파일(`/etc/profile.d/simai-env.sh`)을 적고, 경로 비밀값 `SIMAI_TUNNEL_PREFIX` 가 있으면 막힌 망 최후 예비 SSH 용 wstunnel 서버를 띄운다(아래 "막힌 망 최후 예비 SSH", S15P31A101-286) |
| `tier0/Dockerfile`, `tier0/entrypoint.sh` | 타깃 `tier0`: Tier 0 서비스 이미지(YOLO26x-pose, TensorRT FP16). `ai/uv.lock` 으로 의존성 고정(큰 휠은 묶음별 층, 아래 "층 분할"), 첫 기동 때 엔진 준비. 타깃 `unified`: 같은 층 위에 vLLM·HMR 을 더한 통합 이미지(아래 "통합 이미지", S15P31A101-246) |
| `scripts/simai_bootstrap.py`, `weights.json` | 이미지의 `simai-bootstrap`(기본 진입점): 저장 위치·GPU 세대 감지 → 가중치 목록대로 받고 sha256 확인(재시도·이어받기·워커 가중치 검증 스탬프) → Tier 0 또는 세션 큐. `SIMAI_SERVICES` 면 Tier 0·vLLM·워커 감시 프로세스 (아래 "simai-bootstrap"·"통합 이미지") |
| `exp02/Dockerfile`, `exp02/entrypoint.sh`, `exp02/requirements.{in,lock.txt}` | EXP-02 HMR(Fast SAM 3D Body) 이미지: torch 2.7.1+cu128, Detectron2 는 CUDA 확장 없이(`FORCE_CUDA=0`, CPU 전용) 빌드. 가중치는 시작할 때 `simai-bootstrap` 이 받는다(아래 "실험 이미지") |
| `mapanything/Dockerfile`, `mapanything/requirements.{in,lock.txt}` | EXP-01 MapAnything 이미지: Python 3.12, torch 2.7.1+cu128, map-anything @3d10cf7 (아래 "실험 이미지") |
| `texrecon/Dockerfile` | EXP-01 텍스처 메시 굽기 도구(S15P31A101-298): mvs-texturing `texrecon`(BSD-3) @f337429, mve·rayint 커밋 고정, CPU 전용 우분투 22.04(다이제스트 고정). 노트북 Docker 에서 `ai/experiments/static_scene/texture_mesh.py` 가 컨테이너 하나를 띄워 `docker cp` 로 입력·결과를 주고받는다(폴더 붙이기 없음, 네트워크 없음). 설계 `docs/design/exp01-textured-mesh.md` |
| `risk-engine/Dockerfile` | EC2 Risk Engine 컨테이너(S15P31A101-99): CPU 전용 python:3.11.15-slim, `ai/uv.lock` 의 extra `risk_api`(FastAPI·uvicorn·jsonschema)만, 비루트(uid 10001)·EXPOSE 없음·헬스체크 `/healthz`, 계약 스키마·공통 벡터 포함(약 225 MB). compose `risk-engine` 은 internal 망 `simai-ec2-internal` 에서 `risk-engine:8080` 으로만 닿는다(포트 공개 없음). 설계 `docs/design/risk-engine-container.md` |
| `scripts/risk_plan_roundtrip.py`, `scripts/risk_engine_g304.sh` | Risk Engine 컨테이너 계약 예시 왕복(HTTP·라이브러리 직접), D-G3-04 두 이미지 대조(`bash deploy/scripts/risk_engine_g304.sh simai/risk-engine:$TAG simai/tier0:$TAG`: 공통 벡터 출력 같음 + API 응답 = Tier 0 라이브러리 응답) |
| `*/Dockerfile.dockerignore` | 이미지별 빌드 컨텍스트 **허용 목록**(나열한 파일만 들어간다) |
| `compose.yaml` | 로컬 점검용(RunPod 은 Compose 를 쓰지 않는다) |
| `scripts/check_image.sh` | 이미지 점검(GPU 불필요, 세 이미지 공통·종류는 이미지 ENV `SIMAI_IMAGE_KIND`): 비밀·가중치·엔진·SSH 호스트 키 없음, import, 진입점 오류 코드, GPU 별 엔진 경로(Tier 0), 가중치 묶음 선택(실험), SSH 비밀번호 접속 거부 |
| `scripts/exp_image_check.py` | 실험·통합 이미지 GPU 점검(Pod, 세션 큐 g0·m0·u1): CUDA·이 GPU 세대 커널(torch)·Detectron2 LazyConfig 읽기(CUDA 확장 없음)·pyrender EGL·MapAnything import, 통합 이미지는 HMR(잠금 밖 휠 없이)·vLLM 플러그인·MapAnything(S15P31A101-250) |
| `scripts/gpu_session1.py` | GPU 세션 검증 큐(S15P31A101-183·-207): gpu_queue 로 아래 스크립트를 차례로 돌리고 결과 묶음·scp 명령을 낸다. `prepare`(노트북 사본)·`plan`·`run`(Pod 또는 `--mode dryrun`). 지금 이미지(`--image`, 기본 `SIMAI_IMAGE_KIND`)의 항목만 돈다 |
| `scripts/pod_check.py` | Pod 점검(-31): GPU(최소 사양)·드라이버·CUDA·마운트·용량·쓰기 속도·fsync·flock → JSON. `--no-volume` 은 볼륨 측정 SKIP |
| `scripts/session1_engine.sh` | 아래 "Pod 검증 큐" 1)~7) 을 한 Job 으로(단계별 종료 코드·시간을 steps.tsv·metrics.json 에) |
| `scripts/crash_recovery_g3.py` | Network Volume 강제 종료 복구 실측(-92): 실제 Tier 0 kill -9 ·재시작 + -51 결정적 지점, G3 항목 0건 판정 |
| `scripts/repeat_pytest.py` | pytest 반복(Linux TLS 시험 -45·-175) |
| `scripts/session1_live.py` | LIVE 입력 Pod 확인(-182): 컨테이너 vCPU·CPU 모델 기록, 4스트림 CPU 디코딩 처리량(카메라별 스레드, 판정)·NVDEC 열림·gpu 시험·NVDEC 처리량(참고), LIVE 기동 하네스(기본 디코더 CPU)·10명 기하 처리 시간 |
| `scripts/session1_gpu_priority.py` | Tier 0 GPU 우선 보호 Pod 측정(-60 P0~P7): LIVE 하네스 + `tier0_priority_probe` |
| `scripts/i0_link_measure.py` | 막힌 망 I0 예비 경로 측정(-288, 4차 세션 ④): `pod`(Tier 0 LIVE + 표시 파일 기록)·`laptop`(녹화 재생 relay + 제어 채널, gateway/relay 환경)·`summarize`(두 경로를 한 JSON·표로)·`token-hash`. 절차는 `docs/design/runpod-environment.md` "I0 443 예비 경로 측정" |
| `scripts/simai_gpu_boot.py` | 이미지의 `simai-gpu-boot`(S15P31A101-700): 부팅 비밀값으로 GPU 세션 등록 → 토큰 파일 → GPU 캐시(S3, 워커 토큰 서명) → 시작 묶음 → preflight → lobby → 단계 보고. 기본 CMD `auto` 가 부른다(아래 "GPU 자동 부팅") |
| `scripts/wait_tier0_ready.py` | 이미지의 `simai-wait-tier0`: 분석 워커를 Tier 0 표시 파일 `phase=running`·`reserve.state=OK` 뒤에 띄운다(아래 "분석 워커") |

Network Volume 구조·권한·보관, Pod 점검 기준, SSH 재접속·EC2 경유 기본 경로·443 최후 예비 경로, 세션 큐 항목·예상 시간은 `docs/design/runpod-environment.md` 에 있다.

엔진 캐시 코드는 `ai/src/simai_ai/detection/engine_cache.py`(시험 `ai/tests/detection/test_engine_cache.py`)다.

## 이미지에 넣지 않는 것

| 무엇 | 어디에 | 비고 |
|---|---|---|
| 가중치 (YOLO26x-pose `.pt`, sam-3d-body-dinov3·MoGe2·map-anything 등) | `$SIMAI_ROOT/weights/` | 해시 목록은 저장소 `deploy/weights.json`(3.12). `simai-bootstrap` 이 없으면 받고 다르면 시작하지 않는다. Tier 0 는 `SIMAI_TIER0_WEIGHTS_SHA256`(부트스트랩이 목록 값으로 채움)과 다르면 시작하지 않는다. **예외: 통합 이미지는 시연 가중치(tier0·avatar·ppe·static 기본 판, 약 13.4GB)를 `/opt/simai/baked` 에 굽는다**(아래 "시연 가중치 굽기"). VLM(Cosmos3-Nano 31.5GB)은 굽지 않는다 |
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
| EXP-02 Python 의존성 | `exp02/requirements.lock.txt`(해시 포함, Python 3.11). 잠금 밖 3개(chumpy·Detectron2·MoGe)는 커밋 고정 휠 + `--no-deps` |
| MapAnything Python 의존성 | `mapanything/requirements.lock.txt`(해시 포함, Python 3.12). map-anything 자체는 커밋 고정 + `--no-deps` |
| wstunnel (막힌 망 최후 예비 SSH) | 10.7.1 linux_amd64 릴리스 묶음 sha256 `fa842ed5…ea9ea2`(`base/Dockerfile` ARG). 다르면 빌드가 실패한다 |
| 실험 이미지 코드 사본 | Fast-SAM-3D-Body @d72aa36, DINOv3 허브 코드 @6876159, map-anything @3d10cf7, DINOv2 허브 코드 @7764ea0 (Dockerfile ARG) |
| 이미지 자체 | 레지스트리에 올린 이미지의 다이제스트. RunPod 템플릿은 태그가 아니라 다이제스트로 이미지를 가리킨다 |

**Tier 0 가 CUDA 13 인 이유.** `ai/uv.lock` 의 torch 2.11.0(vLLM 0.21.0 과 같은 판, S15P31A101-235)은 리눅스에서 CUDA 13.0 런타임 휠을 직접 가져온다.
그래서 베이스는 같은 판의 `nvidia/cuda:13.0.3-base-ubuntu24.04` 이고 TensorRT 도 `tensorrt-cu13` 이다. CUDA 라이브러리는 휠이 가져오므로 `runtime` 이 아니라 `base` 변형을 쓴다(중복 수 GB 절약).
CUDA 13.0 은 드라이버 580 이상이 필요하다. 이 베이스에는 순방향 호환 라이브러리가 있어 데이터센터 GPU(H100)는 535·550·565·570·575 계열 드라이버에서도 돌 수 있다(`NVIDIA_REQUIRE_CUDA`).
RunPod 에서 Pod 를 고를 때 CUDA 버전 필터를 13.0 이상으로 두는 것이 가장 확실하다(Pod 검증 큐 1번에서 확인).

**TensorRT.** `tensorrt-cu13` 의 libs 휠(약 3.8GB)은 pypi.nvidia.com 에만 있어 `ai/pyproject.toml` 에 그 인덱스를 `explicit` 으로 두고 libs 패키지만 그 인덱스에서 받는다.
TensorRT·onnx·`nvidia-modelopt[onnx]` 는 `tier0` extra 에 `sys_platform == 'linux'` 로만 들어가 Windows 노트북 개발 환경에는 설치되지 않는다.

## 빌드 (노트북, GPU 불필요)

RunPod Pod 안에서는 Docker 를 쓸 수 없으므로 이미지는 노트북에서 빌드해 레지스트리에 올린다. 저장소 최상위에서:

```sh
TAG=$(git rev-parse --short=12 HEAD)
TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl tier0      # → simai/tier0:$TAG (로컬)
bash deploy/scripts/check_image.sh simai/tier0:$TAG               # 모두 통과해야 올린다
TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl unified    # → simai/unified:$TAG (같은 Tier 0 층 + 워커 층, S15P31A101-246)
bash deploy/scripts/check_image.sh simai/unified:$TAG             # Tier 0 항목 + vLLM·HMR import·EGL 렌더·감시 프로세스 순서
TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl exp02      # → simai/exp02-fs3db:$TAG (Detectron2 CPU 전용 빌드)
TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl mapanything # → simai/mapanything:$TAG
bash deploy/scripts/check_image.sh simai/exp02-fs3db:$TAG         # 실험 이미지도 같은 점검(종류별 항목)
bash deploy/scripts/check_image.sh simai/mapanything:$TAG
TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl risk-engine # → simai/risk-engine:$TAG (CPU, EC2 Risk Engine 컨테이너)
bash deploy/scripts/check_image.sh simai/risk-engine:$TAG         # 벡터·비루트·포트 없음·망 없는 컨테이너 HTTP 왕복
```

- **시연 가중치 굽기(S15P31A101-577).** `unified` 는 Tier 0·아바타·현장 세팅(MapAnything 기본 판·WorldMirror 2.0·LaMa)·PPE 가중치를 이미지 층(`/opt/simai/baked/<목록 path>` + `.sha256`)으로 싣는다. 새 Pod 는 이것들을 HF·GitHub 에서 받지 않고, `simai-bootstrap` 이 사본을 sha256 확인한 뒤 `$SIMAI_ROOT/<path>` 에 심볼릭 링크로 건다(상태 `baked`). VLM 만 Pod 시작 뒤에 받는다. 빌드 셸에 HF 토큰이 있어야 한다(sam-3d-body 승인제·PPE 비공개 저장소). 토큰은 BuildKit 비밀로만 들어가고 층·이력·ARG 에 남지 않는다:
  ```sh
  read -rs HF_TOKEN && export HF_TOKEN       # 셸 기록에 남기지 않는다(Pod 의 RunPod Secret hf_token 과 같은 토큰)
  TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl unified
  unset HF_TOKEN
  ```
  가중치 층은 `--link` 라 잠금·코드가 바뀌어도 다시 만들지 않고 같은 다이제스트라 다시 올리지 않는다(처음 한 번 약 13.4GB 더 올린다). 원본·해시를 바꿀 때는 `deploy/weights.json` 과 `deploy/tier0/Dockerfile` 의 `weights-*` 단계를 같이 바꾼다(시험이 대조한다). 설계·근거: `docs/design/bake-demo-weights.md`.
- Tier 0 이미지는 풀면 약 18.7GB, 통합 이미지는 약 24.3GB 다(시연 가중치 굽기 전. 구운 뒤 약 37.7GB 추정)(2026-10-23 노트북 빌드, S15P31A101-246: torch 2.11.0·onnxruntime-gpu·cupy 제외. 층별 크기는 `docs/design/unified-env.md` "빌드 결과"). 이전 Tier 0 는 압축 약 7.9GB·풀면 약 21GB 였다. 처음 빌드는 받기가 대부분(3MB/s 망에서 약 90분), 이후에는 uv 캐시 마운트로 바뀐 층만 다시 만든다.
- **Re-ID(S15P31A101-471).** Tier 0·통합 이미지는 `--extra reid`(timm·yacs·ftfy·einops 등, torch 는 tier0 와 같은 판)를 함께 설치한다. COPE 저장소는 LICENSE 가 없어 이미지에 넣지 않는다 — 세션 준비 때 Pod `/srv/simai/reid/{cope,weights/cope_msmt17.pth,clip-cache/ViT-B-16.pt}` 로 올리고 `SIMAI_COPE_ROOT`·`SIMAI_COPE_WEIGHTS`·`SIMAI_COPE_CLIP_PATH` 를 Pod 환경에 넣는다(Tier 0 설정 파일에 경로가 없으면 이 환경변수를 읽는다). 없으면 Re-ID 만 꺼지고(`reid: disabled`) 나머지는 그대로 돈다.
- **층 분할(S15P31A101-207).** 큰 휠은 잠금에서 뽑은 묶음별 층(CUDA 13 휠·torch·TensorRT libs·큰 일반 휠)이고, 작은 패키지는 그 위 한 층, simai_ai 코드는 맨 위다. 로컬 재빌드에서 작은 패키지 하나가 바뀌면 다시 만든 층이 풀린 크기로 약 0.6GB, 코드 한 줄이면 약 9MB 다(push 전송량은 압축 크기라 더 작다. 같은 빌드 기계의 BuildKit 캐시가 남아 있을 때. 실측은 `docs/design/exp-images.md`).
  묶음 이름이 잠금에 없으면 빌드가 멈춘다. 묶음을 바꿀 때는 `deploy/tier0/Dockerfile` 의 `pick` 줄을 고친다.
- Docker Hub 받기가 `httpReadSeeker ... EOF` 로 끊기면(SSAFY 망에서 자주 봤다) 같은 명령을 다시 실행한다. 받은 층은 남는다.
- `git status` 가 깨끗할 때만 올릴 이미지를 빌드한다(`TAG` 가 커밋을 가리키므로).
- EXP-02 의 Detectron2 는 CUDA 확장 없이 빌드한다(`FORCE_CUDA=0`, S15P31A101-238). 우리 경로가 쓰는 기능은 LazyConfig(순수 Python)뿐이라 nvcc·CUDA devel 베이스·GPU 세대별 컴파일이 필요 없고, GPU 가 바뀌어도 다시 빌드하지 않는다.
  빌드·실행 단계 모두 CUDA 12.8.1 base 다(devel 이미지를 받지 않는다). `check_image.sh` 가 확장이 CPU 전용(`has_cuda()` False)인지 확인한다.
- Detectron2 CPU 확장(C++) 컴파일 병렬 수는 `EXP02_MAX_JOBS`(기본 2, Docker Desktop 메모리 2GB 에서 맞춘 값). 메모리를 8GB 이상으로 올렸으면 `EXP02_MAX_JOBS=8` 로 빨라진다.
- 노트북 빌드 실측(층 크기·시간·다시 올라가는 층)은 `docs/design/exp-images.md` "빌드 결과" 에 있다.

정적 점검: `docker run --rm -i hadolint/hadolint:v2.14.0 < deploy/tier0/Dockerfile` (세 Dockerfile 모두), 셸 스크립트는 shellcheck. 셸 스크립트는 LF 다(`.gitattributes`).

## 레지스트리 푸시 (사람이 확인한 뒤에만)

푸시는 외부로 내보내는 작업이라 사용자 확인 뒤에 한다. 레지스트리는 비공개여야 한다(Docker Hub 비공개 저장소 `docker.io/jonghwa1492/tier0`, SSAFY GitLab 컨테이너 레지스트리는 구성되어 있지 않다). 예시 다이제스트: `sha256:92af16d5320bcf20e39b8c5a2028f33ec3f390454c761a81205d8fb4bb6c8b3c`(dev da68f00 빌드). 무료 플랜은 비공개 저장소가 하나라 실험 이미지도 같은 저장소에 태그 접두(`exp02-fs3db-`, `mapanything-`)로 올린다. S15P31A101-207 빌드(2026-10-21): Tier 0 `tier0:3926a8772649` `sha256:7fe5811585ffea1c69cd027b2fe0fe2e8b329435cb13eb81032826f3f48b6163`, HMR `tier0:exp02-fs3db-3926a8772649` `sha256:da77b601689973861cd5caf049fa67be36e0c11763987061c67df814bc2a6e28`, MapAnything `tier0:mapanything-3926a8772649` `sha256:f171420cfaed7182185f0ccbf977a8e46492915d21790da0d1dedbec3129d55f`(docs/design/exp-images.md).

```sh
docker login <레지스트리>                                         # 토큰은 docker 자격 증명 저장소에만
REGISTRY=<레지스트리>/<프로젝트> TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl --push tier0
docker buildx imagetools inspect <레지스트리>/<프로젝트>/tier0:$TAG --format '{{.Manifest.Digest}}'
#  → sha256:...  이 값이 이미지 다이제스트다. RunPod 템플릿 이미지는 <레지스트리>/<프로젝트>/tier0@sha256:... 로 쓴다
```

RunPod 은 Container Registry Auth(읽기 전용 토큰)로 접근한다. 토큰은 RunPod 계정의 Container Registry Auth 에 등록한다. 템플릿 환경 변수에 넣지 않는다.

## env_hash (Run 기록)

`run_record.env_hash` 는 레지스트리에 올라간 이미지의 다이제스트(`sha256:...`)다(contracts/experiments/run_record.schema.json, 명세 21.5).

1. 위 푸시 절차의 `imagetools inspect` 로 다이제스트를 얻는다. 로컬 이미지 ID(`docker images` 의 IMAGE ID)는 다이제스트가 아니므로 쓰지 않는다.
2. RunPod 템플릿 이미지를 `...@sha256:<다이제스트>` 로 지정하고, 같은 값을 템플릿 환경 변수 `SIMAI_IMAGE_DIGEST` 에도 넣는다(컨테이너 안에서는 자기 다이제스트를 알 수 없다).
3. GPU 큐 Job 파일의 `record.env.image_digest` 에 같은 값을 적는다. `run_record.py` 는 이미지 다이제스트만 있으면 그 값을 그대로 `env_hash` 로 쓴다(`ai/experiments/gpu_queue/README.md`).
4. 다이제스트를 모르는 Run(로컬 이미지, 볼륨 환경)은 `env_hash` 를 다이제스트로 채우지 않는다. 볼륨 환경은 기존처럼 잠금 파일 해시(`lock_files`)를 쓴다.

## simai-bootstrap (이미지 기본 진입점, S15P31A101-205)

어느 GPU·지역·사이트에서든 같은 순서로 시작한다. 진입점은 `tini → simai-start(SSH) → simai-bootstrap` 이고 기본 인자는 `auto` 다(S15P31A101-700: 부팅 비밀값 환경 변수가 있으면 GPU 자동 부팅, 없으면 `tier0` 와 같다).

| 명령 | 하는 일 |
|---|---|
| `auto` | 이미지 기본 CMD(S15P31A101-700). `SIMAI_GPU_BOOT_SECRET`·`SIMAI_GPU_BOOT_ID`·`SIMAI_EC2_BASE_URL` 가 모두 있으면 비밀 값을 꺼내기 전에 `simai-gpu-boot auto` 로 exec(아래 "GPU 자동 부팅"), 비밀값만 있고 나머지가 없으면 64, 비밀값이 없으면 `tier0` 와 같다 |
| `tier0 [Tier 0 인자]` / `--config ...` | 준비 → `simai-tier0`(엔진 준비 `engine_cache ensure` → `python -m services.tier0`). 부팅 비밀값 없는 `auto` 가 이것이다 |
| (인자 없음) | 사용법만 보이고 64 로 끝난다. 아무것도 띄우지 않는다(S15P31A101-414: 10차 세션에서 SSH 셸의 맨 `simai-bootstrap` 이 예전 교실 기본값으로 Tier 0 를 띄웠다). 컨테이너 시작 경로(CMD `tier0`, 시작 명령 `sleep infinity`)는 그대로다 |
| `lobby [로비 인자...]` | 로비 LIVE 통합 기동(S15P31A101-382): `endpoint.json`·`tier0-live.json`·`pod.jsonl` 구성 → 단일 `Supervisor` 로 `tier0,vllm,worker,avatar` 를 띄우고 Tier 0 종료 코드 그대로 끝난다. 프로세스는 `--services` 만 따르고 `SIMAI_SERVICES` 는 보지 않는다, 워커 ID 는 `--worker-id` → `SIMAI_WORKER_ID` → 토큰 파일 이름(S15P31A101-414, 아래 "로비 LIVE 통합 기동") |
| `queue [gpu_session1 run 인자]` | 준비 → GPU 1차 세션 큐(저장소 사본 `$SIMAI_SESSION_REPO`, 기본 `$SIMAI_ROOT/setup/session1/repo`). 엔진 첫 빌드 시간은 큐 b 항목이 재므로 엔진 준비는 하지 않는다 |
| `check [--preflight\|--sets ...]` / `preflight` | 준비만 하고 결과 JSON 출력. `preflight`(또는 `check --preflight`)는 `tier0,avatar,static,vlm` 가중치·GPU 예산(lobby 기본 `tier0,vllm,worker,avatar`, `SIMAI_SERVICES` 무시)·`texrecon`·`open3d`·`ultralytics` 를 일괄 점검하고 하나라도 실패하면 비-0 종료 코드(2·3·64)로 끝난다. py-spy 는 `diagnostics.py_spy` 에 안내만 한다(RunPod 는 SYS_PTRACE 가 없어 `unavailable` → 내장 계측 -383, 실패 아님) |
| `scene [--worker-token-file <경로>] [--] <명령...>` | 장면 준비 모드(S15P31A101-250·-382, 통합 이미지의 MapAnything·정적 장면 일회성 작업): Tier 0·GPU 확인 → `static` 가중치 → 재확인 → 그 명령을 자식으로 실행하며 Tier 0 시작을 감시(나타나면 장면 작업을 끝내고 75). 종료 코드는 명령의 것. 아래 "장면 준비 모드" |
| 그 밖의 명령(`sleep infinity`, `bash`, `simai-wait-tier0`, `python ...`) | 준비 없이 그대로 실행 |

준비 순서와 종료 코드(simai-tier0·engine_cache 와 같은 규칙: 2 저장·가중치, 3 GPU):

1. 저장 위치: `SIMAI_ROOT` → `/workspace` 가 마운트 지점(`os.path.ismount`: RunPod 볼륨·Pod 볼륨·`docker run -v`)이고 쓸 수 있을 때만 `/workspace/simai`(볼륨 없이 있는 `/workspace` 폴더는 고르지 않는다, S15P31A101-311) → 컨테이너 디스크 `/srv/simai`. 남은 용량 하한 `SIMAI_MIN_FREE_GB`(기본 20) 미만이면 2.
2. GPU: `nvidia-smi` compute_cap → `sm_XX`(`SIMAI_GPU_ARCH` 로 넘김), 메모리·드라이버 기록. GPU 없음, compute_cap·메모리를 읽지 못함, Ampere(8.0) 미만, VRAM 24GB급(22,500 MiB) 미만이면 3. 가중치를 받기 전에 끝난다.
3. 가중치: `weights.json`(이미지 `/opt/simai/deploy/weights.json`)의 `tier0` 항목. 있고 sha256 이 맞으면 건너뜀(엘리스 블록 스토리지 재사용), 없으면 옆 임시 파일(`<이름>.part`)로 받아 sha256 이 맞을 때만 이름을 바꾼다.
   받기가 끊기거나 429·5xx 면 지수 백오프(2·4·8·16초)로 `SIMAI_FETCH_ATTEMPTS`(5)번까지 다시 하고 임시 파일에서 HTTP Range 로 이어받는다(원본이 Range 를 무시하거나 범위가 다르면·416 이면 처음부터, 401·403·404 는 다시 하지 않음). 다 실패해도 받은 바이트는 남겨 다음 시작이 잇는다. 항목마다 지우지 않는 잠금 파일(`$SIMAI_ROOT/weights/.locks/`)로 재확인·받기·해시·게시(rename)·스탬프를 한 임계 구역으로 묶어, 같은 저장 위치의 두 부트스트랩은 앞의 것이 끝날 때까지 기다렸다가 그 파일을 다시 확인한다.
   `tier0` 묶음에 없는 항목(워커·실험 가중치)은 확인 뒤 검증 스탬프 `$SIMAI_ROOT/weights/.verified/<이름>.json`(경로·sha256·크기·mtime_ns·inode·ctime_ns, 해시 전후 stat 이 같을 때만)을 남기고, 다음 시작에 네 값이 같으면 다시 해시하지 않는다(Cosmos3-Nano 31.5GB). 강제로 다시 확인하려면 `weights/.verified/` 를 지운다(가중치는 그대로). Tier 0 가중치는 매번 해시한다. 있는데 다르면 지우지 않고 2, 받은 것이 다르면 버리고 2, 목록에 해시가 없으면 2. HF 원본(`source.hf`)은 `HF_TOKEN` 을 첫 요청에만 싣는다(다른 호스트 리다이렉트에는 안 실림). 해시 근거는 `docs/data/weights.md`. `SIMAI_TIER0_WEIGHTS`·`SIMAI_TIER0_WEIGHTS_SHA256` 이 비어 있으면 목록 값으로 채운다.
4. 기록 `$SIMAI_ROOT/runs/bootstrap/<UTC>-<pid>.json`(추가만, 토큰 값 없음, `hf_token: present|absent` 만) → exec.

HF 토큰은 시작하자마자 환경에서 꺼내 부트스트랩 프로세스 안에만 둔다(가중치 받기에만 씀). nvidia-smi 조회·도움말·그대로 넘기는 명령·Tier 0·큐 어느 자식에도 넘어가지 않는다. `simai-start` 도 ssh-keygen·sshd 를 토큰 없는 환경으로 띄운다. `HUGGING_FACE_HUB_TOKEN` 은 읽지 않고 지울 이름으로만 다룬다.

**가중치 묶음(S15P31A101-207).** `tier0` 명령은 언제나 `tier0` 묶음을 준비한다. `check`·`queue`·`run` 은 이미지 ENV `SIMAI_WEIGHTS_SET`(Tier 0 이미지는 없음 = `tier0`,
HMR 이미지 `exp02`, MapAnything 이미지 `static`)의 묶음을 준비한다. 묶음 이름이 틀렸거나 항목이 없으면 2, 묶음에 sha256 이 빈 항목이 하나라도 있으면
**아무것도 받기 전에** 2 로 멈춘다(앞 항목 수 GB 를 받고 나서 실패하지 않게). `run <명령...>` 은 준비 뒤 그 명령을 exec 한다(명령이 없으면 64).
해시 근거는 `docs/data/weights.md` "실험 이미지 가중치".

## RunPod 템플릿 (Tier 0, 볼륨 없음)

| 항목 | 값 |
|---|---|
| 데이터센터·GPU | 그때그때 남은 지역·GPU. 최소 Tier 0 만 24GB급(RTX 4090 이상), Tier 0 + 실험 48GB(80GB 권장), Ampere 이후. GPU 하나(Tier 0 가 먼저 쓴다) |
| 이미지 | `<레지스트리>/<프로젝트>/tier0@sha256:<다이제스트>` |
| CUDA 버전 필터 | 13.0 이상(위 "CUDA 13 인 이유") |
| Container Disk | 100GB 이상(이미지 풀면 약 19.5GB + 가중치·엔진·결과. 볼륨이 없으므로 여기가 `$SIMAI_ROOT=/srv/simai`) |
| Volume | 붙이지 않는다(Network Volume 은 데이터센터에 묶인다). Pod 볼륨을 쓰면 `/workspace/simai` 가 된다 |
| 공개 HTTP 포트 | 8080(막힌 망 최후 예비 SSH·I0 의 wstunnel, `https://<POD_ID>-8080.proxy.runpod.net`. 아래 "막힌 망 최후 예비 SSH") |
| 공개 TCP 포트 | 22(SSH, 키 인증만), 17000(I0 제어)·17001(I0 영상) 노출(매핑된 바깥 포트는 콘솔 또는 Pod 의 `RUNPOD_TCP_PORT_22`·`RUNPOD_TCP_PORT_17000`·`RUNPOD_TCP_PORT_17001`, 아래 "SSAFY 망 기본 경로", 명세 3.12). 공개 TCP 70000 이상 대칭 포트 요청은 새 RunPod 템플릿 화면이 1~65535 만 받아 할 수 없으므로 TCP 22·17000·17001 을 노출해 SSAFY 망 기본(EC2 경유)·직접 연결에 쓰고, 최후 예비인 443 터널 LIVE 는 공개 TCP 포트 없이 돌린다(`SIMAI_TUNNEL_ALLOW=17000,17001`, S15P31A101-311·-392) |
| 공개 주소 환경 변수 | Tier 0 가 읽는 순서(앞이 우선, 값이 있는 첫 이름만 쓰고 틀린 값이면 오류): 공개 IP `SIMAI_PUBLIC_HOST` → `RUNPOD_PUBLIC_IP`, 공개 포트 `SIMAI_PUBLIC_PORT_<번호>` → `RUNPOD_TCP_PORT_<번호>`. 어느 것도 없으면 등록·기동 오류. RunPod 는 자동으로 들어오고 그 밖의 사이트는 사람이 사이트 무관 이름으로 넣는다. Vast 이름은 docs.vast.ai 로 확인하지 못해 읽지 않는다 |
| 시작 명령 | 비움(이미지 기본: `simai-bootstrap auto` = 부팅 비밀값이 있으면 GPU 자동 부팅, 없으면 `tier0`). 손 경로·검증 세션은 `sleep infinity` 로 띄우고 SSH 로 `simai-bootstrap queue`·`lobby` |
| GPU 기능 | 이미지 `NVIDIA_DRIVER_CAPABILITIES=compute,utility,video`(video 는 선택 디코더 NVDEC 용, -182. Tier 0 기본 디코더는 CPU 라 없어도 기본 경로는 선다, -236). 템플릿에서 덮어쓰지 않는다 |
| 환경 변수 | `HF_TOKEN={{ RUNPOD_SECRET_hf_token }}`(RunPod Secret, 값은 템플릿에 쓰지 않음), `SIMAI_TIER0_GPU_RESERVE_MB=8192`(Tier 0 GPU 선확보, -60, G0 실측 전 초기값), `SIMAI_IMAGE_DIGEST=sha256:...`(이미지 다이제스트와 같은 값), `PUBLIC_KEY=<계정 SSH 공개키>`(비밀 아님), `SIMAI_TUNNEL_PREFIX={{ RUNPOD_SECRET_simai_tunnel_prefix }}`(RunPod Secret, 막힌 망 최후 예비 SSH), 최후 예비로 I0 를 443 터널로 보낼 때만 `SIMAI_TUNNEL_ALLOW=17000,17001`(비밀 아님, 아래 "I0 터널"). `SIMAI_ROOT`·가중치 경로·해시·엔진 캐시는 부트스트랩이 정한다(`SIMAI_ENGINE_CACHE` 기본 `$SIMAI_ROOT/engines`, `SIMAI_OBS2D_ROOT` 는 필요하면 `$SIMAI_ROOT/obs2d`). 선택: `SIMAI_TIER0_MODE`(e2e), `SIMAI_TIER0_IMGSZ`(1280), `SIMAI_TIER0_MAX_BATCH`(4), `SIMAI_TIER0_CAMERA`(2304x1296) |
| 분석 워커 환경 변수 | 같은 Pod 에서 워커를 띄울 때(아래 "분석 워커"): `SIMAI_EC2_BASE_URL`, `SIMAI_WORKER_ID`(EC2 에 등록한 ID. 토큰과 짝이 다르면 대여가 401. `simai-bootstrap` 감시 모드·lobby·after-live 는 비어 있으면 토큰 파일 이름 `<워커 ID>.token` 에서 정한다, S15P31A101-414), `SIMAI_WORKER_GPU_MEMORY_FRACTION=0.70`, `SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION=0.30`, `SIMAI_VLLM_QUANTIZATION=fp8`(기본 `fp8`, `none`·빈 값이면 비활성, S15P31A101-525), `SIMAI_WORKER_VLLM_MAX_NUM_SEQS=4`, `SIMAI_GPU_TIER0_FRACTION=0.20`, `SIMAI_WORKER_LAG_BUDGET_MS=600`. 선택(기본값): `SIMAI_WORKER_MARKER_MAX_AGE_S`(15), `SIMAI_WORKER_RESUME_AFTER_S`(10), `SIMAI_WORKER_MAX_QUEUE_WAIT_EXCEEDED`(2), `SIMAI_WORKER_LIVE_BLOCKED_TYPES`(static_reconstruct,hmr_refine,hmr_appearance_update), `SIMAI_WORKER_LIVE_WAIT_MS`(5000), `SIMAI_WORKER_HOLD_MAX_S`(60), `SIMAI_TIER0_MARKER`, `SIMAI_SETUP_WORLD_ANCHOR`(auto, `off` 면 현장 세팅이 새 보정을 이전 채택 World 에 맞추지 않음, docs/design/world-reanchor.md). 값의 뜻은 docs/design/tier0-gpu-priority.md |
| 비밀 | 템플릿에 값으로 넣지 않는다(HF 토큰·터널 비밀값만 Secret 참조). 워커 토큰은 `.simai-private/worker.env` 의 `SIMAI_WORKER_TOKEN=` |
| 공개키 | RunPod 은 계정 SSH 키를 `PUBLIC_KEY` 로 넣지만, 템플릿에 `PUBLIC_KEY` 를 빈 값으로 두면 그 값이 덮어써 공개키가 하나도 없게 되고 sshd 가 뜨지 않는다(3차 세션 2026-10-26: 빈 `PUBLIC_KEY` + 이름 칸에 이미지 다이제스트가 들어간 `SSH_PUBLIC_KEY`). 그래서 템플릿에 계정 공개키 값을 그대로 넣는다(공개키는 비밀이 아니다). Pod 를 켠 직후 RunPod Logs 에서 `simai-start: sshd 시작 (키 인증만, 키 N개)` 와 `wstunnel 시작` 줄을 먼저 보고, `공개키 형식이 아닌 줄 … 버렸다` / `sshd 를 띄우지 않아 443 예비 터널도 띄우지 않는다` 가 보이면 템플릿 `PUBLIC_KEY` 값(한 줄, `ssh-ed25519 …`)을 고친다(4차 세션: 이 때문에 약 45분 접속 불가) |

결과는 컨테이너 디스크에 있으므로 Pod 를 지우기(Terminate) 전에 scp 로 가져온다(세션 큐가 명령을 출력한다). Stop 만 해도 같은 GPU 를 다시 못 잡을 수 있다.

### SSAFY 망 기본 경로 (EC2 경유, S15P31A101-392)

SSAFY 망(시연장도 SSAFY 망)에서는 일반 외부(`portquiz.net`)의 22·17000·30022 가 막히지만 팀 EC2(`simai.live:22`)는 열린다(2026-11-02 실측: SSH 배너·로그인 확인, SSAFY 가 지급 EC2 로의 22 허용). EC2 에서 밖으로는 22·17000·30022 모두 열리고 EC2 `sshd` 에 포워딩 제한 설정이 없으며, TCP 연결 시간은 노트북→EC2 약 5 ms(가끔 25 ms), EC2→일본 RunPod(이전 Pod IP) 약 41 ms 다(비교: RunPod 443 프록시는 요청당 약 0.58 s 추가(9차 세션), 집에서 직접 SSH ping 48 ms). 따라서 SSAFY 망에서는 **EC2(`simai.live`) SSH 경유 + Pod 공개 포트 Direct TCP** 를 기본 경로로 쓴다(명세 3.12).

1. **I0 (제어 wss·카메라별 영상 TLS):** 노트북에서 EC2 로 로컬 포워드를 연다.

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 simai-ec2 -L 127.0.0.1:17000:<Pod IP>:<17000 매핑> -L 127.0.0.1:17001:<Pod IP>:<17001 매핑>
```

- 노트북→EC2 구간(짧음, 약 5 ms)만 SSH 연결 하나를 같이 쓰고, EC2→Pod 구간(약 41 ms)은 포워드된 연결마다 별도 TCP 다 → 명세 12.2 "카메라별 독립 연결"의 목적(긴 구간에서 한 카메라 손실이 다른 카메라를 막지 않음)을 지킨다.
- TLS 는 Tier 0 가 직접 종료하고, 기기 토큰·공개키 고정도 그대로다(명세 12.7, 14). EC2 는 영상 내용을 볼 수 없다.
- RunPod 템플릿에 TCP 22·17000·17001 을 노출한다(매핑된 바깥 포트는 콘솔 또는 Pod 의 `RUNPOD_TCP_PORT_17000`·`RUNPOD_TCP_PORT_17001`). Tier 0 는 기본 `0.0.0.0` 에서 듣는다(`SIMAI_TIER0_CONTROL_LISTEN_HOST`).
- Tier 0 가 EC2 에 알리는 엔드포인트 `host` 는 `127.0.0.1`(노트북 로컬 포워드)이고 relay·현장 경보 단말은 그 값을 그대로 쓴다.

2. **관리 SSH (파일 올리기·로그):**

```powershell
ssh -J simai-ec2 -p <22 매핑> root@<Pod IP>
```

- **I0 를 이 Pod SSH 하나에 묶지 않는다**(긴 구간 TCP 하나에 제어+4대가 묶여 명세 12.2·16장 "SSH 터널은 접속용"과 어긋난다).

3. **직접 연결과 예비:**

- 집 등 막히지 않은 망은 EC2 를 빼고 Pod 공개 IP·포트로 바로 붙어도 된다.
- LTE 테더링은 두 번째 업링크로 유지하고, RunPod HTTPS 프록시 443 + wstunnel 은 지연이 커 최후 예비로만 둔다(아래 절).

4. **주의·운영 규칙:**

- EC2 가 I0 경로에 들어오므로 EC2 호스트가 멈추면 현장 경보도 함께 멈춘다(전에는 뷰어만). 대응: LTE 테더링·443 프록시로 전환. EC2 애플리케이션 배포는 컨테이너만 재시작하므로 호스트 `sshd` 와 I0 중계는 끊기지 않는다.
- EC2 CPU 를 Jenkins CI 와 같이 쓴다(`t3.xlarge` 4 vCPU, 버스트형) → LIVE 관제·GPU 세션 중에는 CI 빌드를 피한다.
- EC2 보안 그룹 인바운드 22 는 팀 IP 대역만 허용한다. 시연장 SSAFY 망의 바깥 IP 가 다르면 막힐 수 있으므로 시연장 망 시험(NFR-35)에 이 경로를 넣어 먼저 확인한다.
- EC2 접속 키(팀 EC2 pem)는 저장소 밖(`~/.ssh/`)에만 둔다.

### 막힌 망 최후 예비 SSH (443, wstunnel, S15P31A101-286)

SSAFY 노트북 망에서 일반 외부는 나가는 포트가 80·443·8000·8080 뿐이라 Pod 로의 직접 Direct TCP SSH(임의 높은 포트)가 시간 초과로 끝난다(3차 세션 2026-10-26).
SSAFY 망 기본 경로는 위 "SSAFY 망 기본 경로 (EC2 경유)"이고, EC2 경유와 LTE 테더링이 모두 안 될 때의 **최후 예비**로 RunPod HTTPS 프록시(443, 명세 3.12 최후 예비 경로, 요청당 약 0.58 s 추가) 위 WebSocket 으로 sshd 에 닿는다. 30 MB scp 32 s 로 SSH·scp 모두 확인했다.

- Pod: 이미지의 `simai-start` 가 sshd 를 띄운 경우에만 `wstunnel server --log-lvl OFF --restrict-to 127.0.0.1:22 --restrict-to localhost:22 ws://0.0.0.0:${SIMAI_TUNNEL_PORT:-8080}` 를 백그라운드로 띄운다.
  `SIMAI_SSHD=off` 이거나 공개키가 없으면 띄우지 않는다. 프록시 주소는 공개이므로 경로 비밀값 `SIMAI_TUNNEL_PREFIX` 가 없거나 약하면(영문·숫자·`_-` 32~128자가 아니면) 띄우지 않고 한 줄로 알린다.
- 비밀값 처리: `simai-start` 가 맨 앞에서 환경에서 지우고 wstunnel 에만 환경 변수(`WSTUNNEL_RESTRICT_HTTP_UPGRADE_PATH_PREFIX`)로 넘긴다. 명령줄(`ps`)·출력·넘겨받은 명령(`simai-bootstrap`)·SSH 세션 환경 파일에는 없다.
  wstunnel 로그는 규칙(비밀값 포함)을 찍으므로 끄고 버린다. 그래서 Pod 쪽 터널 로그는 없고, 확인은 노트북에서 접속해 본다.
- 비밀값 만들기(한 번, 노트북): `python -c "import secrets; print(secrets.token_urlsafe(32))"`(43자). RunPod 콘솔 Secrets 에 `simai_tunnel_prefix` 로 넣고, 노트북에는 저장소 밖 본인만 읽는 파일(예: `~/.ssh/simai-tunnel-prefix`)에 둔다.
- 노트북 wstunnel 클라이언트는 같은 판을 사람이 직접 받아 sha256 을 확인한다(https://github.com/erebe/wstunnel/releases/tag/v10.7.1, GitHub 릴리스 자산 digest 값).

| 묶음 | sha256 |
|---|---|
| `wstunnel_10.7.1_windows_amd64.tar.gz` | `deb3c8b8d9fecf5428f7e0caabbc13aeb3b1edbfba49e1724a7a065c027bd0f2` |
| `wstunnel_10.7.1_linux_amd64.tar.gz` | `fa842ed53fbb14b1c69cd98829f9895d7f8a6b0d562c57c1175851a52cea9ea2` |

```powershell
# Windows: 받은 묶음 확인 → 풀어서 wstunnel.exe 를 PATH 의 폴더(예: ~/bin)에 둔다
(Get-FileHash .\wstunnel_10.7.1_windows_amd64.tar.gz -Algorithm SHA256).Hash   # 위 표와 같아야 한다
tar -xzf .\wstunnel_10.7.1_windows_amd64.tar.gz wstunnel.exe
# 접속하는 셸에서만 비밀값을 환경 변수로 둔다(ssh 설정 파일·명령줄에 쓰지 않는다)
$env:WSTUNNEL_HTTP_UPGRADE_PATH_PREFIX = (Get-Content ~/.ssh/simai-tunnel-prefix -Raw).Trim()
ssh simai-pod-443
```

노트북 `~/.ssh/config` (`<POD_ID>` 는 콘솔의 Pod ID, Pod 를 새로 만들면 바뀐다):

```
Host simai-pod-443
  HostName <POD_ID>-8080.proxy.runpod.net
  User root
  IdentityFile ~/.ssh/<RunPod 에 등록한 키>
  ProxyCommand wstunnel client --log-lvl OFF --tls-verify-certificate -L stdio://127.0.0.1:22 wss://%h:443
  ServerAliveInterval 30
```

- 비밀값은 클라이언트도 환경 변수 `WSTUNNEL_HTTP_UPGRADE_PATH_PREFIX` 로 받는다(`-P <비밀값>` 도 되지만 설정 파일·프로세스 목록에 남는다). 비밀값이 틀리면 서버가 업그레이드를 거절해 SSH 가 바로 끊긴다.
- 연결은 오래 유지되지 않을 수 있다(RunPod 프록시 최대 100초, 명세 3.12). wstunnel 은 양쪽이 30초마다 WebSocket ping 을 보내고 위 설정은 SSH keepalive 30초다. 큐·긴 작업은 `nohup` 으로 띄우고 끊기면 다시 접속한다.
- 호스트 키는 컨테이너마다 새로 만든다(위 "SSH 직접"과 같은 재접속 규칙, `docs/design/runpod-environment.md`).
- 클라이언트 명령에는 늘 `--tls-verify-certificate` 를 붙인다. wstunnel 클라이언트는 기본으로 바깥 TLS(Cloudflare `*.proxy.runpod.net` 인증서)를 검증하지 않는다(10.7.1 도움말).
  그 위의 SSH(호스트 키)·I0(공개키 고정)는 따로 서버를 확인하지만 경로 비밀값은 바깥 TLS 만 지킨다. SSAFY 망에서는 검증을 켜도 붙는 것을 확인했다(2026-10-26: TLS 핸드셰이크 통과 → HTTP 404, 망이 TLS 를 가로채지 않음).
  시연장 등 다른 망에서는 다시 확인한다. 검증이 실패하면(망이 TLS 를 가로챔) 검증을 끈 채 쓰지 말고 보고한다.

#### I0 터널 (최후 예비, S15P31A101-288)

Direct TCP(I0 기본: 제어 wss·카메라별 영상 TLS, SSAFY 망 기본은 위 EC2 경유 Direct TCP, 명세 3.12·12.2)와 LTE 테더링이 모두 막혔을 때의 최후 예비 경로다. 같은 wstunnel 서버가 SSH 말고도 정해 둔 127.0.0.1 포트로 잇게 한다.

- Pod(템플릿 환경 변수): `SIMAI_TUNNEL_ALLOW=17000,17001`(제어·영상 포트, 쉼표로). `simai-start` 가 `--restrict-to 127.0.0.1:<포트>` 를 더한다. 22 는 늘 허용하고,
  목록이 틀리면(포트 번호가 아님·터널 포트 자신·16개 초과) 하나도 더하지 않고 22 만 연다(대상을 넓히지 않는 쪽, SSH 는 남는다). 이미지를 다시 빌드해야 들어간다(-288).
- Tier 0(Pod): 대칭 매핑 값을 터널 포트로 준다. 제품 코드는 그대로이고, 듣는 포트와 등록하는 주소가 함께 바뀐다.
  `SIMAI_TIER0_CONTROL_PORT=70000`, `SIMAI_TIER0_VIDEO_PORTS=cam1:70001,...`(4대가 영상 포트 하나를 같이 쓴다), `SIMAI_PUBLIC_HOST=127.0.0.1`,
  `SIMAI_PUBLIC_PORT_70000=17000`, `SIMAI_PUBLIC_PORT_70001=17001`, `SIMAI_TIER0_CONTROL_LISTEN_HOST=127.0.0.1`, `SIMAI_TIER0_VIDEO_LISTEN_HOST=127.0.0.1`.
  사이트 무관 이름(`SIMAI_PUBLIC_*`)이 RunPod 값보다 먼저라 같은 Pod 에서 Direct TCP 로 돌릴 때는 이 다섯 줄만 빼면 된다.
- 노트북: Pod 포트와 같은 번호의 로컬 포트를 연다(Tier 0 가 등록하는 주소가 `127.0.0.1:17000/17001` 이라 노트북 로컬 포트가 그 번호여야 한다).
  TCP 연결마다 WebSocket 하나가 열리므로 카메라별 영상 연결이 서로 독립인 구조(relay `tls_sink`)가 유지된다.

```powershell
$env:WSTUNNEL_HTTP_UPGRADE_PATH_PREFIX = (Get-Content <비밀값 파일> -Raw).Trim()
wstunnel client --log-lvl OFF --tls-verify-certificate -L tcp://127.0.0.1:17000:127.0.0.1:17000 -L tcp://127.0.0.1:17001:127.0.0.1:17001 wss://<POD_ID>-8080.proxy.runpod.net:443
```

- TLS: 노트북 relay·현장 경보 단말은 Tier 0 서버를 호스트 이름이 아니라 미리 고정한 공개키 지문으로 확인한다(`simai_site_alert.pinning`. 영상은 SNI 없이 `server_hostname=None`,
  제어는 엔드포인트 조회 경로의 고정 문맥). 그래서 접속 주소가 `127.0.0.1` 로 바뀌어도 검증은 그대로이고 끄거나 바꿀 것이 없다. 고정값과 다른 키면 기기 토큰을 보내기 전에 끊는다.
  다만 고정 주소(`url=`)로 만든 제어 채널은 기본 검증(체인·호스트 이름)을 써서 자체 서명 Tier 0 에는 붙지 않는다. 터널에서도 엔드포인트 조회(공개키 고정) 경로를 쓴다.
- 지금 한계: EC2 `GET /api/endpoints/tier0` 가 서버에 아직 없다(보안 설정만 있음). 그래서 4차 세션 측정은 `scripts/i0_link_measure.py` 가 Pod 출력(endpoint.json)과
  사람이 옮긴 지문(`--pin`)으로 같은 신뢰 규칙의 고정 대상을 쓴다. 시계 교환(`clock_sync_request`)은 현장 경보 단말이 답한다(S15P31A101-289).
- 이미지에 아직 `SIMAI_TUNNEL_ALLOW` 가 없을 때의 대안: SSH 포트 전달 `ssh -N -L 17000:127.0.0.1:17000 -L 17001:127.0.0.1:17001 simai-pod-443`. 이미지 변경이 필요 없지만
  모든 카메라·제어가 SSH 연결 하나(TCP 하나)에 묶여 한 카메라의 지연·끊김이 다른 카메라에 번지고, 그 연결이 끊기면 다 같이 끊긴다. 측정 결과를 wstunnel 직접 전달과 섞지 않는다.

**SSH 세션의 환경 변수.** sshd 가 띄운 셸에는 컨테이너 ENV 가 없다. `simai-start` 가 sshd 를 띄울 때 비밀이 아닌 이름(`SIMAI_*`·`CUDA_*`·`NVIDIA_*`·`UV_*`·`RUNPOD_*`·`PYTHON*`·`PATH`·`LD_LIBRARY_PATH`, 이름에 TOKEN·SECRET·PASSW·API_KEY·PRIVATE·CREDENTIAL 이 든 것 제외)만
`/etc/profile.d/simai-env.sh` 에 적는다. 로그인 셸(`ssh simai-pod`)은 자동으로 읽는다. 비로그인 셸에서도 `/usr/local/bin` 의 `python3`·`simai-bootstrap` 을 찾지만 컨테이너 환경 변수는 없으므로 명령 한 줄은 `ssh simai-pod 'bash -lc "simai-bootstrap queue ..."'` 처럼 로그인 셸로 돌린다. 백그라운드 스크립트도 `SIMAI_ROOT=/srv/simai` 를 명시한다(이 티켓 수정 뒤에도 습관으로).
이미 연 비로그인 셸(`ssh -J … 'cmd'`, tmux, `sh -c`)에서 `python3: not found` 가 나면(10차 세션) `. /etc/profile.d/simai-env.sh` 를 먼저 한다. 그 파일이 없으면(sshd 를 `simai-start` 가 띄우지 않은 컨테이너) 컨테이너 PID 1 환경에서 비밀 아닌 이름만 싣는다:

```sh
while IFS= read -r -d '' kv; do
  case "$kv" in SIMAI_TUNNEL_PREFIX=*|*TOKEN*|*SECRET*|*PASSW*|*API_KEY*) ;;
    PATH=*|VIRTUAL_ENV=*|LD_LIBRARY_PATH=*|SIMAI_*|RUNPOD_*|CUDA_*|NVIDIA_*|UV_*|PYTHON*|YOLO_*|PYOPENGL_PLATFORM=*) export "$kv" ;;
  esac
done < /proc/1/environ
```

작업 폴더는 어디든 된다: `simai-bootstrap` 이 이미지 ai 폴더(`/opt/simai/ai`, `SIMAI_AI_DIR`)를 자식의 `PYTHONPATH` 끝에 더해 `/srv/simai/setup/...` 에서 띄워도 `python -m services.tier0` 를 찾는다(10차 세션 ModuleNotFoundError, S15P31A101-414). cwd·앞쪽 `PYTHONPATH`(저장소 사본)가 여전히 먼저다.
HF 토큰·터널 비밀값은 이 파일에 없다. SSH 에서 가중치를 받아야 하면 그 명령에만 `HF_TOKEN="$(tr '\0' '\n' </proc/1/environ | sed -n 's/^HF_TOKEN=//p')" simai-bootstrap ...` 로 넘긴다.

**GPU 세션 공통 주의 (4차 세션 교훈, S15P31A101-311, 4차 결과 요약은 Jira S15P31A101-293 2026-10-28 댓글).**

- Pod 를 켠 직후 RunPod Logs 에서 `simai-start: sshd 시작 (키 인증만, 키 N개)` 와 `wstunnel 시작` 줄을 먼저 본다. `공개키 형식이 아닌 줄 … 버렸다` / `sshd 를 띄우지 않아 443 예비 터널도 띄우지 않는다` 가 보이면 템플릿 `PUBLIC_KEY` 값(한 줄, `ssh-ed25519 …`)을 고친다(4차: 이 때문에 약 45분 접속 불가).
- Pod 셸 명령은 컨테이너 환경 변수를 불러오도록 `bash -lc '…'` 로 부른다. 백그라운드 스크립트도 `SIMAI_ROOT=/srv/simai` 를 명시한다(이 티켓 수정 뒤에도 습관으로).
- 노트북(Windows)에서 만든 tar 는 Pod 에서 `tar --no-same-owner -xf` 로 푼다(소유자 uid 변경 실패로 tar 가 실패 종료).
- 저장소 사본 코드를 이미지에 깔린 패키지보다 먼저 쓰려면 Job `env` 에 `PYTHONPATH=<repo>/ai/src:<repo>/ai`(4차: 이미지 37c7ae5 에 없는 `simai_ai.hmr.prompt` 로 HMR 실패).
- `gpu_queue` 는 실패한 Job 도 끝난 것으로 기억하고, `exp_job` 은 결과 폴더(metrics.json)가 있으면 덮어쓰지 않는다(종료 4) → 다시 돌릴 때는 큐 이름과 Run 이름을 새로 준다.
- Tier 0 file 모드(녹화 재생)는 저장소 `ai/` 에서 `simai-bootstrap run simai-tier0 --config <json>` 으로(다른 위치면 `services` 모듈을 못 찾는다).
- 공개 TCP 70000 대칭 포트 요청은 새 RunPod 템플릿 화면이 1~65535 만 받아 할 수 없다 → -288 (b) LTE Direct 비교는 그 방법을 찾기 전까지 뺀다. 443 터널 LIVE 는 공개 TCP 포트 없이 된다(`SIMAI_TUNNEL_ALLOW=17000,17001`).
- 4차 실제 순서(다음 세션 기준안): 촬영(빈 장소 25 s ×2: 배경용·축척 정물용, 보정 걷기 180 s) → Pod: 장소 MapAnything·걷기 관절 추출(GPU 수 분) → EXP 큐 동안 노트북에서 World 고정·정물 축척·보정 문서·R1 묶음 → LIVE(+동시 녹화) → 검증 큐(v4)·남은 EXP → 녹화 재생 → 결과 받기(4차 1.85 GB 가 443 터널로 약 8분) → sha256 확인 → Terminate.
- 4차 결과 요약은 문서에 숫자를 옮기지 않고 Jira S15P31A101-293 2026-10-28 댓글을 본다.

### Vast.ai (예비)

Docker 실행 방식은 **Entrypoint** 로 둔다(SSH·Jupyter 방식은 이미지 진입점을 Vast 스크립트로 바꿔 `simai-start`·`simai-bootstrap` 이 돌지 않는다).
`SSH_PUBLIC_KEY`(계정 키, Vast 가 넣는다)로 `simai-start` 가 sshd 를 띄운다. 공개 주소·포트는 `PUBLIC_IPADDR`·`VAST_TCP_PORT_22`.
`HF_TOKEN` 은 Vast 계정 환경 변수로, 디스크는 100GB 이상, CUDA 13.0 이상 필터. 볼륨이 없으면 `/srv/simai` 다.

## GPU VM(엘리스) docker run

GPU VM + 블록 스토리지(엘리스 등 후보, 시연 사이트 미정). VM 의 호스트 sshd 로 들어가므로 컨테이너 sshd 는 끈다(`SIMAI_SSHD=off`).

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
# 3) Tier 0 기동: I0 수신 포트(제어·영상)를 같은 번호로 연다. 공개 IP·포트는 사이트 무관 이름 SIMAI_PUBLIC_HOST·SIMAI_PUBLIC_PORT_<번호> 로 넣는다(명세 3.12)
docker run -d --name simai-tier0 --restart unless-stopped --gpus all \
  -v /mnt/simai-block:/workspace -e SIMAI_ROOT=/workspace/simai -e SIMAI_SSHD=off \
  --env-file /mnt/simai-block/.simai-private/hf.env \
  -p <제어 포트>:<제어 포트> -p <영상 포트들>:<영상 포트들> \
  -e SIMAI_PUBLIC_HOST=<VM 공인 IP> -e SIMAI_TIER0_CONTROL_PORT=70000 -e SIMAI_PUBLIC_PORT_70000=<제어 포트> \
  -e SIMAI_TIER0_GPU_RESERVE_MB=8192 -e SIMAI_IMAGE_DIGEST=sha256:<다이제스트> \
  <이미지> --config /workspace/simai/tier0.json
docker logs -f simai-tier0      # simai-bootstrap: 저장·GPU·가중치 한 줄 → simai-tier0: 엔진 → Tier 0
```

- 블록 스토리지에 가중치·엔진이 이미 있으면 받지 않고(해시만 확인) 엔진 캐시를 쓴다. VM 을 바꿔도 같은 블록 스토리지를 붙이면 된다(A100 은 `sm_80` 경로).
- `--env-file` 은 호스트 docker 가 읽어 컨테이너 환경에만 넣는다. 부트스트랩이 가중치를 받은 뒤 Tier 0 를 띄우기 전에 지운다.
- TLS 키·기기 토큰 파일은 `/mnt/simai-block/.simai-private/` 에 두고 `SIMAI_TIER0_TLS_*_FILE=/workspace/.simai-private/...` 로 가리킨다.
- Tier 0 는 공개 주소를 `SIMAI_PUBLIC_HOST`·`SIMAI_PUBLIC_PORT_<번호>` 로 먼저 읽고, 없을 때만 RunPod 이 넣는 `RUNPOD_PUBLIC_IP`·`RUNPOD_TCP_PORT_<번호>` 를 읽는다(S15P31A101-208, 위 "공개 주소 환경 변수"). VM 에서는 사이트 무관 이름만 넣으면 된다.

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

실험 이미지(`exp02-fs3db`·`mapanything`)는 Tier 0 와 다른 Pod 로 띄운다(Pod 하나 = 이미지 하나, Tier 0 와 동시에 띄우지 않는다, LIVE 관제 시간 금지). 아래 "실험 이미지".

## 통합 이미지 (Tier 0 + vLLM + 분석 워커, S15P31A101-246)

`deploy/tier0/Dockerfile` 의 `unified` 타깃이다(`docker buildx bake unified` → `simai/unified:$TAG`). `tier0` 타깃과 같은 Dockerfile·같은 큰 층(CUDA 13 휠·torch 2.11.0·TensorRT libs·Tier 0 잠금) 위에
워커 층(vLLM 0.21.0 + vllm-cosmos3, HMR 추론 의존성)을 더하고 Fast-SAM-3D-Body @d72aa36·DINOv3 허브 코드 사본을 넣는다. 가상환경은 하나(`ai/uv.lock`, extras `tier0`+`worker`)이고 프로세스는 셋이다(명세 3.2).
설계·결정·잔여 위험은 `docs/design/unified-env.md` "2단계".

| 무엇 | 값 |
|---|---|
| 들어 있는 것 | `tier0` 이미지 전부 + vLLM·flashinfer·transformers 5.9·vllm-cosmos3, HMR 추론(sam-3d-body 가 import 하는 pytorch-lightning·pyrender·roma·timm 등, `hmr` extra), EGL·X11 런타임(libegl1·libgl1·libglvnd0…), triton 런처용 gcc, Fast·DINOv3 코드 사본(`SIMAI_FS3DB_REPO=/opt/simai/hmr/Fast-SAM-3D-Body`) |
| 넣지 않은 것 | 잠금 밖 휠 Detectron2·MoGe·chumpy(시연 HMR 경로가 쓰지 않는다: 렌더 패널·K 없는 그림의 FOV 예비·mhr2smpl 전용. exp02 이미지에만), `onnxruntime-gpu`·`cupy-cuda12x`(CUDA 12 빌드, 두 이미지 모두 뺐다. AutoCast 는 CPU `onnxruntime`), 가중치·엔진·비밀 |
| HMR 경로 | 보정 K → sam-3d-body 추론 → 메시·관절(`run_image.py --mode official --calibration <K> --no-render`). K 없는 그림·렌더 패널은 시작 전에 종료 코드 2(명확한 오류) |
| `NVIDIA_DRIVER_CAPABILITIES` | `compute,utility,video,graphics`(NVDEC 선택 디코더 + pyrender·나중의 텍스처 래스터화 EGL) |
| 기본 명령 | `simai-bootstrap tier0` + `SIMAI_SERVICES=tier0,vllm,worker`(아래 감시 프로세스). `SIMAI_SERVICES=tier0` 이면 `tier0` 이미지와 같은 Tier 0 단독. 이 ENV 는 `tier0` 감시 모드만 쓰고 `lobby`·`preflight` 는 보지 않는다(S15P31A101-414) |
| 가중치 | 기동 전 `tier0` 묶음(YOLO26x-pose)만, Tier 0 준비 뒤 `SIMAI_WORKER_WEIGHTS_SET`(기본 `vlm`, Cosmos3-Nano 18개 31.5GB). 워커 가중치가 실패해도 Tier 0 는 돈다 |
| MapAnything(S15P31A101-250) | 같은 가상환경(torch 2.11.0)에 map-anything @3d10cf7(잠금 밖 휠 `--no-deps`, 소스 사본 `SIMAI_MAPANYTHING_REPO=/opt/simai/mapanything/map-anything`)·DINOv2 허브 코드 @7764ea0. 의존성은 잠금 `scene` 묶음(uniception 0.1.7·rerun-sdk 0.24.1 등). 가중치(`static`)는 이미지에 없다. LIVE 와 따로 도는 준비 단계 일회성 작업이라 `simai-bootstrap scene` 으로만 돌린다(아래) |

### 감시 프로세스 (`SIMAI_SERVICES`)

`tini → simai-start → simai-bootstrap` 이 사라지지 않고 세 프로세스를 각각 새 프로세스 그룹으로 띄워 감시한다.

1. 준비(저장 위치·GPU·`tier0` 가중치) → `simai-tier0`(엔진 준비 → Tier 0). GPU VRAM 이 48GB급(45,000 MiB) 미만이면 vLLM·워커를 띄우지 않고 Tier 0 단독으로 exec 한다(명세 3.12).
2. Tier 0 표시 파일 `state/tier0.live` 가 `phase=running`·`reserve.state=OK`(simai-wait-tier0 와 같은 판정)이고 이번에 띄운 Tier 0 가 쓴 것(`gpu_priority.written_at` ≥ 기동 시각, `pid` 같음)이면 워커 가중치를 받는다(받는 동안에도 Tier 0 는 돈다). 이전 실행이 비정상 종료로 남긴 표시는 준비로 보지 않고 지우지도 않는다.
   **시작 순서(S15P31A101-577)**: 이때 분석 워커와 현장 세팅 가중치 받기를 바로 시작하고, 한 스레드가 아바타·PPE 묶음(통합 이미지는 구운 사본, 수 초) → VLM 묶음(31.5GB)을 차례로 준비한다. 아바타·PPE 는 자기 묶음이 끝나는 대로 뜨고 VLM 받기를 기다리지 않는다. vLLM 은 VLM 묶음 뒤에 뜬다. 워커는 vLLM 전에 돌지만 `SIMAI_WORKER_VLM_GATE=health` 로 `vlm_rationale`·`incident_summary` 는 vLLM `/health` 200 뒤에만 받는다(그 전에는 EC2 대기열에 남아 시도를 쓰지 않는다). VLM 받기가 실패하면 vLLM 만 포기하고(로그 `VLM 미준비`·기록 `worker-weights` 에 이유) 워커·아바타·PPE·Tier 0 는 계속 돈다. 아바타·PPE 묶음이 실패하면 그 둘만 띄우지 않는다.
3. vLLM 서버: `vllm serve $SIMAI_ROOT/weights/cosmos3-nano --served-model-name cosmos3-nano --hf-overrides '{"architectures": ["Cosmos3ReasonerForConditionalGeneration"]}' --gpu-memory-utilization $SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION(0.30) --max-num-seqs $SIMAI_WORKER_VLLM_MAX_NUM_SEQS(4) --max-model-len $SIMAI_WORKER_VLLM_MAX_MODEL_LEN(32768) --quantization $SIMAI_VLLM_QUANTIZATION(fp8) --host 127.0.0.1 --port $SIMAI_VLLM_PORT(8000) --mm-encoder-tp-mode data --async-scheduling $SIMAI_VLLM_EXTRA_ARGS`, `VLLM_USE_DEEP_GEMM=0`, `VLLM_USE_FLASHINFER_SAMPLER=0`, `HF_HUB_OFFLINE=1`. `/health` 가 200 이 될 때까지 기다린다(`SIMAI_VLLM_READY_TIMEOUT_S`, 기본 1800). GPU 가 cc 12.x(`SIMAI_GPU_ARCH` sm_120·sm_121)면 `--attention-backend TRITON_ATTN --mm-encoder-attn-backend TORCH_SDPA` 와 `VLLM_DISABLED_KERNELS=FlashInferFP8ScaledMMLinearKernel` 을 더한다(S15P31A101-720: 이 이미지의 FlashAttention 은 sm_80·sm_90a, FlashInfer 는 sm_100 커널뿐이고 nvcc 가 없다). `SIMAI_VLLM_EXTRA_ARGS` 에 `--attention-backend`(·`--attention-config`)·`--mm-encoder-attn-backend` 가 있거나 환경에 `VLLM_DISABLED_KERNELS` 가 있으면 그 값을 쓴다. H100(9.0)은 그대로.
   `--max-model-len` 이 없으면 모델 기본 262,144 토큰(한 요청 KV 36 GiB)이 남는 KV 를 넘어 기동이 실패하고, runtime 이미지에는 nvcc 가 없어 FlashInfer top-k/top-p 샘플러 JIT 가 실패한다(3차 GPU 세션, `docs/design/unified-env.md`). `SIMAI_VLLM_QUANTIZATION`(기본 `fp8`, S15P31A101-525)은 FP8 온라인 양자화(`--quantization fp8`)를 켜서 가중치 메모리를 `16.79 GiB → 10.05 GiB`(11-06 GPU PoC, 첫 응답 `1.73 → 1.57 s`, 한국어 판정·근거 품질 동등)로 줄인다. `SIMAI_VLLM_QUANTIZATION=none` 또는 빈 값이면 `--quantization` 을 넣지 않고, `SIMAI_VLLM_EXTRA_ARGS` 에 이미 `--quantization`(또는 `-q`)·`--max-model-len` 이 있으면 그 값을 쓰고 중복하지 않는다.
4. 분석 워커·아바타(S15P31A101-246·-382·-493·-525): `python -m services.worker $SIMAI_WORKER_ARGS` 와(`avatar` 선택 시) `python -m services.avatar`.
   - **GPU 메모리 상한(-493·-525)**: 분석 예산 `0.70` 에서 vLLM `0.30`·avatar `0.10` 을 빼 워커 `0.30`(H100 80GB = 81,559 MiB 기준 24,468 MiB ≈ 23.9 GiB), 아바타 미사용이면 `0.40`(32,624 MiB ≈ 31.9 GiB)을 쓴다. H100 의 vLLM `0.30`(24,468 MiB)은 FP8 가중치 `10.05 GiB` + 활성화 프로파일·CUDA graph(`0.03 GiB`)·torch 밖 메모리 약 `2.9 GiB` + KV 캐시 약 `10.9 GiB`(`79,536 토큰`, `--max-model-len 32768` 기준 동시 `2.43x`, 실사용 약 `2,900 토큰/요청` × 동시 4 대비 약 `6.7배`)를 담는다. KV 는 H200 에서 같은 절대 크기(비율 `0.1702`)로 띄운 2026-11-10 시험값이고 H100 실측은 아니다(H200 143,771 MiB 의 `0.30` 에서는 KV `29.07 GiB`, 동시 `6.46x`). FP8 은 가중치만 양자화하고 KV 캐시는 BF16 이다(명세 3.2 'VLM 메모리 항목', S15P31A101-580). 워커 상한은 `worker.env` 의 `SIMAI_WORKER_TORCH_MEMORY_FRACTION` > 프로세스 환경의 명시 값 > 계산값 순서이며, 합이 예산을 넘거나 워커 몫이 **최소 보장 비율 `0.10`(H100 80 GB 기준 ~8 GB, 12차 세션 MapAnything peak alloc 6.4 GB 대비 여유 약 1.6 GB)** 미만이면(예: 12차 세션처럼 워커 몫이 `0.05` ≈ 4 GB 로 줄어드는 구성) 기동 전 종료 코드 64 로 거부한다. 실행 직전 env 파일이 바뀌어 검증값과 다르면 워커를 띄우지 않는다. 아바타 실사용은 `4.0~5.1 GB` 라 상한 `0.10` 과 Tier 0 `0.20` 은 유지한다. 48GB급 이상 80GB 미만은 경고 후 실행(이 조합 미검증·OOM 가능), 48GB급 미만은 Tier 0 단독이다.
   - **토큰 파일 격리**: 워커 토큰 파일은 `SIMAI_WORKER_ENV_FILE`(또는 `SIMAI_WORKER_TOKEN_FILE` 로부터 생성한 `0600` `worker.env`, 없으면 `$SIMAI_ROOT/../.simai-private/worker.env`)을 `simai-wait-tier0 --env-file` 로 `worker`·`avatar` 환경에만 넣는다. Tier 0 토큰 파일(`SIMAI_TIER0_TOKEN_FILE`)은 `tier0` 환경에만 넣는다(명령줄·로그·기록·다른 자식 프로세스 환경에 노출하지 않는다).
5. PPE(`ppe` 선택 시, 명세 5.5, S15P31A101-507): Tier 0 준비 뒤 `ppe` 가중치 묶음(`SIMAI_PPE_WEIGHTS_SET`, 기본 `ppe`)을 아바타 묶음과 함께(VLM 받기와 따로, S15P31A101-577) 준비하고 `python -m services.ppe` 를 띄운다. 명시해야 켜지고 `lobby` 기본(`tier0,vllm,worker,avatar`)·`preflight` 묶음에는 넣지 않았다(G9 동시 실행 판정 전). **GPU 자동 부팅(`simai-gpu-boot`, VRAM 70,000 MiB 이상 = 80GB급·96GB급)은 `ppe` 를 더한 `tier0,vllm,worker,avatar,ppe` 를 기본으로 띄운다**(48GB급은 여유가 2 GiB 남짓이라 lobby 기본 그대로, 단계 보고에 'ppe 꺼짐: VRAM 부족')(S15P31A101-718, 작업 설정의 안전모·조끼 필수 감시). 아바타·PPE 가중치는 따로 준비해 한쪽이 실패해도 다른 쪽은 뜨고, PPE 가 죽으면 PPE 만 백오프 뒤 다시 띄운다(Tier 0·워커·아바타는 그대로). R9·R11 시연 예외(S15P31A101-556) 기간에는 lobby 에 `--services tier0,vllm,worker,avatar,ppe` 를 주고 `simai-bootstrap check --sets ppe` 로 가중치를 먼저 확인한다. 기동·수신 확인 항목은 [`docs/design/ppe-demo-activation.md`](../docs/design/ppe-demo-activation.md) '배포와 현장 검증' 5단계에 있다(S15P31A101-587).
   - **입력·출력**: Tier 0 환경에 `SIMAI_AVATAR_SHARE=1`(아바타 입력 공유 파일 `/dev/shm/simai-avatar-input.npz`, 아바타가 꺼져 있어도 켠다)과 `SIMAI_PPE_RELAY=1` 을 넣는다. PPE 는 그 공유 파일을 읽기만 하고, 만든 `ppe_state` 봉투를 outbox(`SIMAI_PPE_OUTBOX`, 기본 `/dev/shm/simai-ppe-outbox`)에 쓴다. Tier 0 가 그것을 자기 I1 연결(`source_id` 그대로)로 보낸다. PPE 는 I1 연결·워커 토큰·HF 토큰을 쓰지 않는다(같은 `source_id` 연결이 둘이면 EC2 의 운영자 명령이 PPE 쪽으로 갈 수 있다).
   - **GPU 메모리 상한**: `SIMAI_PPE_GPU_MEMORY_FRACTION`(기본 0.05)은 분석 워커 예산(0.70) 밖의 별도 몫이다. Tier 0 + 분석 워커 + PPE ≤ 0.95 를 넘으면 기동 전 64(명세 3.2. 초기값 0.20 + 0.70 + 0.05 는 여유 0, G0 실측으로 다시 정한다). PPE 환경에는 체크포인트 경로·sha256(`SIMAI_PPE_WEIGHTS`·`SIMAI_PPE_WEIGHTS_SHA256`)을 넣고, PPE 는 불러오기 전에 sha256 을 다시 확인한다.
   - **추론 간격(S15P31A101-729)**: 카메라별 추론 시작 간격 하한은 `SIMAI_PPE_MIN_INFER_GAP_MS`(기본 500, 400~500, 범위 밖이면 시작 실패 2)다. 같은 0.5초 촬영 칸은 늘 한 번만 추론하므로 평균은 카메라당 2 Hz 이하다. PPE 상태 로그(60초마다 `[ppe]` `PPE 상태`)의 `timing.bins.cameras.<카메라>.bins_missed` 가 `inferred_bins` 의 5 % 를 넘으면 400~450 으로 낮추고 PPE 를 다시 띄운다(컨테이너 환경이 PPE 프로세스로 그대로 넘어간다).
   - **접수 문**: PPE 는 Tier 0 표시 파일의 지연 신호(3.2 접수 규칙)가 모름·초과면 새 추론을 멈추고 안정 뒤 재개한다. 종료는 Tier 0 보다 먼저다(`ppe → avatar → worker → vllm`).

- vLLM·워커가 죽으면 그 그룹을 SIGKILL 로 정리하고(vLLM EngineCore 같은 손자까지, GPU 메모리를 남기지 않게) 백오프(`SIMAI_SUPERVISE_BACKOFF_S` 5초 → 최대 300초) 뒤 **Tier 0 가 준비 상태일 때만** 다시 띄운다. 워커·아바타는 빠른 실패가 이어져도 최대 300초 간격으로 계속 재시도한다. vLLM은 `SIMAI_SUPERVISE_MAX_FAST_FAILURES`(5)번 뒤 포기하고 워커를 VLM 없이 시작한다. Tier 0 에는 신호를 보내지 않는다.
- Tier 0 가 끝나면 워커 → vLLM 을 내리고 Tier 0 종료 코드 그대로 컨테이너가 끝난다(VM 은 `--restart unless-stopped` 가 컨테이너를 다시 띄운다).
- `docker stop`·RunPod Stop(SIGTERM)은 워커 → vLLM → Tier 0 순서로 그룹 SIGTERM, 각 `SIMAI_STOP_GRACE_S`(15초) 안에 안 끝나면 그룹 SIGKILL. 셋 합쳐 최대 45초라 `docker run --stop-timeout 60`(또는 `docker stop -t 60`)으로 띄운다.
- 출력은 줄마다 `[tier0]`·`[vllm]`·`[worker]` 접두로 `docker logs` 에, 원문은 `$SIMAI_ROOT/logs/<이름>.log` 에 덧붙인다(회전 없음).
- vLLM·워커는 `OMP_NUM_THREADS`·`MKL_NUM_THREADS`·`OPENBLAS_NUM_THREADS`·`NUMEXPR_NUM_THREADS`=`SIMAI_SIDECAR_THREADS`(1, torch intra-op 도 OMP 를 따른다), nice `SIMAI_SIDECAR_NICE`(10),
  CPU 고정 `SIMAI_SIDECAR_CPUS`(기본 `auto`: 쓸 수 있는 vCPU 수 n = min(affinity 개수, cgroup CPU 할당량의 올림)이 8 이상이면 affinity 목록의 뒤쪽 n//2 개에 묶고 앞쪽은 Tier 0 몫, 8 미만이면 묶지 않음. 할당량은 cgroup v2 `cpu.max`·v1 `cpu.cfs_quota_us/cpu.cfs_period_us`(잎·조상 중 가장 작은 값), 못 읽거나 제한 없음이면 affinity 개수. RunPod H100 Pod 는 affinity 224·할당량 23.8 이라 뒤쪽 12개(212-223)에 묶는다(할당량은 컨테이너 전체가 나눠 쓰므로 affinity 절반 112개에 묶으면 제한이 되지 않는다). `none`, `4-7,10`). 계산 결과(affinity 개수·할당량·묶은 범위)는 감시 프로세스 시작 로그 한 줄에 남는다. Tier 0 는 묶지 않는다.
  세 프로세스 모두 `TRITON_CACHE_DIR`·`TORCHINDUCTOR_CACHE_DIR`·`CUDA_CACHE_PATH` 가 `SIMAI_CACHE_ROOT`(기본 `/tmp/simai-cache`)`/<이름>/` 로 나뉜다. HF 토큰은 어느 자식에도 넘기지 않는다.
- vLLM 은 Tier 0 의 TensorRT 엔진 빌드·적재와 GPU 선확보가 끝난 뒤에 뜬다: 진입점이 엔진을 준비한 뒤에야 Tier 0 서비스가 시작해 표시 파일을 쓰고, 감시자는 `phase=running`·`reserve.state=OK` 를 본 뒤에 워커 쪽을 시작한다(엔진 빌드 중 vLLM 이 메모리를 먼저 잡아 빌드가 실패하는 일을 막는다).
- 시험·점검용 덮어쓰기: `SIMAI_TIER0_CMD`·`SIMAI_VLLM_CMD`·`SIMAI_WORKER_CMD`(셸 따옴표 규칙), `SIMAI_TIER0_MARKER`, `SIMAI_VLLM_MODEL`.

### 로비 LIVE 통합 기동 (`simai-bootstrap lobby`) 및 기동 전 점검 (`preflight`) (S15P31A101-382)

9차 세션에서 수동 스크립트(`chain-s9.sh`, `static-s9.sh`, `live-s9.sh`, `avatar-s9.sh`, `worker-s9.sh`, `lobby_live_s9.py`)를 여러 셸로 쪼개 띄우면서 발생했던 문제(GPU 메모리 상한 155% 초과, CPU 친화도·스레드 제한 누락, Tier 0 비정상 종료 시 `return 0` 반환, 기동 전 점검 실패 무시)를 `simai-bootstrap` 단일 경로로 통합했다.

```sh
# 1) 기동 전 일괄 점검 (tier0,avatar,static,vlm 가중치 + GPU 메모리 예산 + texrecon 바이너리 + open3d·ultralytics 모듈)
#    static 의 MapAnything 은 SIMAI_MAPANYTHING_MODEL 한 판만(기본 weights/map-anything, apache 판은 받지 않음, S15P31A101-577)
#    하나라도 실패하면 비-0 종료 코드(2·3·64)로 즉시 실패한다.
simai-bootstrap preflight

# 2) 관제 전 정적 장면 1회 재구성 (Tier 0 LIVE 가 없을 때만 실행되고 Tier 0 시작 시 75 로 선점, 토큰은 파일에서만 읽음)
SIMAI_EC2_BASE_URL=https://<EC2> SIMAI_WORKER_ID=worker-static \
  simai-bootstrap scene --worker-token-file /root/.simai_worker_token -- \
  python -m services.worker --once --wait-ms 5000

# 3) 로비 LIVE 통합 기동 (Tier 0 + vLLM + worker + avatar 를 단일 Supervisor 로 기동)
#    - endpoint.json(TLS SPKI sha256)·tier0-live.json·pod.jsonl 자동 생성
#    - 정적 장면이 이미 준비된 경우 --no-clean-background 로 배경 표본 수집(2.2 GiB RSS·중앙값 계산) 비활성화
#    - 토큰은 --tier0-token-file / --worker-token-file 에서만 읽고 명령줄·로그·다른 프로세스 환경에 노출하지 않음
#    - Tier 0 종료 코드를 삼키지 않고 그대로 반환
simai-bootstrap lobby \
  --work /workspace/simai/runs/lobby-live \
  --bundle-dir /workspace/simai/bundles/lobby-s8 \
  --public-host <공개 IP> \
  --ec2-base-url https://<EC2> \
  --tier0-token-file /root/.simai_tier0_token \
  --worker-token-file /root/worker-s10.token \
  --no-clean-background
```

10차 세션에서 고친 것(S15P31A101-414):

- **프로세스.** `lobby` 는 `--services`(기본 `tier0,vllm,worker,avatar`)만 따르고 `SIMAI_SERVICES` 는 보지 않는다. 통합 이미지 ENV 기본값과 10차 템플릿 값이 아바타 없는 `tier0,vllm,worker` 라 그 값이 lobby 기본값을 덮어 시연 필수인 아바타가 빠졌다(이미지 기본값과 일부러 뺀 값을 구분할 수 없어 "따르되 경고"가 아니라 "무시하고 경고"를 골랐다). 환경 값이 다르면 시작 로그에 `주의: SIMAI_SERVICES=… 는 lobby 에서 쓰지 않는다`, `--services` 로 아바타를 빼면 `주의: 아바타를 띄우지 않는다` 를 남긴다. `preflight` 의 GPU 예산도 같은 기본(아바타 포함)으로 본다.
- **워커 ID.** `--worker-id` → `SIMAI_WORKER_ID` → 토큰·env 파일 안의 `SIMAI_WORKER_ID=` → 토큰 파일 이름(`worker-s10.token` → `worker-s10`, EC2 규칙 `^[a-z0-9][a-z0-9-]{0,62}$` 에 맞고 `worker.token` 같은 흔한 이름이 아닐 때) 순서로 정해 `worker`·`avatar` 환경 `SIMAI_WORKER_ID` 로 넣고, 시작 로그에 `워커 ID <값> (<출처>)` 를 남긴다. 못 정하면 워커가 기본 `worker-01` 로 뜬다는 경고를 남긴다. 10차: 토큰 파일만 주고 ID 를 안 넘겨 워커가 `worker-01` 로 떠 EC2 대여가 401 → 빠른 실패가 반복됐다. 워커는 401·403 이면 `워커 ID 와 토큰이 맞지 않음(현재 ID=…)` 을 남긴다(토큰 값은 찍지 않는다). 그래서 EC2 에서 `admin worker register --id <ID>` 로 받은 토큰은 `<ID>.token` 이름으로 올린다.

### 장면 준비 모드 (`simai-bootstrap scene`, MapAnything, S15P31A101-250)

정적 장면 준비(MapAnything 초기 추정, 명세 4.3·5.x)는 설치 때 한 번 도는 작업이다. LIVE 서비스(Tier 0·vLLM·워커)와 같은 GPU 에서 동시에 돌리지 않고, 명령이 끝나면 프로세스가 끝나 VRAM 을 돌려준다.

```sh
# 관제 시간 밖, 감시 프로세스를 띄우지 않은 컨테이너(Pod 는 시작 명령 sleep infinity)에서
simai-bootstrap scene -- python <사본>/ai/experiments/static_scene/run_mapanything.py --run-id <id> --mode prepared --prepared <입력 폴더>     --model $SIMAI_ROOT/weights/map-anything-apache --runs-dir $SIMAI_ROOT/runs/exp01
# VM: docker run --rm --gpus all -v /mnt/simai-block:/workspace -e SIMAI_ROOT=/workspace/simai -e SIMAI_SSHD=off <통합 이미지> scene -- python ...
```

1. LIVE 확인(받기 전): Tier 0 표시 파일(`SIMAI_TIER0_MARKER`, 기본 `$SIMAI_ROOT/state/tier0.live`)이 30초 안에 갱신됐거나 확인하지 못하면, 또는 GPU 사용 중 메모리가 1024 MiB 초과면 종료 코드 75(못 읽는 것은 2 의 GPU 단계가 판정).
2. 준비: 저장 위치·GPU·가중치 `static` 묶음(`SIMAI_WEIGHTS_SET` 과 상관없음). MapAnything 은 한 판만: 명령에 `--model <판의 폴더>` 가 있으면 그 판, 없으면 `SIMAI_MAPANYTHING_MODEL`(기본 `weights/map-anything`, 현장 세팅 워커와 같음, S15P31A101-577). 통합 이미지에서는 구운 사본을 확인해 링크한다. 실패하면 2·3.
3. 재확인: 표시 파일(받는 동안 Tier 0 가 떴을 수 있다)과 GPU 사용 중 메모리(`nvidia-smi memory.used` 가 1024 MiB 초과, 못 읽음, NaN 등 → 75: 표시 파일로 안 보이는 Tier 0 엔진 빌드·REPLAY·vLLM·다른 컨테이너). 거부하면 `runs/bootstrap/` 에 exit 75 기록을 추가한다.
4. 그 명령을 새 프로세스 그룹 자식으로 실행한다(출력은 `[scene]` 접두, 원문 `$SIMAI_ROOT/logs/scene.log`). 종료 코드는 명령의 것이고 끝나면 그룹을 정리해 VRAM 을 돌려준다. HF 토큰은 2 의 받기에만 쓰고 명령에 넘기지 않는다. 실행 중 Tier 0 가 시작하면 아래처럼 멈춘다.

- `SIMAI_SERVICES` 에 `scene` 을 넣으면 사용법 오류 64(장면 준비는 서비스가 아니다). 이미지 기본 `SIMAI_SERVICES=tier0,vllm,worker` 는 `scene` 명령이 보지 않는다.
- **Tier 0 와 상호 배제(Tier 0 우선, Codex 리뷰 R1).** `tier0` 명령은 GPU 를 잡기 전(가중치 준비 전)에 `$SIMAI_ROOT/locks/tier0.lock` 공유 flock 을 쥐고 exec 를 지나 Tier 0 프로세스가 끝날 때까지 쥔다(커널이 프로세스 종료 때 푼다). 장면 모드는 `locks/scene.lock` 을 쥐고 명령을 새 프로세스 그룹 자식으로 돌리며 0.5초마다 tier0.lock·표시 파일을 본다. Tier 0 가 나타나면 장면 그룹 SIGTERM → 10초 → SIGKILL, 기록 75. Tier 0 시작 경로는 exec 전에 scene.lock 이 풀리기를 최대 20초 기다리고(장면 작업이 VRAM 을 돌려줄 때까지) 남은 그룹을 회수한 뒤(최악 약 40초) 시작한다. 감시 부모가 멈추거나 죽어도 장면 작업이 남지 않게(Codex 재리뷰 R1): 장면 모드는 자식 그룹을 `locks/scene.pgid`(pgid·시작 표지, 원자적 쓰기)에 남기고, Linux 에서는 자식에 `PR_SET_PDEATHSIG`(SIGKILL)를 건다. Tier 0 시작 경로는 잠금이 풀렸거나(부모 소멸) 시한이 지나도(부모 정지) 그 그룹이 살아 있으면 직접 SIGTERM → 10초 → SIGKILL 로 회수하고 사라진 것을 확인한 뒤 진행한다(잠금 해제·시한을 VRAM 반환으로 보지 않는다). 시작 표지가 다르면(pid 재사용) 건드리지 않고, 회수하지 못하면 기록(`stage: tier0-reap-scene`)하고 진행한다(Tier 0 우선). **의도된 정책(Codex 3차 재리뷰, Lead 결정): Tier 0 가용성이 장면 작업보다 우선이다.** 회수하지 못하거나(신호 거부·시한 초과) 건드리지 않은 경우(아래) Tier 0 는 막지 않고 큰 경고 로그와 부트스트랩 기록(`stage: tier0-reap-scene`, `exit: 75`)을 남긴 뒤 시작한다. Tier 0 를 막는 쪽(관제 불가)이 장면 작업과 GPU 를 잠깐 나누는 쪽보다 위험하다고 본다. **모르면 건드리지 않는다**: 기록의 PID namespace(`/proc/self/ns/pid`)가 Tier 0 쪽과 다르거나 모르면, 또는 시작 표지가 기록·지금 중 하나라도 없으면 신호를 보내지 않고 기록을 남긴다. KILL 전에 시작 표지를 다시 본다. **같은 컨테이너 전제**: 상호 배제·회수는 통합 이미지의 같은 컨테이너(같은 PID namespace) 안에서 장면 모드를 돌리는 것을 전제로 한다(`docker exec` 로 같은 컨테이너, Pod 는 SSH 로 같은 컨테이너). 다른 컨테이너의 장면 작업은 잠금으로 보이지만 신호는 보내지 않는다(그 컨테이너가 맡는다). 자식은 시작 handshake(POSIX: 표준 입력에서 1바이트를 받은 뒤에만 exec)로 그룹 기록이 공개된 뒤에만 장면 명령을 시작하고, 기록을 못 쓰면 시작시키지 않고 75. `/proc` 를 읽을 권한이 없는 것(EACCES·EPERM)은 살아 있음(모름), 항목이 없음(ENOENT·ESRCH)만 끝남으로 본다. Tier 0 는 최악 약 40초(잠금 20초 + TERM 10초 + 확인 10초) 늦게 시작할 수 있다. 잠금을 열지 못해도 Tier 0 는 막히지 않는다(장면 쪽은 모름 → 거부). 장면 준비는 그래도 관제 시간 밖에서 Tier 0 를 띄우기 전에 끝낸다.
- torch 2.7.1 이미지(`mapanything`, 아래 "실험 이미지")는 두 환경의 결과 비교(3차 세션 u3·u4 ↔ m1·m2, `ai/experiments/static_scene/compare_envs.py`)가 끝날 때까지 남긴다.

### 사이트 조건 (통합 이미지)

| 항목 | 조건 |
|---|---|
| GPU | 하나. 80GB 권장(H100·A100 80GB), 48GB급 미만이면 Tier 0 단독으로 뜬다. Ampere 이후. VLM 기본은 `0.30`·FP8 가중치 10.05 GiB(31.5GB 는 받는 파일 크기, VRAM 이 아님, 명세 3.2). 이전 기본 `0.55`·BF16 의 H100 실측(3차 GPU 세션)은 가중치 16.79 GiB + KV 23.99 GiB(174,688 토큰)였다. 48GB급은 미확인(명세 19.1). 추정: 전체 약 44~48 GiB × 0.30 ≈ 13~14 GiB − 가중치 10.05 GiB − 실행 시 추가 약 2.9 GiB ≈ KV 0.2~1.4 GiB 로, `--max-model-len` 32,768 요청 하나에 필요한 약 4.5 GiB(H200 시험의 약 7,280 토큰/GiB 기준)에 못 미쳐 기본 인자로는 vLLM 이 뜨지 않을 수 있다 |
| 드라이버 | 최대 CUDA 13.0 이상(RunPod·Vast 의 CUDA 필터 13.0). VM 은 NVIDIA Container Toolkit, `graphics` 기능이 컨테이너에 올라오는지(`ls /usr/share/glvnd/egl_vendor.d/` 에 `10_nvidia.json`) |
| 디스크 | 이미지(풀면 아래 "빌드 결과", 시연 가중치 약 13.4GB 포함) + 받는 가중치 약 31.5GB(Cosmos3-Nano. 나머지는 이미지 사본에 링크) + 엔진·저널·로그. 컨테이너 디스크면 150GB 이상, 시연은 인스턴스를 다시 켜도 남는 디스크(명세 3.12) |
| 포트 | 22(SSH, 키 인증만), I0 수신 포트(공개 TCP 17000·17001 노출, 매핑은 `RUNPOD_TCP_PORT_17000`·`_17001` 또는 대칭 매핑). vLLM 8000 은 127.0.0.1 만(공개하지 않는다) |
| 메모리·CPU | vCPU 8개 이상(명세 3.12 초기값). Tier 0 CPU 디코딩과 vLLM 전처리가 같은 CPU 를 쓴다. 3차 GPU 세션 실측(Pod cgroup 상한 23.8 vCPU): VLM 동시 4 지속 중 Tier 0 CPU 142 → 192%, loadavg 최대 85, `frame_age` p95 60 → 76 ms. cgroup 기준 새 CPU 묶기(S15P31A101-278)는 4차 세션 v4 가 다시 잰다 |

**저장 위치 전제.** 시연 준비·리허설·시연은 인스턴스를 다시 켜도 남는 디스크(`$SIMAI_ROOT`: 블록 스토리지·Pod 볼륨)에 가중치(약 32GB)·TensorRT 엔진 캐시·저널을 둔다(명세 3.12).
그래야 재시작 때 가중치를 다시 받지 않고(검증 스탬프로 다시 해시도 않음) 엔진을 다시 만들지 않는다(H100 약 550초). 컨테이너 디스크(`/srv/simai`)는 개발·시험용이다.

### Pod형 (RunPod 템플릿)

위 "RunPod 템플릿" 과 같고 다음만 다르다. 이미지 `<레지스트리>/<프로젝트>/tier0@sha256:<unified 다이제스트>`(무료 플랜 저장소 하나라 태그 접두 `unified-`), Container Disk 150GB 이상,
환경 변수에 `HF_TOKEN={{ RUNPOD_SECRET_hf_token }}`(Cosmos3-Nano 는 승인제가 아니지만 HF 속도 제한 완화), 분석 워커 값(`SIMAI_WORKER_*`, 위 표), 필요하면 `SIMAI_WORKER_ARGS`.
`SIMAI_SERVICES` 는 템플릿에 넣는다면 `tier0,vllm,worker,avatar`(아바타는 시연 필수. 이 값은 시작 명령을 비운 `tier0` 감시 모드만 쓰고 `simai-bootstrap lobby` 는 보지 않는다, S15P31A101-414). `SIMAI_WORKER_ID` 는 템플릿에 두지 않고 lobby 의 `--worker-id` 또는 토큰 파일 이름(`<워커 ID>.token`)으로 준다(세션마다 새 워커를 등록한다).
시작 명령은 비운다(이미지 기본 = 감시 프로세스). 검증 세션은 `sleep infinity` 로 띄우고 SSH 로 `SIMAI_WEIGHTS_SET=exp02 simai-bootstrap queue`(이 이미지의 큐는 a·b·t·c·d·u1·u2·u3·u4, 아래 "Pod 검증 큐". u3·u4 는 장면 준비 모드로 `static` 가중치를 따로 받는다). 통합 이미지에는 `SIMAI_WEIGHTS_SET` 이 없어(기본 tier0) 그대로 부르면 u2(HMR 보정 K)·`simai-exp02` 가 sam-3d-body 체크포인트 없음(종료 코드 2)으로 실패한다. HMR 만 따로 돌릴 때도 `SIMAI_WEIGHTS_SET=exp02 simai-bootstrap run simai-exp02 python <사본>/ai/experiments/hmr/run_image.py --mode official --calibration <K> --no-render ...`(저장소 사본 코드를 이미지 패키지보다 먼저 쓰려면 `PYTHONPATH=<사본>/ai/src:<사본>/ai`, Tier 0 file 모드 녹화 재생은 `<사본>/ai/` 에서 `simai-bootstrap run simai-tier0 --config <json>`).
RunPod Stop 이 SIGTERM 뒤 얼마를 기다리는지는 문서로 확인하지 못했다: 저널은 기록마다 fsync 라(명세 3.2) 강제 종료돼도 재시작 복구가 된다.

### VM형 (엘리스 등 GPU VM + 블록 스토리지, `docker run --gpus all`)

```sh
# 0)~1) 위 "GPU VM(엘리스) docker run" 과 같다(드라이버·Toolkit 확인, 블록 스토리지 /mnt/simai-block, hf.env)
# 2) 준비만: Tier 0 묶음 + 워커 묶음(31.5GB, 블록 스토리지에 한 번 받으면 다음부터 검증 스탬프로 다시 해시하지 않는다)
docker run --rm --gpus all -v /mnt/simai-block:/workspace -e SIMAI_ROOT=/workspace/simai -e SIMAI_SSHD=off \
  --env-file /mnt/simai-block/.simai-private/hf.env -e SIMAI_WEIGHTS_SET=vlm <통합 이미지> check
# 3) 기동: Tier 0 → (준비) → 워커·아바타 바로, VLM 받은 뒤 vLLM(S15P31A101-577). 정지 시한 60초
docker run -d --name simai --restart unless-stopped --stop-timeout 60 --gpus all \
  -v /mnt/simai-block:/workspace -e SIMAI_ROOT=/workspace/simai -e SIMAI_SSHD=off \
  --env-file /mnt/simai-block/.simai-private/hf.env \
  -p <제어 포트>:<제어 포트> -p <영상 포트들>:<영상 포트들> \
  -e SIMAI_PUBLIC_HOST=<VM 공인 IP> -e SIMAI_TIER0_CONTROL_PORT=70000 -e SIMAI_PUBLIC_PORT_70000=<제어 포트> \
  -e SIMAI_TIER0_GPU_RESERVE_MB=8192 -e SIMAI_IMAGE_DIGEST=sha256:<다이제스트> \
  -e SIMAI_EC2_BASE_URL=https://<EC2> -e SIMAI_WORKER_ID=worker-01 \
  <통합 이미지> --config /workspace/simai/tier0.json
docker logs -f simai     # [tier0] … → simai-bootstrap: Tier 0 준비됨 → [worker] … → 워커 가중치 vlm 준비 → [vllm] … 준비됨
```

Compose 다중 컨테이너로 나누지 않는다(Pod형과 같은 단일 컨테이너, 자문 1). Tier 0 만 띄울 때는 `-e SIMAI_SERVICES=tier0`.

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
- 동적 프로파일은 `detection_baseline` 과 같다(min 1×3×32×32, opt 4×3×736×1280, max 4×3×1280×1280). 빌드는 `simplify=False`.
- Ultralytics 8.4.178 의 TensorRT 내보내기는 FP16 변환(ModelOpt AutoCast)에 `nvidia-modelopt[onnx]>=0.44` 를 요구한다. 없으면 `No module named 'modelopt'` 로 엔진 빌드가 실패하므로(GPU 1차 세션) `tier0` extra 에 넣었다. 잠금은 Pod 에서 엔진 빌드·추론이 통과한 조합(nvidia-modelopt 0.47.0, onnx 1.21.0)이다(S15P31A101-213). CUDA 12 빌드 onnxruntime-gpu·cupy-cuda12x 는 빼고 CPU onnxruntime 을 쓴다(S15P31A101-246, AutoCast 참조 실행은 CPU). 엔진 메타데이터 키에 torch·ModelOpt 판도 들어간다. `check_image.sh` 가 `import modelopt` 를 확인한다.

## 실험 이미지 (EXP-02 HMR, EXP-01 MapAnything, S15P31A101-207)

볼륨 venv(`ai/experiments/{hmr,static_scene}/setup_runpod.sh`)를 지역·GPU 가 바뀌어도 그대로 도는 이미지로 옮겼다. 설계·근거·E-GATE-0 대조 기준은 `docs/design/exp-images.md`.

| | `exp02-fs3db` (HMR) | `mapanything` |
|---|---|---|
| 베이스 | `nvidia/cuda:12.8.1-base-ubuntu24.04` + Python 3.11.15. 빌드 단계도 같은 base(devel·nvcc 없음, Detectron2 CPU 전용 휠만 만든다) | 같은 CUDA base + Python 3.12.13 |
| torch | 2.7.1+cu128 (볼륨 venv 2.5.1+cu124 → Blackwell 커널이 있는 판) | 2.7.1+cu128 (볼륨 venv 2.7.1+cu126 과 같은 판, CUDA 빌드만) |
| GPU 세대 | torch 휠 SASS sm_75·80·86·90·100·120 + PTX 12.0(cuobjdump 로 확인, 8.9 는 8.6 SASS). Detectron2 는 CUDA 커널 없음(CPU 전용 확장, LazyConfig 만 쓴다) (torch: 같은 주 버전의 높은 부 버전 8.7·10.3·12.1 은 SASS 호환) | torch 휠과 같다 |
| 코드 | Fast-SAM-3D-Body @d72aa36 → `SIMAI_FS3DB_REPO`, DINOv3 허브 코드 → `~/.cache/torch/hub/facebookresearch_dinov3_main` | map-anything @3d10cf7 → `SIMAI_MAPANYTHING_REPO`, DINOv2 허브 코드 → `~/.cache/torch/hub/facebookresearch_dinov2_main` |
| 가중치 묶음 | `exp02`: yolo26x-pose, sam-3d-body-dinov3 `model.ckpt`·`assets/mhr_model.pt`, MoGe2 vits·vitl | `static`: map-anything·map-anything-apache (`model.safetensors`·`config.json`) |
| 기본 명령 | `sleep infinity`(SSH 로 들어가 쓰는 개발 Pod, 준비 없음) | 같다 |

```sh
# 준비(저장 위치·GPU·가중치 sha256) → 실행. HMR 은 simai-exp02 가 fastcfg 링크(Fast 저장소 config + 같은 가중치)를 만들고 체크포인트를 확인한다
simai-bootstrap run simai-exp02 python <사본>/ai/experiments/hmr/run_image.py --run-id ... --images ...
simai-bootstrap run python <사본>/ai/experiments/static_scene/run_mapanything.py --run-id ... --images ... --model $SIMAI_ROOT/weights/map-anything-apache
simai-bootstrap queue --session <이름>     # 2차 세션 큐: 이 이미지의 항목(g0~g4 또는 m0~m2)만 돈다
```

- HF 토큰은 `sam-3d-body-dinov3`(승인제)에만 필요하다. Tier 0 와 같이 `HF_TOKEN` 환경 변수로만 받고 자식 프로세스에 넘기지 않는다.
- `sam-3d-body-dinov3-mhr` 의 sha256 은 저장소 `deploy/weights.json` 에 채워져 있다(2026-10-21, `docs/data/weights.md`). 이미 빌드한 이미지 `jonghwa1492/tier0:*-3926a8772649` 안의 목록(`/opt/simai/deploy/weights.json`)에만 비어 있어,
  그 이미지로 exp02 를 돌릴 때는 `SIMAI_WEIGHTS_MANIFEST=<사본>/deploy/weights.json` 으로 저장소 목록을 넘긴다. 넘기지 않으면 HMR 이미지의 `check`·`queue`·`run` 이 받기 전에 종료 코드 2 로 멈춘다. 다음 이미지 빌드부터는 이미지 안 목록에 들어 있다.
- MoGe2 는 Fast 코드가 HF 저장소 이름으로 부르지만 `fs3db_common.use_local_moge()` 가 확인된 파일(`weights/moge/<이름>/model.pt`)로 바꿔 끼운다(없으면 원래 이름: 볼륨 venv 환경).
- 볼륨 venv 와의 차이: conda(micromamba) 대신 uv, MoGe @74fbce0 는 `--no-deps`(flex-gemm·gradio 제외, 볼륨 환경도 MoGe 뒤 torch 를 되돌려 같은 상태), torch.hub 코드는 커밋 고정.
- `run_mapanything.py` 의 `env_hash` 는 `SIMAI_IMAGE_DIGEST` 가 있으면 그 값(없으면 예전처럼 볼륨 잠금 파일 해시)이다.

잠금 갱신(`requirements.in` 을 고쳤을 때, 각 폴더에서. 기존 잠금의 판을 선호하므로 바꾼 것만 움직인다):

```sh
uv pip compile requirements.in --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes -o requirements.lock.txt   # deploy/exp02
uv pip compile requirements.in --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes -o requirements.lock.txt   # deploy/mapanything
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

실험 이미지 (2차 세션, Tier 0 Pod 를 Stop 한 뒤 각 이미지로 Pod 를 띄운다. 큐 항목·대조 기준은 `docs/design/runpod-environment.md` "2차 세션"):

```sh
# 노트북: 사본은 Tier 0 와 같은 prepare 출력. 데이터는 저장소에 없으므로 따로 올린다(공개 영상 프레임 14장, 라운지 사진 4장)
scp -P <포트> -r <hmr_poc14 폴더> root@<IP>:/srv/simai/datasets/hmr_poc14     # exp02 Pod
scp -P <포트> -r <room4 폴더> root@<IP>:/srv/simai/datasets/room4             # mapanything Pod
# exp02 Pod g4(보정 K 입력 경로): prepare 가 저장소 밖 --out 에 만든 g4-inputs.tar(교실 사진 20장 + 보정 기록)를 prepare 출력대로 올려
# datasets/ 에 푼다. 사람이 찍힌 사진이라 내부 확인용이다(공유·커밋하지 않는다)
# Pod: 가중치 받기·sha256 확인 → 이 이미지 항목만 큐 (a 점검 + g0~g4 또는 m0~m2). 끝나면 출력된 scp 로 결과 묶음을 가져오고 Stop·Terminate
nohup simai-bootstrap queue --session <세션> > /srv/simai/setup/session1/session2-<이미지>.log 2>&1 &
```

E-GATE-0 H0·H1·H6, C1·C2 와 `docs/design/exp-images.md` "E-GATE-0 대조 기준" 으로 비교한 뒤, 이후 EXP Run 은 이 이미지 다이제스트를 `env_hash` 로 기록한다.


맵 재생성은 기본적으로 LIVE 표시가 있는 동안 접수 대상에서 제외되며 워커 로그에 이유가 남는다. 작업 종료 후에도 상시 안전 감시가 계속되므로 작업 종료만으로 LIVE 종료가 아니다. 관제를 정상 종료한 뒤 `simai-bootstrap after-live`로 워커만 띄우고 요청한다. LIVE 중 허용 여부는 명세 5.3·19.1에서 실측 후 결정할 미결 사항이다. 서버·화면에는 현재 이 대기 이유가 표시되지 않아 안내 개선이 필요하다.

## EC2 배포 동결 (S15P31A101-582)

GPU 세션 전에 EC2 호스트에서 저장소의 `deploy/ec2/deploy.sh`를 `/opt/simai/bin/deploy.sh`에 다시 설치하고 `freeze.sh`도 `/opt/simai/bin/freeze.sh`에 설치한다(`install -m 755`). 기존 제한 SSH 키가 실행하는 것은 호스트 설치본이다. 이미 동작 중인 배포는 끝난 뒤 동결된다.

```sh
/opt/simai/bin/freeze.sh on --hours 4 --reason "GPU 세션 세팅"
/opt/simai/bin/freeze.sh status
/opt/simai/bin/freeze.sh off
```

### GPU 자동 부팅 (`simai-gpu-boot`, S15P31A101-700)

팀원 누구나 GPU 를 켜면 사람 조작 없이 관제 준비까지 간다(설계 `docs/design/S15P31A101-700-gpu-auto-boot.md`, 서버 쪽 등록은 S15P31A101-699).
이미지 기본 CMD `auto` 가 아래 환경 변수를 보면 `simai-gpu-boot auto` 가 이 순서로 한다: 비밀 값 꺼내기 → GPU·공급자 사실 → TLS 키 → EC2 `POST /api/gpu/sessions` 등록 →
토큰을 권한 600 파일(`$SIMAI_ROOT/../.simai-private/gpu-session/<워커 ID>.token`·`tier0.token`)에 → 보고 시작(`gpu_session_report`, 단계마다 + `report.interval_ms` 마다) →
GPU 캐시(S3 `job-objects/gpu_cache/`, 워커 토큰 서명: TensorRT 엔진·Re-ID 는 Tier 0 전에 받고 vLLM 컴파일 캐시는 뒤에서) → 시작 묶음(보정 sha256 확인·기기 토큰 지문·TLS) →
`simai-bootstrap preflight --sets tier0` → `simai-bootstrap lobby …`(자식) → 단계 보고 WEIGHTS → ENGINE → TIER0 → VLLM → READY → 없던 캐시를 올린다.
실패하면 컨테이너를 끝내지 않고 FAILED(+`reason_code`)를 계속 보고하며 기다린다(공급자가 컨테이너를 다시 띄워 진짜 세션을 밀어내지 않게). 그 상태에서 손 경로(아래)로 이어 갈 수 있다.
캐시 올리기가 저장소에서 403 이면 보고 설명에 「캐시 경로 쓰기 거부(S3 403)」가 붙고 캐시 없이 진행한다(서버 키의 S3 권한을 확인한다, S15P31A101-710).
EC2 가 세션을 끝내면(409 `GPU_SESSION_ENDED`) lobby 를 멈추고, 같은 컨테이너 디스크로 다시 떠도 다시 등록하지 않는다(`/var/tmp/simai-gpu-boot/ended.json`). SIGTERM 이면 lobby 를 멈추고 `ENDED`(`PROVIDER_STOP`).

**RunPod 템플릿 환경 변수·Secrets(값은 템플릿에 쓰지 않는다).** 위 "RunPod 템플릿"·"Pod형" 의 값에 더한다.

| 이름 | 어디에 | 값 |
|---|---|---|
| `SIMAI_GPU_BOOT_SECRET` | RunPod Secret `{{ RUNPOD_SECRET_simai_gpu_boot_secret }}` | EC2 `admin gpu-boot register --id <ID> --worker-id <워커 ID>` 가 한 번 보여 준 부팅 비밀값(새면 `admin gpu-boot revoke`) |
| `SIMAI_GPU_BOOT_ID` | 템플릿 환경 변수(비밀 아님) | 위 `--id` 값(예: `gpu-boot-runpod`) |
| `SIMAI_EC2_BASE_URL` | 템플릿 환경 변수 | `https://simai.live` |
| `SIMAI_TIER0_TLS_PRIVATE_KEY` | RunPod Secret `{{ RUNPOD_SECRET_simai_tier0_tls_key }}` | 지금 쓰는 고정 Tier 0 TLS 비밀 키(PEM 또는 PEM 의 base64 한 줄). 노트북 고정 지문 그대로 |
| `SIMAI_TIER0_TLS_CERT_PEM` | 템플릿 환경 변수(공개) 또는 Secret | 그 키의 인증서(PEM 또는 base64 한 줄) |
| `SIMAI_TIER0_PINNED_SPKI_SHA256` | 템플릿 환경 변수(선택, 공개) | 노트북 `SIMAI_TIER0_PINNED_SPKI_SHA256` 과 같은 값. 다르면 기동 전에 FAILED `TLS_PIN_MISMATCH` |
| `HF_TOKEN` | RunPod Secret `{{ RUNPOD_SECRET_hf_token }}` | 그대로(가중치를 받는 preflight·lobby 자식에만 넘긴다) |
| `SIMAI_IMAGE_DIGEST` | 템플릿 환경 변수 | 이미지 다이제스트(등록 `image_digest`, 모르면 null) |
| `SIMAI_GPU_PROVIDER` | 선택 | `RUNPOD`(없으면 `RUNPOD_POD_ID` 로 판단)·`AWS`·`MANUAL` |
| `SIMAI_PRICE_PER_HOUR_USD` | 선택(S15P31A101-702 가 켤 때 넣는다) | 시간당 가격. 없으면 null |
| `SIMAI_BOOT_SERVICES` | 선택 | lobby `--services`. 비우면 VRAM 으로 정한다(S15P31A101-705): 70,000 MiB 이상 자동 부팅 기본 `tier0,vllm,worker,avatar,ppe`(lobby 기본 + PPE, S15P31A101-718. PPE 를 끄려면 `tier0,vllm,worker,avatar`), 48GB급(45,000~70,000 MiB) lobby 기본(ppe 꺼짐: VRAM 부족), 그 미만 `tier0`(세팅·VLM·아바타 꺼짐, 24GB급 미만은 경고) — 줄인 사실은 단계 보고 설명(stage_detail)에 붙는다 |
| `SIMAI_TIER0_ADVERTISE_HOST` | 선택 | Tier 0 가 I1 `tier0_endpoint` 로 알리는 주소(기본 `127.0.0.1` = SSAFY 망 기본 경로, 노트북이 EC2 SSH 로컬 포워드로 붙는다. 직접 연결이면 공개 IP). 포트는 TCP 17000(제어)·17001(영상, 카메라 넷 공용) |

RunPod 이 자동으로 넣는 `RUNPOD_POD_ID`·`RUNPOD_DC_ID`·`RUNPOD_PUBLIC_IP`·`RUNPOD_TCP_PORT_17000`·`_17001`·`_22` 는 등록 메시지(`instance_id`·`region`·`public_host`·`ports`)로 간다.
AWS·VM 은 같은 자리에 `SIMAI_INSTANCE_ID`·`SIMAI_REGION`·`SIMAI_PUBLIC_HOST`·`SIMAI_PUBLIC_PORT_<번호>`(매핑이 없으면 듣는 포트 그대로)를 넣는다(S15P31A101-703).

**AWS 서울 GPU(S15P31A101-703, 설계 `docs/design/S15P31A101-703-aws-gpu-provider.md`).** 화면에서 켜면 EC2 서버가 RunInstances 의 user-data 로 위 이름들을 넣는다:
호스트(Deep Learning AMI)가 IMDSv2 로 인스턴스 ID·가용 영역·공개 IP 를 읽어 `SIMAI_INSTANCE_ID`·`SIMAI_REGION`·`SIMAI_PUBLIC_HOST` 로, 포트는 `SIMAI_PUBLIC_PORT_17000=17000`·`_17001=17001`(직접 포트),
그리고 `docker run --gpus all --ipc host -p 17000:17000 -p 17001:17001 --env-file /run/simai-gpu.env <이미지>`(기본 CMD `auto`). 비밀 값은 user-data·환경 파일에 없다:
`SIMAI_SECRETS_SOURCE=aws-secretsmanager`·`SIMAI_AWS_SECRET_ID`(비밀 이름)·`SIMAI_AWS_REGION` 을 보면 `simai-bootstrap auto` 가 `simai-gpu-boot` 로 넘기고, `simai-gpu-boot` 가 인스턴스 역할(IMDSv2, 홉 2) 자격으로
Secrets Manager 의 JSON 비밀 `{"SIMAI_GPU_BOOT_SECRET": …, "SIMAI_TIER0_TLS_PRIVATE_KEY": …, "HF_TOKEN": …, "SIMAI_TIER0_TLS_CERT_PEM": …(선택, 공개)}` 를 받아 쓴다(표준 라이브러리 SigV4, aws cli·boto 없음, 이미지 의존성 변화 없음).
막 켠 인스턴스는 역할 자격이 늦게 뜰 수 있어 `SIMAI_AWS_SECRETS_WAIT_S`(기본 180 s) 동안 다시 하고, 거절(권한·이름)이면 등록하지 않고 기다린다(서버 자동 정지가 BOOT_TIMEOUT 으로 끈다).
SSH 는 호스트(시작 템플릿 키 페어, `ubuntu`)로 들어가 `sudo docker exec -it simai-gpu bash`. user-data 로그는 `/var/log/simai-gpu-user-data.log`.
선택 조정: `SIMAI_BOOT_CACHE_WAIT_S`(Tier 0 전 캐시 받기 상한, 600), `SIMAI_BOOT_VLLM_READY_TIMEOUT_S`(Tier 0 준비 뒤 vLLM 을 기다리는 상한, 1800 → 넘으면 READY + "vLLM 미준비"),
`SIMAI_BOOT_VLLM_CACHE_SETTLE_S`(vLLM /health 뒤 컴파일 캐시를 올리기까지, 60).

**손 경로(예비).** 시작 명령을 `sleep infinity` 로 띄우면 `auto` 가 돌지 않아 지금까지처럼 `prep-pod`·`start-lobby`(워커 토큰 발급·시작 묶음·Re-ID 업로드·`simai-bootstrap lobby`) 그대로다.
**손 경로의 lobby 기본(`start-lobby`·`simai-bootstrap lobby`)에는 `ppe` 가 없다**(S15P31A101-718 은 자동 부팅만 바꿨다). 장애로 손 재기동하면 보호구 감시가 빠지므로 `--services tier0,vllm,worker,avatar,ppe` 를 주고, 자동 부팅을 다시 돌릴 때 목록을 바꾸려면 `SIMAI_BOOT_SERVICES` 로 명시한다.
자동 부팅이 FAILED 로 기다리는 Pod 에서는 이미 받은 토큰 파일(`gpu-session/session.json` 에 경로)과 `gpu-session/lobby-bundle/` 을 `start-lobby` 의 `--worker-token-file`·`--tier0-token-file`·`--bundle-dir` 로 그대로 쓴다.
Re-ID 캐시가 아직 없으면 손으로 `$SIMAI_ROOT/reid` 에 올린 뒤 `simai-gpu-boot seed-reid` 로 한 번 올린다(자동 부팅은 READY 뒤 그 폴더가 있고 캐시에 없으면 스스로 올린다).

### GPU 세션 시작·끝 절차 (S15P31A101-652, 14차 세션 기준)

14차 세션에서 동결을 처음 써 보니 설치·해제 순서가 문서에 흩어져 있었다. 아래 순서를 그대로 따른다(EC2 호스트). `/opt/simai/repo` 는 마지막 배포가 푼 dev 판이다(`REVISION` 파일). 그 뒤 머지된 스크립트 변경을 넣으려면 먼저 배포가 끝났는지 본다.

**시작 (Pod 를 켜기 전)**

```sh
# 1) 배포·동결·보류 배포 스크립트와 타이머 설치(저장소 판과 다르면 다시). 배포가 쓰는 것은 설치본이다
cd /opt/simai/repo && cat REVISION
sudo sh deploy/ec2/install-host.sh     # 복사·cmp·타이머 켜기까지 한다(아래 "작업·세팅 연동 자동 보류")
# 2) 동결: 세션 길이 + 여유(보통 4~6시간). 진행 중 배포가 있으면 끝난 뒤 걸린다
sudo /opt/simai/bin/freeze.sh on --hours 5 --reason "14차 GPU 세션"
sudo /opt/simai/bin/freeze.sh status     # "동결 중 (사유·만료 UTC·설정자)" 확인
# 3) 워커 토큰 발급(세션 워커 ID), Pod 기동·게이트웨이 run-remote 는 그다음
```

**끝 (이 순서로)**

1. 게이트웨이를 미리보기로 돌린다(`gw.sh preview` → 관제 창 3개를 닫고 카메라 미리보기만). 녹화를 했으면 녹화 폴더 `manifest.json` 이 생겼는지 본다.
2. Pod 서비스를 내린다(감시 프로세스에 SIGTERM: 워커 → vLLM → Tier 0 순서). 결과 묶음·로그(`$SIMAI_ROOT/logs`, `runs/`)를 노트북으로 가져온 뒤 Pod 를 Stop 한다(볼륨이 없다).
3. 이번 세션 워커 토큰을 폐기한다(`worker-token.sh revoke <워커 ID>`).
4. 동결을 푼다: `sudo /opt/simai/bin/freeze.sh off` → `status` 가 `동결 없음`.
5. 동결 중 건너뛴 머지가 있으면(Jenkins 로그 "동결 중 … 배포 건너뜀") dev 최신으로 배포를 다시 실행한다. 수동 동결로 건너뛴 커밋은 자동으로 다시 배포되지 않는다(작업·세팅 진행 중이라 **보류**한 커밋만 타이머가 배포한다).
6. Pod 임시 수정(환경 파일·코드 덧댐)은 세션 기록에 적고 MR 로 옮긴다(다음 이미지에 반영되기 전에는 Pod 를 새로 켜면 사라진다).

시간은 1~168 정수다. 사유·만료 UTC·설정자는 `/opt/simai/deploy.freeze`에 기록한다. 동결 중 Jenkins 로그에는 사유·만료와 배포 건너뜀이 표시되고 성공으로 끝난다. 만료되면 경고 후 배포한다. 건너뛴 커밋은 자동으로 다시 배포되지 않으므로 세션 후 배포를 다시 실행한다.

### 작업·세팅 연동 자동 보류 (S15P31A101-676, GPU 세션 빈 구간 -682)

수동 동결 말고도, 배포 직전에 진행 중인 작업·세팅이 있거나 GPU 세션 중(Tier 0 가 붙어 있음·녹화 업로드 중)이면 deploy.sh 가 배포를 **보류**하고 끝나면 자동으로 배포한다. 설계: `docs/design/S15P31A101-676-deploy-freeze-work-linked.md`, `docs/design/S15P31A101-682-deploy-hold-gaps.md`.

- **판단**(같은 `deploy.lock` 안, 수동 동결 확인 다음): `docker exec simai-ec2-db-1 psql` 로 운영 DB 를 읽기 전용 트랜잭션으로 묻는다. 진행 중 작업 = 진행 중 실행(S15P31A101-696, `work_run_window` 뷰에서 `ended_at IS NULL` 이고 작업이 IN_PROGRESS), 진행 중 세팅 = `setup_session.status` 가 COMPLETED·FAILED 가 아니거나 그 세션의 맵 보강 다시 굽기가 QUEUED·RUNNING, Tier 0 연결 = 서버가 `system_health` 를 최근 3분 안에 받음(`received_at`, Tier 0 는 I1 이 붙어 있으면 작업과 무관하게 1 Hz 로 보낸다. Pod 준비~세팅 전·세팅 뒤~작업 전·작업 뒤 빈 구간을 막는다), 녹화 업로드 = `upload_grant` 에 최근 10분 안 `RECORDING_SEGMENT` 발급(게이트웨이가 조각마다 받는다). 이미지를 만든 뒤 서비스를 건드리기 직전에 한 번 더 본다.
- **보류**: Jenkins 가 보낸 tar 를 `/opt/simai/pending/<커밋>.tar` 로 두고 `/opt/simai/pending/hold`(커밋·처음 보류·마지막 보류·사유·시도 횟수)를 쓴다. 보류는 하나뿐이다. 새 커밋이 오면 이전 보류를 대체하고 처음 보류 시각은 이어받는다. Jenkins 로그: `작업·세팅·GPU 세션 진행 중이라 배포 보류(작업 48 실행 2, Tier 0 연결(마지막 보고 2026-11-13 05:00:12 UTC), 녹화 업로드 3건)`, 빌드는 성공.
- **자동 실행**: `simai-deploy-pending.timer` 가 3분마다(앞 실행이 끝나고 3분 뒤) `/opt/simai/bin/deploy-pending.sh` → `deploy.sh pending` 을 부른다. 보류가 없으면 바로 끝난다. 수동 동결 중이면 하지 않는다. 진행 중이 없으면 저장한 tar 로 같은 배포를 하고 성공하면 보류를 지운다. 실패하면 보류를 남기고 3번까지 다시 해 본 뒤 멈춘다(새 커밋이 오면 다시 시작). 다른 배포가 잠금을 쥐고 있으면 다음 차례로 넘긴다.
- **12시간 안전장치**: 처음 보류에서 12시간이 지나면 진행 중이어도 배포하고 경고를 남긴다(작업 종료를 잊은 경우·Pod 를 켜 둔 채 둔 경우). 그때 진행 중이던 항목의 키는 `pending/ignored` 에 적어 다음부터 그 항목으로는 보류하지 않는다. 키는 `work <작업 ID>-<실행 번호>`(실행마다 따로, -696 전 `work <ID>` 도 읽는다)·`setup <ID>`·`tier0 <이어진 연결의 시작 epoch>`·`upload <이어진 업로드의 시작 epoch>` 다. Tier 0 는 같은 연결이 이어지는 동안만 넘기고, Pod 를 껐다 켜면(수신이 3분 넘게 끊김) 새 연결이라 다시 보류한다.
- **설정**(`/opt/simai/.env`, 1 이상 정수, 틀리면 경고하고 기본값): `SIMAI_DEPLOY_HOLD_MAX_HOURS`(안전장치 시간, 기본 12), `SIMAI_DEPLOY_HOLD_TIER0_MINUTES`(Tier 0 최근 수신 범위, 기본 3분), `SIMAI_DEPLOY_HOLD_UPLOAD_MINUTES`(녹화 업로드 최근 발급 범위, 기본 10분). Pod 를 끄면 3분(+타이머 최대 3분) 뒤 보류한 배포가 나간다.
- **DB 를 묻지 못하면**(컨테이너 없음·오류·30초 초과·알아볼 수 없는 응답) 보류하지 않고 지금처럼 배포하며 경고한다. 배포가 영원히 막히지 않게 하기 위해서다.
- 수동 동결과의 관계: 동결이 늘 우선이다. 동결 중에 온 커밋은 지금처럼 건너뛰고(저장하지 않는다), 동결 전부터 있던 보류는 동결이 풀린 뒤 타이머가 배포한다.

설치(EC2, 머지되어 `/opt/simai/repo` 에 풀린 뒤 한 번. 스크립트·유닛을 고치면 다시):

```sh
cd /opt/simai/repo && cat REVISION
sudo sh deploy/ec2/install-host.sh          # 배포 사용자가 ubuntu 가 아니면 인자로 준다
systemctl list-timers simai-deploy-pending.timer --no-pager
```

`install-host.sh` 는 deploy.sh·freeze.sh·deploy-pending.sh 를 `/opt/simai/bin` 에 복사(cmp 확인)하고, `/opt/simai/pending`(배포 사용자, 750)을 만들고, 유닛 두 개(`deploy/ec2/systemd/`)를 `/etc/systemd/system` 에 넣어 타이머를 켠다. authorized_keys 는 건드리지 않는다.

확인:

```sh
sudo /opt/simai/bin/freeze.sh status          # 동결 줄 + "자동 보류 중 (커밋·사유·처음/마지막 보류·시도)" 또는 "자동 보류 없음"
journalctl -u simai-deploy-pending -n 50 --no-pager
cat /opt/simai/pending/history.log            # 보류·대체·시도·완료·안전장치 기록(UTC)
```

보류를 버리려면(그 커밋을 배포하지 않기로 했을 때): `sudo rm /opt/simai/pending/hold /opt/simai/pending/<커밋>.tar`. 다음 머지가 다시 보류·배포한다. 손 배포(`SSH_ORIGINAL_COMMAND='deploy <커밋>'`)도 같은 확인을 거쳐 보류된다. 진행 중이어도 바로 배포해야 하면 작업을 끝내거나 안전장치 시간을 줄인다. GPU 세션 중 서버 수정을 바로 넣어야 하면 Pod 의 Tier 0 를 내리거나(3분 뒤 풀림) `SIMAI_DEPLOY_HOLD_MAX_HOURS` 를 잠시 1 로 줄인다.
