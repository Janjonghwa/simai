# CI 용 Jenkins 이미지(S15P31A101-28). 빌드: 저장소 최상위에서 docker buildx bake jenkins (deploy/docker-bake.hcl).
# - 검사는 Docker 없이 이 컨테이너 안에서 직접 돌린다. 호스트 Docker 소켓을 받지 않는다(받으면 호스트 root 와 같다, docs/design/ci-pipeline.md).
#   그래서 각 파트의 도구를 이미지에 넣는다. 판은 docker-bake.hcl 의 변수로 배포 이미지와 같게 맞춘다.
#   - JDK 21: Jenkins 공식 lts-jdk21(server/build.gradle toolchain)
#   - Node·corepack(pnpm 판은 front/package.json 의 packageManager): NODE_IMAGE(= 관제 앱 빌드 이미지)
#   - uv·Python 3.11: UV_IMAGE·PYTHON_VERSION(= Risk Engine·Tier 0 이미지)
#   - git(ai 시험이 저장소 git 정보를 읽는다), libtk8.6(site_alert Tk 시험), postgresql-client(서버 시험 DB 를 빌드마다 새로 만든다)
# - 플러그인은 plugins.txt 로 넣는다(판은 빌드 때의 최신 호환판). 설정·빌드 기록은 jenkins-home 볼륨에 남는다.
# - 빌드 컨텍스트는 저장소 최상위이고, 들어오는 파일은 deploy/jenkins/Dockerfile.dockerignore 의 허용 목록뿐이다.
ARG JENKINS_IMAGE
ARG NODE_IMAGE
ARG UV_IMAGE

FROM ${NODE_IMAGE} AS node
FROM ${UV_IMAGE} AS uv

FROM ${JENKINS_IMAGE}
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends libtk8.6 postgresql-client \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/corepack /usr/local/lib/node_modules/corepack
RUN ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
 && corepack enable pnpm

COPY --from=uv /uv /uvx /usr/local/bin/
ARG PYTHON_VERSION
# uv 가 관리하는 Python 을 이미지에 미리 넣는다(잡이 내려받지 않게). 잡은 jenkins 계정이라 읽기만 한다
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_DOWNLOADS=never \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN UV_PYTHON_DOWNLOADS=automatic uv python install ${PYTHON_VERSION} \
 && chmod -R a+rX /opt/uv

USER jenkins
COPY deploy/jenkins/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
