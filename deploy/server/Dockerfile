# EC2 Spring 서버 이미지(S15P31A101-66). 빌드: 저장소 최상위에서 docker buildx bake server (deploy/docker-bake.hcl).
# - 빌드 단계는 JDK 로 bootJar 만 만든다. 시험은 CI 가 돌린다(이미지 빌드에서 DB 를 띄우지 않는다).
# - contracts.gradle 이 ../contracts 의 JSON Schema 로 계약 클래스를 만들므로 /src/server 옆에 /src/contracts 를 둔다.
# - 실행 단계는 JRE 만, 비루트 사용자(uid 10002). 비밀 값은 compose 가 환경 변수로 넣는다(이미지에 없음).
# - 빌드 컨텍스트는 저장소 최상위이고, 들어오는 파일은 deploy/server/Dockerfile.dockerignore 의 허용 목록뿐이다.
ARG JDK_IMAGE
ARG JRE_IMAGE

FROM ${JDK_IMAGE} AS build
WORKDIR /src/server
COPY server/gradlew server/build.gradle server/settings.gradle server/contracts.gradle ./
COPY server/gradle ./gradle
COPY server/buildSrc ./buildSrc
# Windows checkout(core.autocrlf)에서 빌드하면 gradlew 가 CRLF 라 sh 가 읽지 못한다
RUN sed -i 's/\r$//' gradlew
COPY contracts /src/contracts
COPY server/src/main ./src/main
RUN --mount=type=cache,target=/root/.gradle \
    sh gradlew --no-daemon bootJar \
 && cp build/libs/*.jar /tmp/simai.jar

FROM ${JRE_IMAGE}
RUN groupadd --system --gid 10002 simai \
 && useradd --system --uid 10002 --gid 10002 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin simai
WORKDIR /opt/simai
COPY --from=build /tmp/simai.jar ./simai.jar
USER simai
# 컨테이너 메모리 상한(compose mem_limit)의 75% 를 힙으로 쓴다
ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75"
EXPOSE 8080
# 관리 명령도 같은 진입점이다: docker compose run --rm server admin create-user ...
ENTRYPOINT ["java", "-jar", "/opt/simai/simai.jar"]
