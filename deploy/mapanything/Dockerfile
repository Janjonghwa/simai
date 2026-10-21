# EXP-01 MapAnything 정적 재구성 실험 이미지 (S15P31A101-207, 명세 3.12 "Docker 이미지는 E-GATE-1 전에 만든다", 20.5).
# ai/experiments/static_scene/setup_runpod.sh 의 Network Volume venv(Python 3.12, torch 2.7.1+cu126)를 어느 GPU·지역에서나 도는 이미지로 옮긴 것이다.
# - torch 는 같은 2.7.1 이고 CUDA 빌드만 cu128 (Blackwell sm_100·sm_120 커널 포함). 근거와 E-GATE-0 대조 기준은 docs/design/exp-images.md.
# - 코드: map-anything @3d10cf7(수정 없음, --no-deps 설치 + 사본), DINOv2 허브 코드 @7764ea0(가중치 없음. MapAnything 이 전체 체크포인트를
#   읽을 때 torch.hub 로 인코더 골격만 만든다. 이 로컬 캐시가 있으면 GitHub 에서 받지 않는다).
# - 가중치(facebook/map-anything·map-anything-apache)는 이미지에 없다. 시작할 때 simai-bootstrap 이 deploy/weights.json 의 static 묶음을
#   저장 위치에 받고 sha256 을 확인한다.
ARG BASE_IMAGE=simai-base-cu128-py312

# ---------------------------------------------------------------- 0) 잠금에서 층 목록·코드 사본
FROM ${BASE_IMAGE} AS prep
ARG MA_COMMIT=3d10cf7a3016fc0f9bb13a071ee66c47b10be0d9
ARG DINOV2_COMMIT=7764ea0f912e53c92e82eb78a2a1631e92725fc8
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY deploy/mapanything/requirements.lock.txt all.txt
RUN set -eu; \
    pick(){ out=$1; shift; : > "$out"; \
      for n in "$@"; do \
        awk -v n="$n" '/^[^ #]/ { x = $1; sub(/[ =;@].*/, "", x); keep = (x == n) } keep && !/^ *#/' all.txt > one.txt; \
        grep -Eq "^$n( @ |==)" one.txt || { echo "층 묶음의 $n 이 잠금에 없다(deploy/mapanything/Dockerfile 묶음을 고친다)" >&2; exit 1; }; \
        cat one.txt >> "$out"; \
      done; }; \
    pick cuda.txt nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cufft-cu12 nvidia-cusolver-cu12 nvidia-cusparse-cu12 \
      nvidia-cusparselt-cu12 nvidia-nccl-cu12 nvidia-cuda-nvrtc-cu12 nvidia-curand-cu12 nvidia-nvjitlink-cu12 \
      nvidia-cuda-runtime-cu12 nvidia-cuda-cupti-cu12 nvidia-cufile-cu12 nvidia-nvtx-cu12; \
    pick torch.txt torch torchvision triton; \
    rm one.txt
RUN git clone -q https://github.com/facebookresearch/map-anything /opt/code/map-anything \
 && git -C /opt/code/map-anything checkout -q "${MA_COMMIT}" \
 && rm -rf /opt/code/map-anything/.git \
 && git clone -q https://github.com/facebookresearch/dinov2 /opt/code/facebookresearch_dinov2_main \
 && git -C /opt/code/facebookresearch_dinov2_main checkout -q "${DINOV2_COMMIT}" \
 && rm -rf /opt/code/facebookresearch_dinov2_main/.git

# ---------------------------------------------------------------- 1) 실행 이미지
FROM ${BASE_IMAGE}
ARG MA_COMMIT=3d10cf7a3016fc0f9bb13a071ee66c47b10be0d9
# libgl1·libglib2.0-0: 볼륨 venv 가 돌던 RunPod PyTorch 이미지에 있던 GL·glib 로더(수 MB, 시각화 의존성용), gcc: torch.compile(triton) 런처
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libgl1 libglib2.0-0 gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/mapanything/.venv \
    PATH=/opt/mapanything/.venv/bin:${PATH}
RUN uv venv -q /opt/mapanything/.venv
COPY --from=prep /src/cuda.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/cuda.txt
COPY --from=prep /src/torch.txt /tmp/layers/
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q --no-deps --require-hashes -r /tmp/layers/torch.txt
COPY deploy/mapanything/requirements.lock.txt /opt/mapanything/requirements.lock.txt
RUN --mount=type=cache,target=/root/.cache/uv uv pip install -q -r /opt/mapanything/requirements.lock.txt
COPY --from=prep /opt/code/map-anything /opt/mapanything/map-anything
# 의존성은 잠금이 이미 깔았다(--no-deps). 편집 모드가 아니므로 사본을 지워도 되지만 run_mapanything.py 가 사본 경로를 sys.path 에 넣으므로 둔다
RUN uv pip install -q --no-deps /opt/mapanything/map-anything
COPY --from=prep /opt/code/facebookresearch_dinov2_main /root/.cache/torch/hub/facebookresearch_dinov2_main

COPY --chmod=0755 deploy/scripts/simai_bootstrap.py /usr/local/bin/simai-bootstrap
# 윈도 체크아웃의 CRLF 가 shebang 을 깨뜨리지 않게 빌드에서 CR 을 뺀다(check_image.sh 는 결과를 다시 검사한다)
RUN sed -i 's/\r$//' /usr/local/bin/simai-bootstrap
COPY deploy/weights.json /opt/simai/deploy/weights.json

ENV SIMAI_IMAGE_KIND=mapanything \
    SIMAI_WEIGHTS_SET=static \
    SIMAI_WEIGHTS_MANIFEST=/opt/simai/deploy/weights.json \
    SIMAI_MAPANYTHING_REPO=/opt/mapanything/map-anything \
    SIMAI_MAPANYTHING_COMMIT=${MA_COMMIT} \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

# 기본은 SSH 로 들어가 쓰는 개발 Pod(sleep infinity, 준비 없음). 준비 → 실행은 'simai-bootstrap run python ...', 세션 큐는 'simai-bootstrap queue'.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simai-start", "/usr/local/bin/simai-bootstrap"]
CMD ["sleep", "infinity"]
