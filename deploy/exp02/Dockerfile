# EXP-02 Fast SAM 3D Body 실험 이미지 (골격, E-GATE-1 전, 명세 3.12 "Docker 이미지는 E-GATE-1 전에 만든다", 20.5).
# ai/experiments/hmr/setup_runpod.sh 의 Network Volume 환경(micromamba)을 이미지로 옮긴 것이다.
# - 코드: Fast-SAM-3D-Body @d72aa36 를 이미지에 넣는다(수정 없음). SIMAI_FS3DB_REPO 가 이 위치를 가리킨다.
# - 체크포인트(sam-3d-body-dinov3, YOLO26x-pose)·데이터·실험 산출물은 이미지에 없다. Network Volume(/workspace/simai)에서 읽는다.
# - HF 토큰은 이미지에 넣지 않는다. 가중치 받기는 기존 setup_runpod.sh weights 가 /workspace/.simai-private/hf.env 를 읽어 한다.
# - Detectron2 CUDA 확장은 대상 GPU 아키텍처로 빌드한다(기본 9.0 = H100). GPU 가 바뀌면 TORCH_CUDA_ARCH_LIST 를 바꿔 다시 빌드한다.
ARG BASE_IMAGE=simai-base-cu124-devel
FROM ${BASE_IMAGE}

ARG TORCH_CUDA_ARCH_LIST=9.0
ARG MAX_JOBS=4
ARG FS_COMMIT=d72aa36
ARG DETECTRON2_COMMIT=a1ce2f9
ARG MOGE_COMMIT=74fbce0

# git·빌드 도구(Detectron2·chumpy 빌드), pyrender EGL 렌더용 glvnd 로더(libEGL.so 무버전 이름은 libegl-dev 가 준다)
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      git build-essential libegl1 libegl-dev libgl1 libglvnd0 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/exp02/.venv \
    PATH=/opt/exp02/.venv/bin:${PATH} \
    CUDA_HOME=/usr/local/cuda \
    TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST}

# 1) 잠근 의존성 (deploy/exp02/requirements.lock.txt, 해시 확인). 잠금에 없는 것은 아래에서 --no-deps 로만 넣는다.
COPY deploy/exp02/requirements.lock.txt /opt/exp02/requirements.lock.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/exp02/.venv \
 && uv pip install --python /opt/exp02/.venv/bin/python -r /opt/exp02/requirements.lock.txt

# 2) 잠금 밖 3개: chumpy(setup.py 가 pip 를 import), Detectron2(빌드에 torch 필요), MoGe(의존성 flex-gemm 이 torch 2.5.1 과 충돌)
#    --no-deps 라 잠금의 버전을 바꾸지 않는다. MoGe 의 flex-gemm·gradio 가 빠져 있어 uv pip check 는 실패하므로 import 점검으로 본다.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/exp02/.venv/bin/python --no-deps --no-build-isolation chumpy==0.70 \
 && MAX_JOBS=${MAX_JOBS} uv pip install --python /opt/exp02/.venv/bin/python --no-deps --no-build-isolation \
      "detectron2 @ git+https://github.com/facebookresearch/detectron2.git@${DETECTRON2_COMMIT}" \
 && uv pip install --python /opt/exp02/.venv/bin/python --no-deps \
      "moge @ git+https://github.com/microsoft/MoGe.git@${MOGE_COMMIT}" \
 && echo "${TORCH_CUDA_ARCH_LIST}" > /opt/exp02/.detectron2_arch

# 3) Fast-SAM-3D-Body 코드 (가중치 없음)
RUN git clone -q https://github.com/yangtiming/Fast-SAM-3D-Body /opt/exp02/Fast-SAM-3D-Body \
 && git -C /opt/exp02/Fast-SAM-3D-Body checkout -q "${FS_COMMIT}" \
 && rm -rf /opt/exp02/Fast-SAM-3D-Body/.git

COPY --chmod=0755 deploy/exp02/entrypoint.sh /usr/local/bin/simai-exp02

ENV SIMAI_ROOT=/workspace/simai \
    SIMAI_FS3DB_REPO=/opt/exp02/Fast-SAM-3D-Body \
    PYOPENGL_PLATFORM=egl \
    YOLO_AUTOINSTALL=false \
    YOLO_CONFIG_DIR=/tmp/ultralytics \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/simai-start", "/usr/local/bin/simai-exp02"]
CMD ["bash"]
