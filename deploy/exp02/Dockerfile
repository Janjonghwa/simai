# EXP-02 Fast SAM 3D Body 실험 이미지 (S15P31A101-32 골격 → -207 마무리, 명세 3.12 "Docker 이미지는 E-GATE-1 전에 만든다", 20.5).
# ai/experiments/hmr/setup_runpod.sh 의 Network Volume 환경(micromamba, torch 2.5.1+cu124)을 어느 GPU·지역에서나 도는 이미지로 옮긴 것이다.
# - torch 2.7.1+cu128: Blackwell(sm_100·sm_120) 커널이 있는 판. 고른 근거와 E-GATE-0 대조 기준은 docs/design/exp-images.md.
# - Detectron2 CUDA 확장은 빌드 단계(CUDA 12.8 devel)에서 Ampere 이후 전 세대(TORCH_CUDA_ARCH_LIST)로 미리 컴파일해 휠로 옮긴다.
#   실행 이미지는 CUDA base 라 nvcc·devel 층(압축 약 5GB)을 레지스트리에 올리지 않는다.
# - 코드: Fast-SAM-3D-Body @d72aa36(수정 없음), DINOv3 허브 코드 @6876159(가중치 없음. Fast 가 이 로컬 캐시를 GitHub 보다 먼저 쓴다).
# - 가중치(sam-3d-body-dinov3, MoGe2, YOLO26x-pose)는 이미지에 없다. 시작할 때 simai-bootstrap 이 deploy/weights.json 의 exp02 묶음을
#   저장 위치에 받고 sha256 을 확인한다(HF 토큰은 환경 변수 HF_TOKEN 만, 자식 프로세스에 넘기지 않음).
ARG BUILD_IMAGE=simai-base-cu128-devel
ARG BASE_IMAGE=simai-base-cu128

# ---------------------------------------------------------------- 0) 잠금에서 층 목록 (deploy/tier0/Dockerfile 과 같은 방식)
FROM ${BASE_IMAGE} AS layer-lists
WORKDIR /src
COPY deploy/exp02/requirements.lock.txt all.txt
RUN set -eu; \
    pick(){ out=$1; shift; : > "$out"; \
      for n in "$@"; do \
        awk -v n="$n" '/^[^ #]/ { x = $1; sub(/[ =;@].*/, "", x); keep = (x == n) } keep && !/^ *#/' all.txt > one.txt; \
        grep -Eq "^$n( @ |==)" one.txt || { echo "층 묶음의 $n 이 잠금에 없다(deploy/exp02/Dockerfile 묶음을 고친다)" >&2; exit 1; }; \
        cat one.txt >> "$out"; \
      done; }; \
    pick cuda.txt nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cufft-cu12 nvidia-cusolver-cu12 nvidia-cusparse-cu12 \
      nvidia-cusparselt-cu12 nvidia-nccl-cu12 nvidia-cuda-nvrtc-cu12 nvidia-curand-cu12 nvidia-nvjitlink-cu12 \
      nvidia-cuda-runtime-cu12 nvidia-cuda-cupti-cu12 nvidia-cufile-cu12 nvidia-nvtx-cu12; \
    pick torch.txt torch torchvision triton; \
    rm one.txt

# ---------------------------------------------------------------- 1) 빌드: Detectron2·chumpy·MoGe 휠, 코드 사본
FROM ${BUILD_IMAGE} AS build
ARG TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0;10.0;12.0"
ARG MAX_JOBS=2
ARG FS_COMMIT=d72aa36
ARG DETECTRON2_COMMIT=a1ce2f9
ARG MOGE_COMMIT=74fbce0
ARG DINOV3_COMMIT=6876159a11b4df116f30f667f8c9888617df0751
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY deploy/exp02/requirements.lock.txt /opt/build/requirements.lock.txt
# torch 가 있어야 Detectron2 를 빌드할 수 있다(빌드용 환경. 실행 이미지에는 만든 휠만 옮긴다)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv -q /opt/build/.venv \
 && uv pip install -q --python /opt/build/.venv/bin/python -r /opt/build/requirements.lock.txt
# 잠금 밖 3개를 휠로: chumpy(setup.py 가 pip 를 import), Detectron2(빌드에 torch 필요), MoGe(의존성 flex-gemm·gradio 를 빼려고 --no-deps)
RUN CUDA_HOME=/usr/local/cuda TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST}" MAX_JOBS=${MAX_JOBS} FORCE_CUDA=1 \
    /opt/build/.venv/bin/python -m pip wheel -q --no-deps --no-build-isolation -w /opt/wheels \
      chumpy==0.70 \
      "detectron2 @ git+https://github.com/facebookresearch/detectron2.git@${DETECTRON2_COMMIT}" \
      "moge @ git+https://github.com/microsoft/MoGe.git@${MOGE_COMMIT}" \
 && echo "${TORCH_CUDA_ARCH_LIST}" > /opt/wheels/detectron2_arch.txt
RUN git clone -q https://github.com/yangtiming/Fast-SAM-3D-Body /opt/code/Fast-SAM-3D-Body \
 && git -C /opt/code/Fast-SAM-3D-Body checkout -q "${FS_COMMIT}" \
 && rm -rf /opt/code/Fast-SAM-3D-Body/.git \
 && echo "Fast 저장소에 커밋된 가중치(mhr2smpl 실험용, 이미지에 넣지 않음):" \
 && find /opt/code/Fast-SAM-3D-Body -type f \( -name '*.pth' -o -name '*.pt' -o -name '*.ckpt' -o -name '*.safetensors' -o -name '*.onnx' -o -name '*.engine' \) -print -delete \
 && git clone -q https://github.com/facebookresearch/dinov3 /opt/code/facebookresearch_dinov3_main \
 && git -C /opt/code/facebookresearch_dinov3_main checkout -q "${DINOV3_COMMIT}" \
 && rm -rf /opt/code/facebookresearch_dinov3_main/.git

# ---------------------------------------------------------------- 2) 실행 이미지 (CUDA base + 잠근 휠 + 빌드한 휠)
FROM ${BASE_IMAGE}
# pyrender EGL 렌더용 glvnd 로더(libEGL.so 무버전 이름은 libegl-dev 가 준다), torch.compile(triton) 런처를 컴파일할 C 컴파일러,
# git: 잠금의 MoGe 의존성 두 개(utils3d_moge·pipeline)가 커밋 고정 git 주소다
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libegl1 libegl-dev libgl1 libglvnd0 libglib2.0-0 gcc libc6-dev git \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/exp02/.venv \
    PATH=/opt/exp02/.venv/bin:${PATH}
RUN uv venv -q /opt/exp02/.venv
# 큰 휠을 묶음별 층으로(해시 확인) → 잠금 전체(이미 깐 것은 건너뜀) → 빌드한 휠(--no-deps 라 잠금의 판을 바꾸지 않는다).
# MoGe 의 flex-gemm·gradio 가 빠져 있어 uv pip check 는 실패하므로 import 점검(check_image.sh)으로 본다.
COPY --from=layer-lists /src/cuda.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/cuda.txt
COPY --from=layer-lists /src/torch.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/torch.txt
COPY deploy/exp02/requirements.lock.txt /opt/exp02/requirements.lock.txt
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q -r /opt/exp02/requirements.lock.txt
# pyrender 는 import 때 pyglet(X11 libXrender)을 연다. 볼륨 venv 가 돌던 RunPod PyTorch 이미지에는 있던 라이브러리(-207 check_image 에서 발견)
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libxrender1 libxext6 libsm6 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/wheels /tmp/wheels
RUN uv pip install -q --no-deps /tmp/wheels/*.whl \
 && cp /tmp/wheels/detectron2_arch.txt /opt/exp02/.detectron2_arch \
 && rm -rf /tmp/wheels
COPY --from=build /opt/code/Fast-SAM-3D-Body /opt/exp02/Fast-SAM-3D-Body
COPY --from=build /opt/code/facebookresearch_dinov3_main /root/.cache/torch/hub/facebookresearch_dinov3_main

COPY --chmod=0755 deploy/exp02/entrypoint.sh /usr/local/bin/simai-exp02
COPY --chmod=0755 deploy/scripts/simai_bootstrap.py /usr/local/bin/simai-bootstrap
# 윈도 체크아웃의 CRLF 가 shebang 을 깨뜨리지 않게 빌드에서 CR 을 뺀다(check_image.sh 는 결과를 다시 검사한다)
RUN sed -i 's/\r$//' /usr/local/bin/simai-exp02 /usr/local/bin/simai-bootstrap
COPY deploy/weights.json /opt/simai/deploy/weights.json

# SIMAI_ROOT 는 고정하지 않는다(simai-bootstrap 이 SIMAI_ROOT → /workspace/simai → /srv/simai 순서로 고른다).
ENV SIMAI_IMAGE_KIND=exp02 \
    SIMAI_WEIGHTS_SET=exp02 \
    SIMAI_WEIGHTS_MANIFEST=/opt/simai/deploy/weights.json \
    SIMAI_FS3DB_REPO=/opt/exp02/Fast-SAM-3D-Body \
    PYOPENGL_PLATFORM=egl \
    YOLO_AUTOINSTALL=false \
    YOLO_OFFLINE=true \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

# 기본은 SSH 로 들어가 쓰는 개발 Pod(sleep infinity, 준비 없음). 준비 → 실행은 'simai-bootstrap run simai-exp02 python ...',
# 세션 큐는 'simai-bootstrap queue'(deploy/README.md "실험 이미지").
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simai-start", "/usr/local/bin/simai-bootstrap"]
CMD ["sleep", "infinity"]
