# deploy: 모델별 Docker 이미지 (S15P31A101-32)

모델별 Python·CUDA·Torch 환경을 Docker 이미지로 고정하고, 이미지 다이제스트를 Run 기록의 `env_hash` 로 쓴다(명세 3.12 저장소 규칙, 20.5, 21.5).
Network Volume 에는 가중치, 데이터, 실험 산출물, TensorRT 엔진 캐시만 둔다. 환경은 볼륨에 설치하지 않는다(E-GATE-0 은 예외였다).

## 파일

| 파일 | 하는 일 |
|---|---|
| `docker-bake.hcl` | 빌드 정의. 외부 이미지(CUDA·uv) 다이제스트 고정, 대상 `tier0`·`exp02`(베이스를 함께 빌드) |
| `base/Dockerfile`, `base/simai-start.sh` | 공통 베이스: CUDA 이미지 + uv + uv 관리 Python 3.11.15 + RunPod SSH(키 인증만) 시작 스크립트 |
| `tier0/Dockerfile`, `tier0/entrypoint.sh` | Tier 0 서비스 이미지(YOLO26x-pose, TensorRT FP16). `ai/uv.lock` 으로 의존성 고정, 첫 기동 때 엔진 준비 |
| `exp02/Dockerfile`, `exp02/entrypoint.sh`, `exp02/requirements.{in,lock.txt}` | EXP-02 Fast SAM 3D Body 이미지(골격). 체크포인트는 볼륨에서 읽는다 |
| `*/Dockerfile.dockerignore` | 이미지별 빌드 컨텍스트 **허용 목록**(나열한 파일만 들어간다) |
| `compose.yaml` | 로컬 점검용(RunPod 은 Compose 를 쓰지 않는다) |
| `scripts/check_image.sh` | 이미지 점검(GPU 불필요): 비밀·가중치·엔진·SSH 호스트 키 없음, import, 진입점 오류 코드, GPU 별 엔진 경로, SSH 비밀번호 접속 거부 |

엔진 캐시 코드는 `ai/src/simai_ai/detection/engine_cache.py`(시험 `ai/tests/detection/test_engine_cache.py`)다.

## 이미지에 넣지 않는 것

| 무엇 | 어디에 | 비고 |
|---|---|---|
| 가중치 (YOLO26x-pose `.pt`, sam-3d-body-dinov3 등) | `/workspace/simai/weights/` | 해시 목록은 GitLab 에도 남긴다(3.12). Tier 0 는 `SIMAI_TIER0_WEIGHTS_SHA256` 을 주면 다를 때 시작하지 않는다 |
| TensorRT 엔진 | `/workspace/simai/engines/` | Pod 첫 기동 때 그 GPU 에서 만든다(아래) |
| 데이터·녹화·2D 관측·Run 산출물 | `/workspace/simai/{datasets,recordings,obs2d,runs}` | 19.1 볼륨 구조 |
| 비밀 (HF 토큰, Tier 0 TLS 개인키, 기기·워커 토큰) | `/workspace/.simai-private/` (HF 토큰은 `hf.env`) | 이미지·저장소·RunPod 템플릿 환경 변수에 넣지 않는다. 볼륨은 chmod 가 안 먹으므로 다른 Pod 에 붙일 때 주의 |
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

## RunPod 템플릿 (Tier 0)

| 항목 | 값 |
|---|---|
| 데이터센터·GPU | 일본 AP-JP-1, H100 80GB SXM, GPU 하나(Tier 0 가 먼저 쓴다) |
| 이미지 | `<레지스트리>/<프로젝트>/tier0@sha256:<다이제스트>` |
| CUDA 버전 필터 | 13.0 이상(위 "CUDA 13 인 이유") |
| Container Disk | 40GB 이상(이미지 풀면 약 19.5GB + 임시 파일) |
| Network Volume | AP-JP-1 볼륨을 `/workspace` 에 |
| 공개 TCP 포트 | 22(SSH, 키 인증만), I0 수신 포트(70000 이상 대칭 매핑, 실제 포트는 `RUNPOD_TCP_PORT_*`, 명세 3.12) |
| 시작 명령 | 비움(이미지 기본: `simai-tier0` → 엔진 준비 → `python -m services.tier0`) |
| 환경 변수 | `SIMAI_ROOT=/workspace/simai`, `SIMAI_TIER0_WEIGHTS=/workspace/simai/weights/yolo/yolo26x-pose.pt`, `SIMAI_TIER0_WEIGHTS_SHA256=<해시 목록의 값>`, `SIMAI_ENGINE_CACHE=/workspace/simai/engines`, `SIMAI_OBS2D_ROOT=/workspace/simai/obs2d`, `SIMAI_IMAGE_DIGEST=sha256:...`. 선택: `SIMAI_TIER0_MODE`(e2e), `SIMAI_TIER0_IMGSZ`(1280), `SIMAI_TIER0_MAX_BATCH`(4), `SIMAI_TIER0_CAMERA`(2304x1296) |
| 비밀 | 템플릿에 넣지 않는다. `/workspace/.simai-private/` 에서 읽는다. `PUBLIC_KEY` 는 RunPod 이 계정 SSH 키로 채운다 |

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
- 엔진 경로는 Tier 0 프로세스에 `SIMAI_TIER0_ENGINE` 환경 변수로 넘긴다. Tier 0 조립(후속 티켓)이 이 값으로 엔진을 연다.
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
노트북 준비: 이미지 빌드 → `check_image.sh` 통과 → 사용자 확인 뒤 푸시 → 다이제스트로 템플릿 작성. 볼륨에 `weights/yolo/yolo26x-pose.pt` 와 해시 목록이 있는지 확인(E-GATE-0 때 받아 둠).

Tier 0 이미지 (일본 AP-JP-1 H100, 볼륨 `/workspace`, 시작 명령 `sleep infinity` 로 띄우고 SSH 로 들어가 실행):

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
# 5) 진입점 전체 (Tier 0 는 아직 골격이라 "아직 조립하지 않았다" 로 종료 코드 1 이 정상. 엔진 준비 로그가 먼저 나와야 한다)
simai-tier0 --config /dev/null; echo "exit=$?"
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
