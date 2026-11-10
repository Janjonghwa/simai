#!/bin/sh
# EC2 자동 배포(S15P31A101-254). 절차: docs/design/ec2-deploy.md "자동 배포"
# 호스트의 /opt/simai/bin/deploy.sh 로 설치해 두고, Jenkins 배포 키가 이 스크립트만 실행한다
# (authorized_keys 의 restrict,command=). 저장소의 이 파일을 고치면 설치본도 다시 복사한다.
#   입력: SSH_ORIGINAL_COMMAND = "deploy <커밋 40자>", 표준 입력 = 그 커밋의 git archive(tar)
#   순서: 소스 풀기 → bake ec2 → (마이그레이션이 바뀌었으면) DB 덤프 → SIMAI_TAG 바꾸기 → up --wait → https 확인
#         → 오래된 이미지 정리
#   실패하면 이전 소스·이전 태그로 다시 띄우고 1 로 끝난다(스키마는 돌아오지 않는다. 덤프로 되살린다)
set -eu

SIMAI=/opt/simai
ENV_FILE=$SIMAI/.env
# jenkins·ci-db 는 넣지 않는다. 배포가 자기를 돌리는 Jenkins 를 다시 띄우면 안 된다
SERVICES="web certbot server db risk-engine"
# 태그를 몇 판까지 남길지(이전 판으로 되돌리기용)
KEEP=3
BACKUPS=$SIMAI/backups
KEEP_BACKUPS=5
MIGRATION=server/src/main/resources/db/migration

fail() { echo "deploy: $*" >&2; exit 2; }

# shellcheck disable=SC2086 # 일부러 낱말로 나눈다
set -- ${SSH_ORIGINAL_COMMAND:-}
[ "$#" -eq 2 ] && [ "$1" = deploy ] || fail "사용법: deploy <커밋 40자>"
case "$2" in *[!0-9a-f]*) fail "커밋이 16진수가 아니다: $2" ;; esac
[ "${#2}" -eq 40 ] || fail "커밋은 40자다: $2"
COMMIT=$2
TAG=$(printf %s "$COMMIT" | cut -c1-12)

exec 9>"$SIMAI/deploy.lock"
flock -w 1800 9 || fail "다른 배포가 30분 넘게 끝나지 않는다"

# freeze.sh 와 같은 잠금 안에서 확인한다. 입력을 비워 SSH 파이프의 git archive 가 정상 종료하게 한다.
if [ -f "$SIMAI/deploy.freeze" ]; then
    expiry=$(sed -n '1p' "$SIMAI/deploy.freeze")
    reason=$(sed -n '2p' "$SIMAI/deploy.freeze")
    case "$expiry" in ''|*[!0-9]*) fail "동결 파일의 만료 시각이 올바르지 않다" ;; esac
    if [ "$expiry" -gt "$(date -u +%s)" ]; then
        echo "deploy: 동결 중 (사유: $reason, 만료: $(date -u -d "@$expiry" '+%Y-%m-%d %H:%M:%S UTC')) — 배포 건너뜀"
        cat > /dev/null
        exit 0
    fi
    echo "deploy: 경고: 동결이 만료되어 배포한다 (사유: $reason)" >&2
fi

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
PREV=$(env_get SIMAI_TAG)
DOMAIN=$(env_get SIMAI_DOMAIN)
REGISTRY=$(env_get SIMAI_REGISTRY)
REGISTRY=${REGISTRY:-simai}
echo "deploy: $PREV → $TAG ($COMMIT)"

rm -rf "$SIMAI/repo.new"
mkdir -p "$SIMAI/repo.new"
tar -x -C "$SIMAI/repo.new"
echo "$TAG" > "$SIMAI/repo.new/REVISION"

(cd "$SIMAI/repo.new" && TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl --progress=plain ec2)

# Flyway 는 앞으로만 간다. 마이그레이션(V·R)이 바뀐 배포는 띄우기 전에 지금 DB 를 덤프한다. 덤프가 실패하면 배포하지 않는다
if ! diff -r -q "$SIMAI/repo/$MIGRATION" "$SIMAI/repo.new/$MIGRATION" > /dev/null 2>&1; then
    mkdir -p -m 700 "$BACKUPS"
    dump="$BACKUPS/simai-$(date +%Y%m%d-%H%M%S)-$PREV.dump"
    docker compose --env-file "$ENV_FILE" -f "$SIMAI/repo/deploy/ec2/compose.yaml" exec -T db \
        pg_dump -U postgres -Fc simai > "$dump.part"
    mv "$dump.part" "$dump"
    echo "deploy: 마이그레이션이 바뀌어 DB 를 덤프했다: $dump"
    # shellcheck disable=SC2012 # 이름은 이 스크립트가 만든 것뿐이다
    ls -1t "$BACKUPS"/*.dump | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
fi

# 이미지가 다 만들어진 뒤에 소스를 바꾼다. 되돌리기용으로 이전 소스는 repo.old 에 둔다
rm -rf "$SIMAI/repo.old"
mv "$SIMAI/repo" "$SIMAI/repo.old"
mv "$SIMAI/repo.new" "$SIMAI/repo"

up() {
    sed -i "s/^SIMAI_TAG=.*/SIMAI_TAG=$1/" "$ENV_FILE"
    # shellcheck disable=SC2086
    docker compose --env-file "$ENV_FILE" -f "$SIMAI/repo/deploy/ec2/compose.yaml" up -d --wait --wait-timeout 300 $SERVICES
}

check() {
    # 컨테이너 healthcheck(--wait) 다음에 밖에서 보는 길(Nginx → 화면)도 확인한다
    curl -fsS -o /dev/null --max-time 10 --retry 5 --retry-delay 3 --retry-all-errors "https://$DOMAIN/"
}

if up "$TAG" && check; then
    echo "deploy: $TAG 배포 완료"
else
    echo "deploy: $TAG 실패, $PREV 로 되돌린다" >&2
    rm -rf "$SIMAI/repo.failed"
    mv "$SIMAI/repo" "$SIMAI/repo.failed"
    mv "$SIMAI/repo.old" "$SIMAI/repo"
    up "$PREV" || echo "deploy: 되돌리기도 실패했다. 손으로 확인한다(docs/design/ec2-deploy.md)" >&2
    exit 1
fi

# 지금·이전 판을 빼고 최근 KEEP 판보다 오래된 태그를 지운다(image ls 는 최근 것부터 나온다)
for name in server web risk-engine; do
    docker image ls "$REGISTRY/$name" --format '{{.Tag}}' | tail -n +$((KEEP + 1)) | while read -r old; do
        [ "$old" = "$TAG" ] || [ "$old" = "$PREV" ] || docker image rm "$REGISTRY/$name:$old" > /dev/null || true
    done
done
docker image prune -f > /dev/null
