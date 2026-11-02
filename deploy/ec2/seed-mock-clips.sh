#!/usr/bin/env bash
# 이력 화면 확인용 목 클립 적재·정리. 운영 EC2 에서만 돌린다(/opt/simai/.env 의 저장소 키·DB 를 쓴다).
# Tier 0 클립 업로드(ai/services/tier0/clips)가 EC2 에 아직 붙지 않아 media_clip 이 0건이다. 그동안 프론트가
# /api/events/{id}/clips · /api/clips/{id} 를 실제 S3 재생 주소로 확인할 수 있게, 최근 LIVE CRITICAL·HIGH 이벤트 몇 개에
# 같은 mp4 를 서버 키 규칙(event-clips/<event_id>/<clip_id>.mp4)대로 올리고 media_clip 행을 넣는다.
# 목 행은 redactor_version = 'MOCK-SEED' 로 표시하고, cleanup 이 그 표시로 객체·행을 모두 지운다.
# 업로드 서명(upload_grant)은 거치지 않는다. 조회 경로는 media_clip 행과 객체만 본다(ClipService).
#
# 사용:
#   deploy/ec2/seed-mock-clips.sh seed <mp4 경로> [이벤트 수=10] [이벤트당 카메라 수=2]
#   deploy/ec2/seed-mock-clips.sh cleanup
# 필요: docker(aws cli 는 amazon/aws-cli 이미지로 돌린다), sha256sum, /opt/simai/.env
set -euo pipefail

SIMAI=${SIMAI:-/opt/simai}
ENV_FILE=$SIMAI/.env
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$SIMAI/repo/deploy/ec2/compose.yaml")
MARK='MOCK-SEED'
AWS_IMAGE=amazon/aws-cli:2.22.35

[ -f "$ENV_FILE" ] || { echo "env 파일이 없다: $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
: "${SIMAI_STORAGE_BUCKET:?SIMAI_STORAGE_BUCKET 이 비었다}"
: "${SIMAI_STORAGE_ACCESS_KEY:?}" "${SIMAI_STORAGE_SECRET_KEY:?}"
REGION=${SIMAI_STORAGE_REGION:-ap-northeast-2}

psql() { "${COMPOSE[@]}" exec -T db psql -U postgres -d simai -v ON_ERROR_STOP=1 -At "$@"; }
aws() {
    docker run --rm -e AWS_ACCESS_KEY_ID="$SIMAI_STORAGE_ACCESS_KEY" -e AWS_SECRET_ACCESS_KEY="$SIMAI_STORAGE_SECRET_KEY" \
        -e AWS_DEFAULT_REGION="$REGION" ${AWS_MOUNT:+-v "$AWS_MOUNT"} "$AWS_IMAGE" "$@"
}

seed() {
    local mp4=${1:?mp4 경로} events=${2:-10} cams=${3:-2}
    [ -f "$mp4" ] || { echo "파일이 없다: $mp4" >&2; exit 1; }
    local abs; abs=$(readlink -f "$mp4")
    local size sha; size=$(stat -c %s "$abs"); sha=$(sha256sum "$abs" | cut -d' ' -f1)

    # 최근 LIVE CRITICAL·HIGH 이벤트 중 클립이 없는 것. 카메라는 camera 표에서 돌아가며 고른다.
    mapfile -t rows < <(psql -c "
        WITH e AS (
            SELECT e.event_id, e.severity
            FROM risk_event e
            WHERE e.source = 'LIVE' AND e.severity IN ('CRITICAL', 'HIGH') AND e.gateway_id IS NOT NULL
              AND e.raised_at IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM media_clip c WHERE c.event_id = e.event_id)
            ORDER BY e.raised_at DESC LIMIT $events),
        cam AS (SELECT camera_id, row_number() OVER (ORDER BY camera_id) - 1 AS n FROM camera WHERE removed_at IS NULL)
        SELECT e.event_id, cam.camera_id, gen_random_uuid()
        FROM e JOIN cam ON cam.n < $cams
        ORDER BY e.event_id, cam.camera_id")
    [ ${#rows[@]} -gt 0 ] || { echo "클립을 붙일 LIVE CRITICAL·HIGH 이벤트가 없다" >&2; exit 1; }
    echo "이벤트 $events 개 × 카메라 $cams 대 = 클립 ${#rows[@]} 개, 파일 $size 바이트"

    local sql=""
    for row in "${rows[@]}"; do
        IFS='|' read -r event camera clip <<<"$row"
        local key="event-clips/$event/$clip.mp4"
        AWS_MOUNT="$abs:/in.mp4:ro" aws s3 cp /in.mp4 "s3://$SIMAI_STORAGE_BUCKET/$key" --content-type video/mp4 --no-progress
        sql+="
        INSERT INTO media_clip (clip_id, event_id, gateway_id, event_sequence, resume_count, rule_id, severity, source, camera_id,
            stream_epochs, anchor_capture_at, requested_start_at, requested_end_at, clip_start_capture_at, clip_end_capture_at,
            frame_count, starts_with_keyframe, truncated_start, truncated_end, has_gaps, content_type, container, size_bytes, sha256,
            face_redaction, redactor_version, clip_created_at, delete_after, retention_hold, object_key, uploaded_at, spooled,
            upload_attempts, received_at)
        SELECT '$clip', e.event_id, e.gateway_id, COALESCE(e.event_sequence, 1), e.resume_count, e.rule_id, e.severity, 'LIVE', '$camera',
            ARRAY[0], e.raised_at, e.raised_at - interval '20 seconds', e.raised_at + interval '20 seconds',
            e.raised_at - interval '20 seconds', e.raised_at + interval '20 seconds',
            1000, true, false, false, false, 'video/mp4', 'mp4', $size, '$sha',
            'NONE', '$MARK', e.raised_at + interval '21 seconds', now() + interval '14 days', e.severity = 'CRITICAL', '$key', now(), false,
            1, now()
        FROM risk_event e WHERE e.event_id = '$event';
        INSERT INTO media_clip_retention (clip_id, event_id, gateway_id, resume_count, severity, retention_hold, reason, source, issued_at)
        SELECT '$clip', e.event_id, e.gateway_id, e.resume_count, e.severity, true, 'CRITICAL_AUTO', 'LIVE', now()
        FROM risk_event e WHERE e.event_id = '$event' AND e.severity = 'CRITICAL';"
    done
    psql -c "BEGIN; $sql COMMIT;" >/dev/null
    echo "끝. 확인: GET /api/events/<event_id>/clips → GET /api/clips/<clip_id>"
    psql -c "SELECT event_id, count(*) AS clips, min(severity) FROM media_clip WHERE redactor_version = '$MARK' GROUP BY event_id"
}

cleanup() {
    mapfile -t keys < <(psql -c "SELECT object_key FROM media_clip WHERE redactor_version = '$MARK'")
    echo "목 클립 ${#keys[@]} 개를 지운다"
    for key in "${keys[@]}"; do aws s3 rm "s3://$SIMAI_STORAGE_BUCKET/$key" || echo "객체 삭제 실패(계속): $key" >&2; done
    psql -c "BEGIN;
        DELETE FROM clip_access_log WHERE clip_id IN (SELECT clip_id FROM media_clip WHERE redactor_version = '$MARK');
        DELETE FROM media_clip_retention WHERE clip_id IN (SELECT clip_id FROM media_clip WHERE redactor_version = '$MARK');
        DELETE FROM media_clip WHERE redactor_version = '$MARK';
        COMMIT;"
    echo "끝"
}

case ${1:-} in
    seed) shift; seed "$@" ;;
    cleanup) cleanup ;;
    *) sed -n 2,12p "$0" >&2; exit 2 ;;
esac
