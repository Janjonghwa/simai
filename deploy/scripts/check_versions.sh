#!/bin/sh
# 프로젝트 설정의 판과 배포·CI 이미지(deploy/docker-bake.hcl)의 판이 같은지 본다(S15P31A101-28). 저장소 최상위에서 실행한다.
#   sh deploy/scripts/check_versions.sh
# 어긋나면 web·server·Jenkins 이미지 빌드가 깨지거나(front 의 engineStrict, Gradle toolchain) CI 가 다른 판으로 검사한다.
# 고칠 때는 docker-bake.hcl 의 이미지 태그와 다이제스트를 함께 바꾼다. pnpm 판은 corepack 이 packageManager 에서 읽으므로 보지 않는다.
set -eu

bake=deploy/docker-bake.hcl
fail=0

# bake 변수의 기본값(이미지 참조)
bake_var() {
    sed -n "s/^variable \"$1\" { default = \"\([^\"]*\)\" }$/\1/p" "$bake"
}

check() {
    if [ "$2" = "$3" ]; then
        echo "OK   $1: $2"
    else
        echo "FAIL $1: 프로젝트 $2, 이미지 $3" >&2
        fail=1
    fi
}

# Node: front/.node-version 과 NODE_WEB(node:<판>-...)
node_project=$(tr -d ' \r\n' < front/.node-version)
node_image=$(bake_var NODE_WEB | sed -n 's/^node:\([0-9.]*\)-.*/\1/p')
check "Node (front/.node-version ↔ NODE_WEB)" "$node_project" "$node_image"

# Java: server/build.gradle toolchain 과 JDK·JRE·Jenkins 이미지의 주 판
java_project=$(sed -n 's/.*JavaLanguageVersion\.of(\([0-9]*\)).*/\1/p' server/build.gradle)
check "Java (build.gradle ↔ JDK_SERVER)" "$java_project" "$(bake_var JDK_SERVER | sed -n 's/^eclipse-temurin:\([0-9]*\)-.*/\1/p')"
check "Java (build.gradle ↔ JRE_SERVER)" "$java_project" "$(bake_var JRE_SERVER | sed -n 's/^eclipse-temurin:\([0-9]*\)-.*/\1/p')"
check "Java (build.gradle ↔ JENKINS_IMAGE)" "$java_project" "$(bake_var JENKINS_IMAGE | sed -n 's/.*-jdk\([0-9]*\)@.*/\1/p')"

# Python: requires-python 의 하한(>=3.11)과 PYTHON_VERSION 의 주.부 판
python_image=$(sed -n 's/^variable "PYTHON_VERSION" { default = "\([0-9]*\.[0-9]*\)\.[0-9]*" }$/\1/p' "$bake")
for p in ai/pyproject.toml gateway/*/pyproject.toml; do
    py_project=$(sed -n 's/^requires-python = ">=\([0-9]*\.[0-9]*\).*/\1/p' "$p")
    check "Python ($p ↔ PYTHON_VERSION)" "$py_project" "$python_image"
done

exit $fail
