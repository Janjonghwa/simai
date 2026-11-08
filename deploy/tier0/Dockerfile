# Tier 0 서비스 이미지와 통합 이미지 (명세 3.1 RunPod Tier 0, 3.2 프로세스 분리, 3.12, 21.5). 빌드: deploy/README.md (docker buildx bake tier0 / unified).
# 타깃 두 개가 아래 큰 층(CUDA 13 휠·torch·TensorRT libs·큰 일반 휠·Tier 0 잠금)을 같이 쓴다(S15P31A101-246):
#   tier0    Tier 0 만(지금까지의 이미지와 같은 내용). 기본 진입점은 simai-bootstrap → simai-tier0
#   unified  같은 가상환경에 워커 스택(vLLM 0.21.0 + vllm-cosmos3, HMR 추론 의존성 + Fast-SAM-3D-Body 코드 사본)을 더한 이미지. simai-bootstrap 이 Tier 0 → vLLM 서버 → 분석 워커를
#            별도 프로세스(그룹)로 띄우고 감시한다(SIMAI_SERVICES, deploy/README.md "통합 이미지", docs/design/unified-env.md "2단계")
#            MapAnything(map-anything @3d10cf7, 잠금 밖 --no-deps, 의존성은 잠금 scene 묶음)도 같은 가상환경에 있다. LIVE 와 따로 도는 준비 단계 일회성 작업이다
#            ('simai-bootstrap scene <명령>', S15P31A101-250, docs/design/unified-env.md "3단계")
# - Python 의존성은 ai/uv.lock 그대로(uv sync --locked). pyproject 와 잠금이 어긋나면 빌드가 실패한다.
#   큰 휠은 잠금에서 뽑은 묶음별 층으로 먼저 깐다(S15P31A101-207, docs/design/exp-images.md "Tier 0 층 분할"). 가상환경을 통째 COPY --from 하지 않는다.
# - 가중치(YOLO26x-pose .pt, Cosmos3-Nano)와 TensorRT 엔진은 이미지에 없다. 모두 저장 위치($SIMAI_ROOT)에 둔다.
#   시작하면 simai-bootstrap 이 저장 위치·GPU 를 고르고 가중치를 목록(deploy/weights.json)대로 받아 확인한 뒤(S15P31A101-205),
#   simai-tier0 가 그 GPU 에서 엔진을 만들고 캐시한다(entrypoint → simai_ai.detection.engine_cache).
# - 빌드 컨텍스트는 저장소 최상위이고, 들어오는 파일은 deploy/tier0/Dockerfile.dockerignore 의 허용 목록뿐이다.
ARG BASE_IMAGE=simai-base
# MapAnything 코드 커밋(unified 만 쓴다). deploy/mapanything/Dockerfile 의 torch 2.7.1 이미지와 같은 커밋이라 두 환경의 결과를 비교한다
ARG MA_COMMIT=3d10cf7a3016fc0f9bb13a071ee66c47b10be0d9
ARG DINOV2_COMMIT=7764ea0f912e53c92e82eb78a2a1631e92725fc8
ARG WM2_COMMIT=df9988efb87bfc0f4947eb3889411cf957478b06

# 0) 잠금(ai/uv.lock)에서 층별 설치 목록(해시 포함)을 뽑는다. 잠금이 바뀌면 이 단계는 다시 돌지만, 목록 내용이 같으면
#    아래 COPY --from 의 캐시 키(내용 체크섬)가 같아 큰 층을 다시 만들지 않는다(같은 층 = 같은 다이제스트 = 다시 올리지 않음).
#    묶음(바뀌는 빈도가 낮은 것부터): cuda = torch 가 가져오는 CUDA 13 휠, torch = torch·triton·torchvision,
#    trt = TensorRT libs(pypi.nvidia.com), wheels = 드물게 바뀌는 큰 일반 휠. 나머지 작은 패키지는 4) 층, simai_ai 코드는 5) 층.
#    worker = 통합 이미지의 워커 쪽 큰 휠(tier0 + worker 내보내기에서 뽑는다. Tier 0 묶음에 없는 것만).
#    묶음의 이름이 잠금에 없으면 빌드를 멈춘다(나뉜 줄 알았는데 4) 층 하나에 다 들어가는 일을 막는다).
FROM ${BASE_IMAGE} AS layer-lists
WORKDIR /src
COPY ai/pyproject.toml ai/uv.lock ./
RUN set -eu; \
    uv export -q --locked --no-dev --extra tier0 --extra reid --no-emit-project --format requirements-txt -o all.txt; \
    uv export -q --locked --no-dev --extra tier0 --extra reid --extra worker --no-emit-project --format requirements-txt -o all-worker.txt; \
    pick(){ src=$1; out=$2; shift 2; : > "$out"; \
      for n in "$@"; do \
        awk -v n="$n" '/^[^ #]/ { x = $1; sub(/[ =;].*/, "", x); keep = (x == n) } keep && !/^ *#/' "$src" > one.txt; \
        grep -q "^$n==" one.txt || { echo "층 묶음의 $n 이 잠금에 없다(deploy/tier0/Dockerfile 묶음을 고친다)" >&2; exit 1; }; \
        cat one.txt >> "$out"; \
      done; }; \
    pick all.txt cuda.txt nvidia-cublas nvidia-cudnn-cu13 nvidia-cufft nvidia-cusolver nvidia-cusparse nvidia-cusparselt-cu13 \
      nvidia-nccl-cu13 nvidia-nvshmem-cu13 nvidia-cuda-nvrtc nvidia-curand nvidia-nvjitlink nvidia-cuda-runtime \
      nvidia-cuda-cupti nvidia-cufile nvidia-nvtx cuda-toolkit; \
    pick all.txt torch.txt torch triton torchvision; \
    pick all.txt trt.txt tensorrt-cu13-libs; \
    pick all.txt wheels.txt polars-runtime-32 pyarrow opencv-python-headless scipy av; \
    pick all-worker.txt worker.txt flashinfer-cubin vllm tokenspeed-triton nvidia-cutlass-dsl-libs-base llvmlite tilelang \
      xgrammar z3-solver; \
    rm one.txt

# ---------------------------------------------------------------- Tier 0 의존성 (두 타깃 공통)
FROM ${BASE_IMAGE} AS tier0-deps

WORKDIR /opt/simai/ai
# 가상환경은 하나다(Tier 0·vLLM·워커가 같은 판, S15P31A101-235). 그래서 이미지 전역 VIRTUAL_ENV·PATH 로 둔다.
# LD_LIBRARY_PATH 는 두지 않는다(CUDA·TensorRT 휠은 자기 RUNPATH 로 찾는다)
ENV UV_PROJECT_ENVIRONMENT=/opt/simai/ai/.venv \
    VIRTUAL_ENV=/opt/simai/ai/.venv \
    PATH=/opt/simai/ai/.venv/bin:${PATH}

# 1)~3) 큰 휠을 묶음마다 다른 층으로(--require-hashes: 잠금의 해시와 다르면 실패, --no-deps: 의존성은 4) 의 uv sync 가 맞춘다).
#    한 층(11.5GB)이던 것을 나눠 레지스트리에 병렬로 올리고, 끊겨도 끝난 층은 다시 올리지 않는다.
RUN uv venv -q /opt/simai/ai/.venv
COPY --from=layer-lists /src/cuda.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/cuda.txt
COPY --from=layer-lists /src/torch.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/torch.txt
# TensorRT libs 는 pypi.nvidia.com 에만 있다(ai/pyproject.toml [tool.uv.sources]). 이 층만 그 색인을 쓴다
COPY --from=layer-lists /src/trt.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -q --no-deps --require-hashes --index-url https://pypi.nvidia.com -r /tmp/layers/trt.txt
COPY --from=layer-lists /src/wheels.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/wheels.txt

# 4) 나머지 의존성(잠금이 바뀌면 이 층부터 다시). --locked: 잠금이 pyproject 와 다르면 실패. 위 층의 패키지는 같은 판이라 건너뛴다.
COPY ai/pyproject.toml ai/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra tier0 --extra reid --no-install-project

# ---------------------------------------------------------------- tier0 타깃
FROM tier0-deps AS tier0

# 5) simai_ai 패키지(편집 모드 아님)와 배포 단위(services 는 설치하지 않고 이 폴더에서 실행), 계약 스키마
COPY ai/src ./src
COPY ai/services ./services
COPY contracts /opt/simai/contracts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra tier0 --extra reid --no-editable \
 && rm -rf ./src

COPY --chmod=0755 deploy/tier0/entrypoint.sh /usr/local/bin/simai-tier0
# 분석 워커를 Tier 0 준비(표시 파일 phase=running·reserve OK) 뒤에 띄우는 도구 (S15P31A101-60·-183, deploy/README "분석 워커")
COPY --chmod=0755 deploy/scripts/wait_tier0_ready.py /usr/local/bin/simai-wait-tier0
# 어느 GPU·지역·사이트에서든 같은 순서로 준비하는 시작 스크립트와 가중치 목록 (S15P31A101-205, deploy/README "simai-bootstrap")
COPY --chmod=0755 deploy/scripts/simai_bootstrap.py /usr/local/bin/simai-bootstrap
# 윈도 체크아웃의 CRLF 가 shebang 을 깨뜨리지 않게 빌드에서 CR 을 뺀다(check_image.sh 는 결과를 다시 검사한다)
RUN sed -i 's/\r$//' /usr/local/bin/simai-tier0 /usr/local/bin/simai-wait-tier0 /usr/local/bin/simai-bootstrap
RUN sed -i '1c#!/opt/simai/ai/.venv/bin/python' /usr/local/bin/simai-bootstrap
COPY deploy/weights.json /opt/simai/deploy/weights.json

# 실행 중에 패키지를 설치하지 않는다(ultralytics 자동 설치 끔). 없는 의존성은 실패로 드러나야 한다.
# NVIDIA_DRIVER_CAPABILITIES 의 video: 선택 디코더 NVDEC(libnvcuvid)을 컨테이너에 올린다(S15P31A101-182, 기본은 CPU -236).
# SIMAI_ROOT 는 고정하지 않는다(simai-bootstrap 이 SIMAI_ROOT → /workspace/simai → /srv/simai 순서로 고른다).
# SIMAI_SERVICES 가 없으면 Tier 0 단독이다(simai-bootstrap 이 감시 프로세스 없이 simai-tier0 로 exec).
ENV SIMAI_CONTRACTS_DIR=/opt/simai/contracts \
    SIMAI_WEIGHTS_MANIFEST=/opt/simai/deploy/weights.json \
    YOLO_AUTOINSTALL=false \
    YOLO_OFFLINE=true \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

# 기본 CMD tier0 = 준비 → Tier 0(인자 없는 simai-bootstrap 은 사용법만 보이고 64, S15P31A101-414). 'sleep infinity'·bash 같은 다른 명령은 준비 없이 그대로 실행한다.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simai-start", "/usr/local/bin/simai-bootstrap"]
CMD ["tier0"]

# ---------------------------------------------------------------- 통합 이미지 의존성 (Tier 0 층 위에 워커 층)
FROM tier0-deps AS unified-deps
# SSAFY 망의 HTTP(80) 프록시가 Ubuntu 저장소 요청 일부를 400 으로 거절한다(S15P31A101-366). 받는 패키지는 같고 경로만 HTTPS 로 바꾼다.
RUN sed -i 's#http://\(archive\|security\).ubuntu.com#https://\1.ubuntu.com#g' /etc/apt/sources.list.d/ubuntu.sources
# pyrender EGL 렌더용 glvnd 로더(libEGL.so 무버전 이름은 libegl-dev), pyglet 이 여는 X11 라이브러리(-207 에서 발견),
# (sam_3d_body 가 import 때 pyrender 를 연다), triton(vLLM·torch.compile) 런처를 컴파일할 C 컴파일러, git(잠금의 git 의존성 vllm-cosmos3·transformers-cosmos3·utils3d-moge·pipeline 설치)
# hadolint ignore=DL3008
RUN apt-get -o Acquire::Retries=8 update \
 && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=8 install -y --no-install-recommends \
      libegl1 libegl-dev libgl1 libglvnd0 libglib2.0-0 libxrender1 libxext6 libsm6 gcc libc6-dev git \
 && rm -rf /var/lib/apt/lists/*
COPY --from=layer-lists /src/worker.txt /tmp/layers/
# --no-config: 이 폴더의 ai/pyproject.toml [tool.uv] constraint-dependencies 를 uv pip 가 읽으면 --require-hashes 와 부딪힌다(판은 이미 해시로 고정)
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-config --no-deps --require-hashes -r /tmp/layers/worker.txt
# 워커 묶음 전체(Tier 0 층의 패키지는 같은 판이라 건너뛴다. tier0 ⊂ tier0+worker 라 지우는 것도 없다)
# hadolint ignore=DL3059
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra tier0 --extra reid --extra worker --no-install-project

# ---------------------------------------------------------------- HMR 코드 사본 (이미지에는 결과만 옮긴다)
# 잠금 밖 휠(chumpy·Detectron2·MoGe)은 통합 이미지에 넣지 않는다(S15P31A101-246, docs/design/unified-env.md "2단계"):
# 시연 HMR 경로(보정 K → sam-3d-body 추론 → 메시·관절, 렌더 없음)는 셋 없이 import 된다(exp02 이미지에서 셋을 막고 확인).
# Detectron2 는 렌더 패널(tools.vis_utils)의 LazyConfig, MoGe 는 K 가 없을 때의 FOV 예비 경로, chumpy 는 mhr2smpl 에서만 쓴다 → exp02 이미지에만 둔다.
FROM unified-deps AS hmr-code
ARG FS_COMMIT=d72aa36
ARG DINOV3_COMMIT=6876159a11b4df116f30f667f8c9888617df0751
RUN git clone -q https://github.com/yangtiming/Fast-SAM-3D-Body /opt/code/Fast-SAM-3D-Body \
 && git -C /opt/code/Fast-SAM-3D-Body checkout -q "${FS_COMMIT}" \
 && rm -rf /opt/code/Fast-SAM-3D-Body/.git \
 && echo "Fast 저장소에 커밋된 가중치(mhr2smpl 실험용, 이미지에 넣지 않음):" \
 && find /opt/code/Fast-SAM-3D-Body -type f \( -name '*.pth' -o -name '*.pt' -o -name '*.ckpt' -o -name '*.safetensors' -o -name '*.onnx' -o -name '*.engine' \) -print -delete \
 && git clone -q https://github.com/facebookresearch/dinov3 /opt/code/facebookresearch_dinov3_main \
 && git -C /opt/code/facebookresearch_dinov3_main checkout -q "${DINOV3_COMMIT}" \
 && rm -rf /opt/code/facebookresearch_dinov3_main/.git

# ---------------------------------------------------------------- MapAnything 코드 사본·휠 (S15P31A101-250, 이미지에는 결과만 옮긴다)
# map-anything @3d10cf7(수정 없음)을 휠로 만들어 둔다(잠금 밖. 실행 이미지에서 --no-deps 로 깔고, 의존성은 잠금의 scene 묶음이 이미 깔았다).
# DINOv2 허브 코드 @7764ea0(가중치 없음): uniception 인코더가 torch.hub.load("facebookresearch/dinov2", pretrained=False) 로 골격만 만들 때 이 캐시를 쓴다.
FROM unified-deps AS ma-code
ARG MA_COMMIT
ARG DINOV2_COMMIT
ARG WM2_COMMIT
RUN git clone -q https://github.com/facebookresearch/map-anything /opt/code/map-anything \
 && git -C /opt/code/map-anything checkout -q "${MA_COMMIT}" \
 && rm -rf /opt/code/map-anything/.git \
 && git clone -q https://github.com/facebookresearch/dinov2 /opt/code/facebookresearch_dinov2_main \
 && git -C /opt/code/facebookresearch_dinov2_main checkout -q "${DINOV2_COMMIT}" \
 && rm -rf /opt/code/facebookresearch_dinov2_main/.git \
 && git clone -q https://github.com/Tencent-Hunyuan/HY-World-2.0 /opt/code/HY-World-2.0 \
 && git -C /opt/code/HY-World-2.0 checkout -q "${WM2_COMMIT}" \
 && rm -rf /opt/code/HY-World-2.0/.git /opt/code/HY-World-2.0/assets \
 && find /opt/code/HY-World-2.0 -type f \( -name '*.pth' -o -name '*.pt' -o -name '*.ckpt' -o -name '*.safetensors' -o -name '*.onnx' -o -name '*.engine' \) -delete
# setuptools 가 사본에 남기는 build/·egg-info 는 지운다(사본이 sys.path 에 들어가 설치 메타데이터와 겹치지 않게)
RUN --mount=type=cache,target=/root/.cache/uv uv build -q --no-config --wheel --out-dir /opt/code/dist /opt/code/map-anything \
 && rm -rf /opt/code/map-anything/build /opt/code/map-anything/*.egg-info

# texrecon: deploy/texrecon/Dockerfile 과 같은 고정 커밋·빌드 절차, 최종 ABI 는 unified 와 같다.
FROM unified-deps AS texrecon-build
ARG MVST_COMMIT=f3374298ac959cb5afe47a14e4d35d2ac7fbdbb1
ARG MVE_COMMIT=91c0f3fde0781399b2fe872d8e52e4899274a84a
ARG RAYINT_COMMIT=b62c8905f6dd11a179128517e814e0e00f9bb809
# hadolint ignore=DL3008
RUN apt-get -o Acquire::Retries=8 update \
 && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=8 install -y --no-install-recommends \
      build-essential cmake git ca-certificates libpng-dev libjpeg-dev libtiff-dev libtbb-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# hadolint ignore=DL3003
RUN set -eu; \
    git clone https://github.com/nmoehrle/mvs-texturing.git; \
    git -C mvs-texturing checkout --quiet "$MVST_COMMIT"; \
    cd mvs-texturing; \
    sed -i "s#GIT_REPOSITORY  https://github.com/nmoehrle/mve.git#GIT_REPOSITORY  https://github.com/nmoehrle/mve.git\n    GIT_TAG         $MVE_COMMIT#" elibs/CMakeLists.txt; \
    sed -i "s#GIT_TAG         cuda#GIT_TAG         $RAYINT_COMMIT#" elibs/CMakeLists.txt; \
    grep -q "$MVE_COMMIT" elibs/CMakeLists.txt && grep -q "$RAYINT_COMMIT" elibs/CMakeLists.txt; \
    mkdir build; cd build; cmake -DCMAKE_BUILD_TYPE=RELEASE ..; make -j"$(nproc)"; \
    test -x apps/texrecon/texrecon


# ---------------------------------------------------------------- unified 타깃
FROM unified-deps AS unified
# CPU 텍스처 굽기 실행 파일만 복사한다. 빌드 도구는 최종 이미지에 남기지 않는다. libusb 는 open3d import 에 필요하다(-366).
RUN apt-get -o Acquire::Retries=8 update \
 && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=8 install -y --no-install-recommends libpng16-16t64 libjpeg-turbo8 libtiff6 libtbb12 libgomp1 libusb-1.0-0 procps \
 && rm -rf /var/lib/apt/lists/*
# 진단 도구(-389): procps(ps·top)와 py-spy(샘플링 프로파일러). 9차에 Pod 에 없어 판정 지연을 손 계측으로 쟀다.
# py-spy 는 앱 가상환경(잠금)과 섞지 않게 별도 경로에 해시로 고정해 둔다. 컨테이너에 ptrace 권한이 없으면 부착되지 않는다(-382 기동 전 점검이 알린다).
RUN printf '%s\n' 'py-spy==0.4.2 --hash=sha256:aeb0323409199c785f730645e9f4bb7a7b9ca2c481f2c331a55642b5d13fa52f' > /tmp/tools.txt \
 && uv pip install -q --no-config --no-deps --require-hashes --python /opt/simai/ai/.venv/bin/python --target /opt/simai/tools -r /tmp/tools.txt \
 && ln -s /opt/simai/tools/bin/py-spy /usr/local/bin/py-spy && rm /tmp/tools.txt
COPY --from=texrecon-build /src/mvs-texturing/build/apps/texrecon/texrecon /usr/local/bin/texrecon
COPY --from=texrecon-build /src/mvs-texturing/LICENSE.txt /usr/local/share/licenses/mvs-texturing/LICENSE.txt
ENV SIMAI_TEXRECON=/usr/local/bin/texrecon
ARG MA_COMMIT
ARG WM2_COMMIT
COPY --from=hmr-code /opt/code/Fast-SAM-3D-Body /opt/simai/hmr/Fast-SAM-3D-Body
COPY --from=hmr-code /opt/code/facebookresearch_dinov3_main /root/.cache/torch/hub/facebookresearch_dinov3_main
# MapAnything·WorldMirror 2.0 소스 사본(SIMAI_MAPANYTHING_REPO·SIMAI_WM2_REPO)과 DINOv2 허브 코드
COPY --from=ma-code /opt/code/map-anything /opt/simai/mapanything/map-anything
COPY --from=ma-code /opt/code/HY-World-2.0 /opt/simai/worldmirror2/HY-World-2.0
COPY --from=ma-code /opt/code/facebookresearch_dinov2_main /root/.cache/torch/hub/facebookresearch_dinov2_main

# 5) simai_ai 패키지·배포 단위·계약 스키마(잠금 밖 휠이 없으므로 tier0 와 같은 정확 sync)
COPY ai/src ./src
COPY ai/services ./services
COPY contracts /opt/simai/contracts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra tier0 --extra reid --extra worker --no-editable \
 && rm -rf ./src
# map-anything 휠(잠금 밖)은 정확 sync 뒤에 --no-deps 로 둔다(정확 sync 는 잠금에 없는 패키지를 지운다, docs/design/unified-env.md "설치 순서" 3).
# 휠(약 2MB)은 단계에서 bind 로 읽어 층에 남기지 않는다. 코드가 바뀌면 이 층도 다시 만든다(몇 초)
RUN --mount=type=bind,from=ma-code,source=/opt/code/dist,target=/tmp/ma-dist \
    uv pip install -q --no-config --no-deps --no-index /tmp/ma-dist/mapanything-*.whl

COPY --chmod=0755 deploy/tier0/entrypoint.sh /usr/local/bin/simai-tier0
COPY --chmod=0755 deploy/scripts/wait_tier0_ready.py /usr/local/bin/simai-wait-tier0
COPY --chmod=0755 deploy/scripts/simai_bootstrap.py /usr/local/bin/simai-bootstrap
# HMR 실행 도우미(fastcfg 링크·체크포인트 확인, exp02 이미지와 같은 파일). 가중치 묶음은 이미지에 정하지 않으므로(기본 tier0) 부를 때 준다:
#   SIMAI_WEIGHTS_SET=exp02 simai-bootstrap run simai-exp02 python .../run_image.py --calibration ... --no-render   (세션 큐 u2 도 SIMAI_WEIGHTS_SET=exp02 simai-bootstrap queue)
COPY --chmod=0755 deploy/exp02/entrypoint.sh /usr/local/bin/simai-exp02
RUN sed -i 's/\r$//' /usr/local/bin/simai-tier0 /usr/local/bin/simai-wait-tier0 /usr/local/bin/simai-bootstrap /usr/local/bin/simai-exp02
RUN sed -i '1c#!/opt/simai/ai/.venv/bin/python' /usr/local/bin/simai-bootstrap
COPY deploy/weights.json /opt/simai/deploy/weights.json

# SIMAI_SERVICES: simai-bootstrap tier0 가 띄울 프로세스(Tier 0 → vLLM 서버 → 분석 워커). 'tier0' 로 주면 Tier 0 단독(tier0 이미지와 같은 동작).
#   avatar·ppe 는 명시 선택이다(ppe: 입력 공유 파일·outbox 로 Tier 0 I1 을 거쳐 ppe_state 를 보낸다, deploy/README.md "감시 프로세스" 5).
# SIMAI_WORKER_WEIGHTS_SET: Tier 0 준비 뒤에 받는 워커 가중치 묶음(Cosmos3-Nano 약 31.5GB, 받는 동안에도 Tier 0 는 돈다).
# NVIDIA_DRIVER_CAPABILITIES 는 합집합: video(NVDEC 선택 디코더) + graphics(pyrender EGL, HMR).
# SIMAI_MAPANYTHING_REPO·_COMMIT: MapAnything 이미지(deploy/mapanything)와 같은 이름. 장면 준비 모드(simai-bootstrap scene)는 static 묶음을 받는다
ENV SIMAI_IMAGE_KIND=unified \
    SIMAI_MAPANYTHING_REPO=/opt/simai/mapanything/map-anything \
    SIMAI_MAPANYTHING_COMMIT=${MA_COMMIT} \
    SIMAI_WM2_REPO=/opt/simai/worldmirror2/HY-World-2.0 \
    SIMAI_WM2_COMMIT=${WM2_COMMIT} \
    SIMAI_SERVICES=tier0,vllm,worker \
    SIMAI_WORKER_WEIGHTS_SET=vlm \
    SIMAI_CONTRACTS_DIR=/opt/simai/contracts \
    SIMAI_WEIGHTS_MANIFEST=/opt/simai/deploy/weights.json \
    SIMAI_FS3DB_REPO=/opt/simai/hmr/Fast-SAM-3D-Body \
    PYOPENGL_PLATFORM=egl \
    YOLO_AUTOINSTALL=false \
    YOLO_OFFLINE=true \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simai-start", "/usr/local/bin/simai-bootstrap"]
CMD ["tier0"]
