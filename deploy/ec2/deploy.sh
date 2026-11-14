#!/bin/sh
# EC2 자동 배포(S15P31A101-254). 절차: docs/design/ec2-deploy.md "자동 배포"
# 호스트의 /opt/simai/bin/deploy.sh 로 설치해 두고, Jenkins 배포 키가 이 스크립트만 실행한다
# (authorized_keys 의 restrict,command=). 저장소의 이 파일을 고치면 설치본도 다시 복사한다(install-host.sh).
#   입구 두 개:
#   - Jenkins: SSH_ORIGINAL_COMMAND = "deploy <커밋 40자>", 표준 입력 = 그 커밋의 git archive(tar)
#   - 보류 배포: deploy.sh pending (deploy-pending.sh 가 호스트 타이머로 부른다. 강제 명령 SSH 로는 인자를 줄 수 없다)
#   순서: 동결 확인 → 진행 중 작업·세팅·Tier 0 연결·녹화 업로드 확인(있으면 보류, S15P31A101-676·-682) → 소스 풀기 → bake ec2 → 다시 확인
#         → (마이그레이션이 바뀌었으면) DB 덤프 → SIMAI_TAG 바꾸기 → up --wait → https 확인 → 오래된 이미지 정리
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
# 보류 배포(S15P31A101-676): 보류한 커밋의 tar 하나와 기록 파일 hold 를 둔다. 새 커밋이 오면 이전 보류를 대체한다
PENDING=$SIMAI/pending
# hold 의 줄: 1 커밋, 2 처음 보류 epoch(대체돼도 유지, 안전장치 기준), 3 마지막 보류 epoch, 4 사유, 5 자동 배포 시도 횟수
HOLD=$PENDING/hold
# 안전장치로 넘긴 진행 항목의 키(항목 줄의 앞 두 칸, 작업 종료를 잊은 경우 등). 다음 배포부터 이 키로는 보류하지 않는다
IGNORED=$PENDING/ignored
DB_CONTAINER=simai-ec2-db-1
# 보류 배포가 연달아 실패하면 몇 번까지 다시 해 볼지. 넘으면 새 커밋이 오거나 손으로 배포할 때까지 멈춘다
PENDING_MAX_ATTEMPTS=3

fail() { echo "deploy: $*" >&2; exit 2; }
log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$PENDING/history.log" || true; }
hold_field() { if [ -f "$HOLD" ]; then sed -n "$1p" "$HOLD"; fi; }
check_commit() {
    case "$1" in *[!0-9a-f]*) fail "커밋이 16진수가 아니다: $1" ;; esac
    [ "${#1}" -eq 40 ] || fail "커밋은 40자다: $1"
}

if [ -n "${SSH_ORIGINAL_COMMAND:-}" ]; then
    MODE=ssh
    # shellcheck disable=SC2086 # 일부러 낱말로 나눈다
    set -- $SSH_ORIGINAL_COMMAND
    [ "$#" -eq 2 ] && [ "$1" = deploy ] || fail "사용법: deploy <커밋 40자>"
    check_commit "$2"
    COMMIT=$2
elif [ "$#" -eq 1 ] && [ "$1" = pending ]; then
    MODE=pending
else
    fail "사용법: deploy <커밋 40자> (SSH_ORIGINAL_COMMAND) | deploy.sh pending"
fi

exec 9>"$SIMAI/deploy.lock"
if [ "$MODE" = pending ]; then
    # 타이머가 몇 분마다 부른다. 다른 배포·동결이 잠금을 쥐고 있으면 기다리지 않고 다음 차례에 본다
    flock -n 9 || { echo "deploy: 다른 배포가 잠금을 쥐고 있어 보류 배포는 다음 차례에 본다"; exit 0; }
    [ -f "$HOLD" ] || exit 0
    COMMIT=$(hold_field 1)
    check_commit "$COMMIT"
    INPUT=$PENDING/$COMMIT.tar
    [ -f "$INPUT" ] || fail "보류 기록은 있는데 $INPUT 이 없다. 새 커밋을 배포하거나 $HOLD 를 지운다"
    attempts=$(hold_field 5)
    case "$attempts" in ''|*[!0-9]*) attempts=0 ;; esac
    if [ "$attempts" -ge "$PENDING_MAX_ATTEMPTS" ]; then
        echo "deploy: 보류 배포가 ${attempts}번 실패해 자동 실행을 멈췄다($COMMIT). 새 커밋을 기다리거나 손으로 배포한다"
        exit 0
    fi
else
    flock -w 1800 9 || fail "다른 배포가 30분 넘게 끝나지 않는다"
fi
TAG=$(printf %s "$COMMIT" | cut -c1-12)

# freeze.sh 와 같은 잠금 안에서 확인한다. 입력을 비워 SSH 파이프의 git archive 가 정상 종료하게 한다.
# 수동 동결이 우선이다: 동결 중이면 보류 배포도 하지 않는다(보류 파일은 그대로 둔다)
if [ -f "$SIMAI/deploy.freeze" ]; then
    expiry=$(sed -n '1p' "$SIMAI/deploy.freeze")
    reason=$(sed -n '2p' "$SIMAI/deploy.freeze")
    case "$expiry" in ''|*[!0-9]*) fail "동결 파일의 만료 시각이 올바르지 않다" ;; esac
    if [ "$expiry" -gt "$(date -u +%s)" ]; then
        echo "deploy: 동결 중 (사유: $reason, 만료: $(date -u -d "@$expiry" '+%Y-%m-%d %H:%M:%S UTC')) — 배포 건너뜀"
        [ "$MODE" = pending ] || cat > /dev/null
        exit 0
    fi
    echo "deploy: 경고: 동결이 만료되어 배포한다 (사유: $reason)" >&2
fi

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }
# .env 의 1 이상 정수 설정. $1 이름 $2 기본값 $3 단위(경고용). 비었으면 기본값, 틀렸으면 경고하고 기본값
env_int() {
    v=$(env_get "$1")
    case "$v" in
        '') v=$2 ;;
        0*|*[!0-9]*) echo "deploy: 경고: $1 이 1 이상 정수가 아니라 $2$3으로 본다" >&2; v=$2 ;;
    esac
    printf '%s' "$v"
}
# 보류 안전장치(시간). 이만큼 지나도록 보류가 풀리지 않으면 진행 중이어도 배포한다(작업 종료를 잊은 경우·Pod 를 켜 둔 채 둔 경우)
HOLD_MAX_HOURS=$(env_int SIMAI_DEPLOY_HOLD_MAX_HOURS 12 시간)
# Tier 0 가 붙어 있다고 볼 최근 health 수신 범위(분, S15P31A101-682). health 는 I1 이 붙어 있으면 작업과 무관하게 1 Hz 로 온다
HOLD_TIER0_MINUTES=$(env_int SIMAI_DEPLOY_HOLD_TIER0_MINUTES 3 분)
# 녹화 업로드 중으로 볼 최근 RECORDING_SEGMENT 업로드 주소 발급 범위(분, S15P31A101-682). 게이트웨이는 조각(5분·최대 120MB)마다 새로 받는다
HOLD_UPLOAD_MINUTES=$(env_int SIMAI_DEPLOY_HOLD_UPLOAD_MINUTES 10 분)
# Tier 0·업로드가 이어진 구간의 시작을 찾을 때 거슬러 볼 시간. 안전장치보다 길어야 같은 연결의 키가 안전장치 전에 바뀌지 않는다
HOLD_LOOKBACK_HOURS=$((HOLD_MAX_HOURS * 2))

# 진행 항목을 한 줄에 하나 낸다. 앞 두 칸이 키(안전장치가 넘긴 항목을 가리는 단위)이고 나머지는 표시용이다.
# - 작업 "work <work_id>-<run_no>": 작업 실행(S15P31A101-696, V73 work_run)을 시작했고 아직 끝내지 않았다. 하루 작업 하나에
#   실행이 여럿이라 키에 실행 번호를 넣는다(안전장치가 넘긴 실행 1 때문에 같은 작업의 실행 2 를 보류하지 않는 일이 없게).
#   work_unit 이 IN_PROGRESS 인 것만 본다(실행 표 밖에서 끝낸 작업의 열린 실행은 진행 중이 아니다). 실행 기록이 없는 작업은
#   work_run_window 뷰가 work_unit 시각을 실행 1로 준다.
# - 세팅 "setup <uuid>": SetupService 가 새 세팅을 막는 조건과 같다. 종결 상태는 COMPLETED·FAILED 뿐이고(V33 CHECK, 관리자 취소도 FAILED),
#   끝난 세션이라도 맵 보강 다시 굽기(setup_supplement_rebake)가 QUEUED·RUNNING 이면 진행 중이다.
# - Tier 0 연결 "tier0 <이어진 구간 시작 epoch> <마지막 수신 epoch>" (S15P31A101-682): 서버가 system_health 를 받은 시각
#   (received_at, EC2 시계)이 최근 HOLD_TIER0_MINUTES 분 안이다. reported_at(Tier 0 시계)은 청크를 고르는 데만 쓴다(1시간 여유).
#   미래 시각 행(가짜 기록)은 보지 않는다. 구간 시작 = 수신 간격이 HOLD_TIER0_MINUTES 분을 넘은 마지막 지점이라 Pod 를 껐다 켜면 새 키다.
# - 녹화 업로드 "upload <이어진 구간 시작 epoch> <최근 발급 건수>" (S15P31A101-682): upload_grant 의 RECORDING_SEGMENT 발급
#   (issued_at, EC2 시계)이 최근 HOLD_UPLOAD_MINUTES 분 안이다. 구간 시작은 Tier 0 와 같은 방식이다.
ACTIVE_SQL="SELECT 'work ' || r.work_id || '-' || r.run_no FROM work_run_window r JOIN work_unit w ON w.work_id = r.work_id
    WHERE r.ended_at IS NULL AND w.status = 'IN_PROGRESS'
UNION ALL
SELECT 'setup ' || setup_session_id FROM setup_session s WHERE s.status NOT IN ('COMPLETED', 'FAILED')
    OR EXISTS (SELECT 1 FROM setup_supplement_rebake r
               WHERE r.setup_session_id = s.setup_session_id AND r.status IN ('QUEUED', 'RUNNING'))
UNION ALL
SELECT 'tier0 ' || floor(extract(epoch FROM coalesce(max(t) FILTER (WHERE gap > make_interval(mins => $HOLD_TIER0_MINUTES)), min(t))))::bigint
       || ' ' || floor(extract(epoch FROM max(t)))::bigint
FROM (SELECT received_at AS t, received_at - lag(received_at) OVER (ORDER BY received_at) AS gap FROM system_health
      WHERE reported_at > now() - make_interval(hours => $HOLD_LOOKBACK_HOURS + 1)
        AND received_at > now() - make_interval(hours => $HOLD_LOOKBACK_HOURS)
        AND received_at <= now() + interval '1 minute') h
HAVING max(t) > now() - make_interval(mins => $HOLD_TIER0_MINUTES)
UNION ALL
SELECT 'upload ' || floor(extract(epoch FROM coalesce(max(t) FILTER (WHERE gap > make_interval(mins => $HOLD_UPLOAD_MINUTES)), min(t))))::bigint
       || ' ' || count(*) FILTER (WHERE t > now() - make_interval(mins => $HOLD_UPLOAD_MINUTES))
FROM (SELECT issued_at AS t, issued_at - lag(issued_at) OVER (ORDER BY issued_at) AS gap FROM upload_grant
      WHERE purpose = 'RECORDING_SEGMENT'
        AND issued_at > now() - make_interval(hours => $HOLD_LOOKBACK_HOURS)
        AND issued_at <= now() + interval '1 minute') g
HAVING max(t) > now() - make_interval(mins => $HOLD_UPLOAD_MINUTES)"

mkdir -p "$PENDING"
if [ "$MODE" = ssh ]; then
    # 보류할 수도 있으니 입력을 먼저 파일로 받는다. 망가진 입력으로 이전 보류를 바꾸지 않게 여기서 확인한다
    INPUT=$(mktemp "$PENDING/.incoming.XXXXXX")
    trap 'rm -f "$INPUT"' EXIT
    cat > "$INPUT"
    if ! { [ -s "$INPUT" ] && tar -tf "$INPUT" > /dev/null; }; then fail "입력이 tar 가 아니다($COMMIT)"; fi
fi

# 진행 항목(ACTIVE_SQL 설명)을 한 줄에 하나 낸다. 읽기 전용 트랜잭션이다. 묻지 못하면 1
active_items() {
    out=$(timeout 30 docker exec -e PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=10000' \
        "$DB_CONTAINER" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d simai -tA -c "$ACTIVE_SQL") || return 1
    out=$(printf '%s\n' "$out" | sed '/^$/d')
    if printf '%s\n' "$out" | grep -Evx '(work [0-9]+(-[0-9]+)?|setup [0-9a-f-]{36}|tier0 [0-9]+ [0-9]+|upload [0-9]+ [0-9]+)?' > /dev/null; then
        echo "deploy: DB 응답을 알아볼 수 없다: $(printf '%s\n' "$out" | head -n 1)" >&2
        return 1
    fi
    printf '%s' "$out"
}

# 항목 줄 → 사유("작업 48 실행 2", "세팅 <uuid>", "Tier 0 연결(마지막 보고 <UTC>)", "녹화 업로드 3건")를 ", " 로 잇는다
reason_text() {
    printf '%s\n' "$1" | while read -r kind key detail; do
        case "$kind" in
            work) case "$key" in
                      *-*) echo "작업 ${key%%-*} 실행 ${key#*-}" ;;
                      *) echo "작업 $key" ;;
                  esac ;;
            setup) echo "세팅 $key" ;;
            tier0) echo "Tier 0 연결(마지막 보고 $(date -u -d "@$detail" '+%Y-%m-%d %H:%M:%S UTC'))" ;;
            upload) echo "녹화 업로드 ${detail}건" ;;
        esac
    done | awk 'NR > 1 { printf ", " } { printf "%s", $0 }'
}

# 보류해야 하면 0 이고 REASON 에 사유를 둔다. DB 를 묻지 못하면 경고하고 1(배포가 영원히 막히지 않게 한다)
should_hold() {
    REASON=
    if ! all=$(active_items); then
        echo "deploy: 경고: DB 에서 진행 중 작업·세팅·Tier 0 연결·녹화 업로드를 확인하지 못해 보류하지 않고 배포한다" >&2
        return 1
    fi
    items=$all
    if [ -n "$items" ] && [ -s "$IGNORED" ]; then
        # 키(앞 두 칸)로 가린다. Tier 0·업로드는 키가 이어진 구간의 시작이라 표시 칸(마지막 수신·건수)이 바뀌어도 같은 항목이다
        items=$(printf '%s\n' "$items" | awk 'NR == FNR { ignored[$1 " " $2] = 1; next } !(($1 " " $2) in ignored)' "$IGNORED" -)
    fi
    [ -n "$items" ] || return 1
    REASON=$(reason_text "$items")
    first=$(hold_field 2)
    case "$first" in ''|*[!0-9]*) return 0 ;; esac
    if [ $(( $(date -u +%s) - first )) -ge $(( HOLD_MAX_HOURS * 3600 )) ]; then
        echo "deploy: 경고: 보류가 ${HOLD_MAX_HOURS}시간을 넘어 진행 중($REASON)이어도 배포한다. 이 항목으로는 다시 보류하지 않는다" >&2
        # 지금 진행 중인 항목 전부(전에 넘긴 것 포함)의 키를 남긴다. 전에 넘긴 것을 빼고 쓰면 그 항목이 다음 배포를 다시 보류한다.
        # 끝난 항목은 여기서 빠진다(작업·세팅 ID 와 구간 시작은 다시 나오지 않는다)
        printf '%s\n' "$all" | awk '{ print $1 " " $2 }' > "$IGNORED"
        log "안전장치 ${HOLD_MAX_HOURS}시간: 진행 중($REASON)이어도 배포 $COMMIT"
        return 1
    fi
    return 0
}

# hold 를 원자적으로 바꾼다: $1 처음 보류 $2 마지막 보류 $3 사유 $4 시도 횟수
write_hold() {
    tmp=$(mktemp "$PENDING/.hold.XXXXXX")
    printf '%s\n%s\n%s\n%s\n%s\n' "$COMMIT" "$1" "$2" "$3" "$4" > "$tmp"
    mv -f "$tmp" "$HOLD"
}

clear_hold() {
    old=$(hold_field 1)
    rm -f "$HOLD"
    if [ -n "$old" ]; then rm -f "$PENDING/$old.tar"; fi
}

# Jenkins 입력을 보류한다. 최신 커밋 하나만 남기고, 처음 보류 시각은 이어받는다
hold_input() {
    now=$(date -u +%s)
    first=$(hold_field 2)
    case "$first" in ''|*[!0-9]*) first=$now ;; esac
    old=$(hold_field 1)
    mv -f "$INPUT" "$PENDING/$COMMIT.tar"
    write_hold "$first" "$now" "$REASON" 0
    if [ -n "$old" ] && [ "$old" != "$COMMIT" ]; then
        rm -f "$PENDING/$old.tar"
        echo "deploy: 이전 보류($(printf %s "$old" | cut -c1-12))를 이번 커밋으로 대체했다"
    fi
    log "보류 $COMMIT ($REASON)"
    echo "deploy: 작업·세팅·GPU 세션 진행 중이라 배포 보류($REASON) — $TAG 를 저장했고 끝나면 자동으로 배포한다"
}

if should_hold; then
    if [ "$MODE" = ssh ]; then
        hold_input
    else
        echo "deploy: 아직 작업·세팅·GPU 세션 진행 중($REASON) — 보류 배포 $TAG 는 다음 차례에 본다"
    fi
    exit 0
fi
if [ "$MODE" = pending ]; then
    # 시작 전에 시도를 센다. 중간에 실패해 끝나도(set -e) 횟수가 남아 같은 커밋을 끝없이 다시 하지 않는다
    write_hold "$(hold_field 2)" "$(hold_field 3)" "$(hold_field 4)" $((attempts + 1))
    log "보류 배포 시도 $((attempts + 1)) $COMMIT"
    echo "deploy: 진행 중 작업·세팅·GPU 세션이 없어 보류한 배포를 실행한다 ($TAG, 시도 $((attempts + 1)))"
fi

PREV=$(env_get SIMAI_TAG)
DOMAIN=$(env_get SIMAI_DOMAIN)
REGISTRY=$(env_get SIMAI_REGISTRY)
REGISTRY=${REGISTRY:-simai}
echo "deploy: $PREV → $TAG ($COMMIT)"

rm -rf "$SIMAI/repo.new"
mkdir -p "$SIMAI/repo.new"
tar -x -C "$SIMAI/repo.new" < "$INPUT"
echo "$TAG" > "$SIMAI/repo.new/REVISION"

(cd "$SIMAI/repo.new" && TAG=$TAG docker buildx bake -f deploy/docker-bake.hcl --progress=plain ec2)

# 이미지를 만드는 몇 분 사이에 작업·세팅·Tier 0 연결이 시작됐을 수 있다. 서비스를 건드리기 직전에 한 번 더 본다(만든 이미지는 캐시로 남는다)
if should_hold; then
    rm -rf "$SIMAI/repo.new"
    if [ "$MODE" = ssh ]; then
        hold_input
    else
        write_hold "$(hold_field 2)" "$(hold_field 3)" "$(hold_field 4)" "$attempts"
        echo "deploy: 이미지를 만드는 동안 작업·세팅·GPU 세션이 시작돼($REASON) 보류 배포를 다음 차례로 미룬다"
    fi
    exit 0
fi
if [ "$MODE" = ssh ] && [ -f "$HOLD" ]; then
    echo "deploy: 이번 커밋이 이전 보류($(hold_field 1 | cut -c1-12))를 대체한다"
    log "보류 대체(바로 배포) $(hold_field 1) → $COMMIT"
    clear_hold
fi

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
    [ "$MODE" = ssh ] || log "보류 배포 실패 $COMMIT (시도 $((attempts + 1))/$PENDING_MAX_ATTEMPTS)"
    rm -rf "$SIMAI/repo.failed"
    mv "$SIMAI/repo" "$SIMAI/repo.failed"
    mv "$SIMAI/repo.old" "$SIMAI/repo"
    up "$PREV" || echo "deploy: 되돌리기도 실패했다. 손으로 확인한다(docs/design/ec2-deploy.md)" >&2
    exit 1
fi
if [ "$MODE" = pending ]; then
    clear_hold
    log "보류 배포 완료 $COMMIT"
fi

# 지금·이전 판을 빼고 최근 KEEP 판보다 오래된 태그를 지운다(image ls 는 최근 것부터 나온다)
for name in server web risk-engine; do
    docker image ls "$REGISTRY/$name" --format '{{.Tag}}' | tail -n +$((KEEP + 1)) | while read -r old; do
        [ "$old" = "$TAG" ] || [ "$old" = "$PREV" ] || docker image rm "$REGISTRY/$name:$old" > /dev/null || true
    done
done
docker image prune -f > /dev/null
