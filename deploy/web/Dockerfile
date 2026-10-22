# EC2 Nginx 이미지(S15P31A101-66): 관제 앱 정적 파일 + HTTPS 종료 + /api·/ws 를 server 로 넘기기.
# 빌드: 저장소 최상위에서 docker buildx bake web (deploy/docker-bake.hcl).
# - 관제 앱은 빌드할 때 주소를 박는다(Vite). 운영 빌드는 https·wss 주소만 받는다(front/apps/admin/src/lib/parseEnv.ts).
# - 인증서는 이미지에 없다. compose 가 Let's Encrypt 볼륨을 읽기 전용으로 붙인다(docs/design/ec2-deploy.md).
# - 빌드 컨텍스트는 저장소 최상위이고, 들어오는 파일은 deploy/web/Dockerfile.dockerignore 의 허용 목록뿐이다.
ARG NODE_IMAGE
ARG NGINX_IMAGE

FROM ${NODE_IMAGE} AS build
# pnpm 판은 여기 적지 않는다. corepack 이 front/package.json 의 packageManager 를 읽어 그 판을 받는다(front 가 올리면 같이 따라간다)
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable pnpm
WORKDIR /src/front
COPY front ./
ARG VITE_API_BASE_URL
ARG VITE_STOMP_URL
# pnpm install 의 prepare 가 git 훅을 연결하지 않게 한다. 운영 빌드는 msw 를 쓰지 않는다
ENV HUSKY=0 \
    VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_STOMP_URL=${VITE_STOMP_URL} \
    VITE_API_MOCK=false
RUN --mount=type=cache,target=/pnpm-store \
    pnpm install --frozen-lockfile --store-dir /pnpm-store \
 && pnpm --filter admin build

FROM ${NGINX_IMAGE}
RUN rm /etc/nginx/conf.d/default.conf
COPY --from=build /src/front/apps/admin/dist /usr/share/nginx/html
# 이미지의 진입점이 시작할 때 templates/*.template 의 ${SIMAI_DOMAIN} 을 채워 conf.d 에 쓴다
COPY deploy/web/templates/ /etc/nginx/templates/
COPY --chmod=0755 deploy/web/40-simai-certs.sh /docker-entrypoint.d/40-simai-certs.sh
