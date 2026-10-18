# simai 이미지 빌드 정의. 저장소 최상위에서 실행한다 (자세한 절차: deploy/README.md).
#   docker buildx bake tier0                     # 베이스(cu130) → Tier 0, 로컬 이미지로 적재
#   docker buildx bake exp02                     # 베이스(cu124 devel) → EXP-02
#   REGISTRY=<레지스트리>/simai TAG=$(git rev-parse --short=12 HEAD) docker buildx bake --push tier0   # 푸시(사람이 확인한 뒤)
# 외부 이미지는 모두 다이제스트로 고정한다. 바꿀 때는 태그와 다이제스트를 함께 바꾸고 MR 에 적는다.

variable "REGISTRY" { default = "simai" }
variable "TAG" { default = "dev" }

# docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04 (CUDA 13.0 런타임 + 순방향 호환 라이브러리. torch 2.14.1 휠의 nvidia-cuda-runtime 13.0.96 과 같은 판)
variable "CUDA_TIER0" { default = "nvidia/cuda:13.0.3-base-ubuntu24.04@sha256:7c7413a56200486f71f181cad9310f6fd31b6bb21816ade15fc9c1e1e927a5c1" }
# docker.io/nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04 (EXP-02: torch 2.5.1+cu124, Detectron2 빌드용 nvcc)
variable "CUDA_EXP02" { default = "nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04@sha256:622e78a1d02c0f90ed900e3985d6c975d8e2dc9ee5e61643aed587dcf9129f42" }
# ghcr.io/astral-sh/uv:0.11.27 (노트북 uv 와 같은 판)
variable "UV_IMAGE" { default = "ghcr.io/astral-sh/uv:0.11.27@sha256:4d01caf3b22dfd11003455e2e68153da08c4ee1fa54fdbd166c6282d22693419" }
variable "PYTHON_VERSION" { default = "3.11.15" }
# EXP-02 Detectron2 CUDA 확장을 빌드할 GPU 아키텍처 (H100·H200 9.0, A40 8.6, RTX 4070 8.9)
variable "EXP02_CUDA_ARCH" { default = "9.0" }

group "default" { targets = ["tier0"] }

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

target "base-cu124-devel" {
  inherits   = ["_common"]
  dockerfile = "deploy/base/Dockerfile"
  args = {
    CUDA_IMAGE     = "${CUDA_EXP02}"
    UV_IMAGE       = "${UV_IMAGE}"
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
  tags = ["${REGISTRY}/base:cu124-devel-${TAG}"]
}

target "exp02" {
  inherits   = ["_common"]
  dockerfile = "deploy/exp02/Dockerfile"
  contexts   = { simai-base-cu124-devel = "target:base-cu124-devel" }
  args = {
    BASE_IMAGE           = "simai-base-cu124-devel"
    TORCH_CUDA_ARCH_LIST = "${EXP02_CUDA_ARCH}"
  }
  tags = ["${REGISTRY}/exp02-fs3db:${TAG}"]
}
