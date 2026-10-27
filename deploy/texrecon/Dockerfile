# EXP-01 텍스처 메시 굽기 도구 이미지 (S15P31A101-298): mvs-texturing `texrecon`(Waechter 2014, BSD-3-Clause), CPU 전용.
# 노트북 Docker(Linux 컨테이너)에서 돈다. GPU·가중치·비밀이 없다. 쓰는 곳: ai/experiments/static_scene/texture_mesh.py
# (`--texrecon docker:simai/texrecon:dev`). 설계·정한 수치는 docs/design/exp01-textured-mesh.md.
# - 코드는 모두 커밋으로 고정한다. 상위 elibs/CMakeLists.txt 는 mve(브랜치 기본값)·rayint(cuda 브랜치)를 커밋 없이 받으므로
#   빌드 전에 GIT_TAG 를 아래 커밋으로 박는다(받는 시점에 따라 결과가 달라지지 않게).
# - OpenMVS TextureMesh 는 AGPL 이라 쓰지 않는다(설계 문서 "버린 대안").
#   docker buildx bake texrecon      # 또는: docker build -f deploy/texrecon/Dockerfile -t simai/texrecon:dev .
ARG UBUNTU_IMAGE=ubuntu:22.04@sha256:e0f80c829509ef4df9e76bd8944eb7b8ec25cbb379b112a7f9448b2c77a77c9b

FROM ${UBUNTU_IMAGE} AS build
ARG MVST_COMMIT=f3374298ac959cb5afe47a14e4d35d2ac7fbdbb1
ARG MVE_COMMIT=91c0f3fde0781399b2fe872d8e52e4899274a84a
ARG RAYINT_COMMIT=b62c8905f6dd11a179128517e814e0e00f9bb809
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
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

FROM ${UBUNTU_IMAGE}
# hadolint ignore=DL3008
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libpng16-16 libjpeg-turbo8 libtiff5 libtbb12 libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/mvs-texturing/build/apps/texrecon/texrecon /usr/local/bin/texrecon
COPY --from=build /src/mvs-texturing/LICENSE.txt /usr/local/share/licenses/mvs-texturing/LICENSE.txt
ENV SIMAI_IMAGE_KIND=texrecon
RUN useradd -m -u 10001 simai
USER simai
WORKDIR /work
ENTRYPOINT ["texrecon"]
