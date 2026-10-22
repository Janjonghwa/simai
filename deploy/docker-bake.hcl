# simai 이미지 빌드 정의. 저장소 최상위에서 실행한다 (자세한 절차: deploy/README.md).
#   docker buildx bake tier0                     # 베이스(cu130) → Tier 0, 로컬 이미지로 적재
#   docker buildx bake exp02                     # 베이스(cu128 base) → EXP-02 HMR (Detectron2 는 CUDA 확장 없이 빌드)
#   docker buildx bake mapanything               # 베이스(cu128 base, Python 3.12) → EXP-01 MapAnything
#   docker buildx bake risk-engine               # EC2 Risk Engine 컨테이너(CPU, python slim, S15P31A101-99)
#   REGISTRY=<레지스트리>/simai TAG=$(git rev-parse --short=12 HEAD) docker buildx bake --push tier0   # 푸시(사람이 확인한 뒤)
# 외부 이미지는 모두 다이제스트로 고정한다. 바꿀 때는 태그와 다이제스트를 함께 바꾸고 MR 에 적는다.

variable "REGISTRY" { default = "simai" }
variable "TAG" { default = "dev" }

# docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04 (CUDA 13.0 런타임 + 순방향 호환 라이브러리. torch 2.14.1 휠의 nvidia-cuda-runtime 13.0.96 과 같은 판)
variable "CUDA_TIER0" { default = "nvidia/cuda:13.0.3-base-ubuntu24.04@sha256:7c7413a56200486f71f181cad9310f6fd31b6bb21816ade15fc9c1e1e927a5c1" }
# 실험 이미지(S15P31A101-207): torch 2.7.1+cu128 휠이 CUDA 12.8 라이브러리를 직접 가져오므로 base. Detectron2 도 CUDA 확장 없이 base 에서 빌드(S15P31A101-238)
# docker.io/nvidia/cuda:12.8.1-base-ubuntu24.04
variable "CUDA_EXP" { default = "nvidia/cuda:12.8.1-base-ubuntu24.04@sha256:133c78a0575303be34164d0b90137a042172bdf60696af01a3c424ab402d86e2" }
# ghcr.io/astral-sh/uv:0.11.27 (노트북 uv 와 같은 판)
variable "UV_IMAGE" { default = "ghcr.io/astral-sh/uv:0.11.27@sha256:4d01caf3b22dfd11003455e2e68153da08c4ee1fa54fdbd166c6282d22693419" }
variable "PYTHON_VERSION" { default = "3.11.15" }
# docker.io/library/python:3.11.15-slim-trixie (EC2 Risk Engine 컨테이너, CPU 전용. Tier 0 와 같은 Python 판)
variable "PYTHON_RISK_ENGINE" { default = "python:3.11.15-slim-trixie@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff" }
# MapAnything 은 볼륨 venv(Python 3.12)와 같은 판대
variable "PYTHON_VERSION_MAPANYTHING" { default = "3.12.13" }
# Detectron2 CPU 확장 컴파일 병렬 수. Docker Desktop 메모리가 작으면(2GB) 1~2
variable "EXP02_MAX_JOBS" { default = "2" }

group "default" { targets = ["tier0"] }
group "exp" { targets = ["exp02", "mapanything"] }

target "_common" {
  context   = "."
  platforms = ["linux/amd64"]
  labels = {
    "org.opencontainers.image.source"   = "S15P31A101 simai"
    "org.opencontainers.image.revision" = "${TAG}"
  }
}

target "base-cu130" {
  inherits   = ["_common"]
  dockerfile = "deploy/base/Dockerfile"
  args = {
    CUDA_IMAGE     = "${CUDA_TIER0}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
  tags = ["${REGISTRY}/base:cu130-${TAG}"]
}

target "tier0" {
  inherits   = ["_common"]
  dockerfile = "deploy/tier0/Dockerfile"
  contexts   = { simai-base = "target:base-cu130" }
  args       = { BASE_IMAGE = "simai-base" }
  tags       = ["${REGISTRY}/tier0:${TAG}"]
}

target "base-cu128" {
  inherits   = ["_common"]
  dockerfile = "deploy/base/Dockerfile"
  args = {
    CUDA_IMAGE     = "${CUDA_EXP}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
  tags = ["${REGISTRY}/base:cu128-${TAG}"]
}

target "base-cu128-py312" {
  inherits   = ["_common"]
  dockerfile = "deploy/base/Dockerfile"
  args = {
    CUDA_IMAGE     = "${CUDA_EXP}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION_MAPANYTHING}"
  }
  tags = ["${REGISTRY}/base:cu128-py312-${TAG}"]
}

target "exp02" {
  inherits   = ["_common"]
  dockerfile = "deploy/exp02/Dockerfile"
  contexts = { simai-base-cu128 = "target:base-cu128" }
  args = {
    BASE_IMAGE  = "simai-base-cu128"
    BUILD_IMAGE = "simai-base-cu128"
    MAX_JOBS    = "${EXP02_MAX_JOBS}"
  }
  tags = ["${REGISTRY}/exp02-fs3db:${TAG}"]
}

target "mapanything" {
  inherits   = ["_common"]
  dockerfile = "deploy/mapanything/Dockerfile"
  contexts   = { simai-base-cu128-py312 = "target:base-cu128-py312" }
  args       = { BASE_IMAGE = "simai-base-cu128-py312" }
  tags       = ["${REGISTRY}/mapanything:${TAG}"]
}

# EC2 Risk Engine 컨테이너(S15P31A101-99): CPU 전용, CUDA 베이스를 쓰지 않는다
target "risk-engine" {
  inherits   = ["_common"]
  dockerfile = "deploy/risk-engine/Dockerfile"
  args = {
    PYTHON_IMAGE = "${PYTHON_RISK_ENGINE}"
    UV_IMAGE     = "${UV_IMAGE}"
  }
  tags = ["${REGISTRY}/risk-engine:${TAG}"]
}
