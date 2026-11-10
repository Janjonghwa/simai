#!/bin/sh
# EC2 호스트에서 실행. 배포와 같은 잠금을 잡아 진행 중 배포가 끝난 뒤 동결한다.
set -eu
SIMAI=/opt/simai
fail() { echo "freeze: $*" >&2; exit 2; }
[ "$#" -ge 1 ] || fail '사용법: freeze.sh on --hours N --reason "..." | off | status'
action=$1
shift
hours= reason=
case "$action" in
    on)
        while [ "$#" -gt 0 ]; do
            [ "$#" -ge 2 ] || fail '옵션 값이 없다'
            case "$1" in
                --hours) hours=$2 ;;
                --reason) reason=$2 ;;
                *) fail "알 수 없는 옵션: $1" ;;
            esac
            shift 2
        done
        case "$hours" in ''|0*|*[!0-9]*) fail '시간은 1~168 정수여야 한다' ;; esac
        [ "${#hours}" -le 3 ] && [ "$hours" -ge 1 ] && [ "$hours" -le 168 ] || fail '시간은 1~168 정수여야 한다'
        [ -n "$reason" ] || fail '사유를 입력한다'
        case "$reason" in *'
'*) fail '사유는 한 줄이어야 한다' ;; esac
        ;;
    off|status) [ "$#" -eq 0 ] || fail '불필요한 인자' ;;
    *) fail '사용법: freeze.sh on --hours N --reason "..." | off | status' ;;
esac
exec 9>"$SIMAI/deploy.lock"
flock -w 1800 9 || fail '다른 배포가 30분 넘게 끝나지 않는다'
file=$SIMAI/deploy.freeze
case "$action" in
    on)
        expires=$(( $(date -u +%s) + hours * 3600 ))
        # 같은 디렉터리에서 원자적으로 교체한다.
        tmp=$(mktemp "$SIMAI/.deploy.freeze.XXXXXX")
        trap 'rm -f "$tmp"' EXIT HUP INT TERM
        printf '%s\n%s\n%s\n' "$expires" "$reason" "${SUDO_USER:-$(id -un)}" > "$tmp"
        mv -f "$tmp" "$file"
        echo "freeze: 동결 설정 (사유: $reason, 만료: $(date -u -d "@$expires" '+%Y-%m-%d %H:%M:%S UTC'), 설정자: ${SUDO_USER:-$(id -un)})"
        ;;
    off) rm -f "$file"; echo 'freeze: 동결 해제' ;;
    status)
        if [ ! -f "$file" ]; then echo 'freeze: 동결 없음'; exit 0; fi
        expires=$(sed -n '1p' "$file")
        case "$expires" in ''|*[!0-9]*) fail '동결 파일의 만료 시각이 올바르지 않다' ;; esac
        state='동결 중'; [ "$expires" -gt "$(date -u +%s)" ] || state='동결 만료'
        echo "freeze: $state (사유: $(sed -n '2p' "$file"), 만료: $(date -u -d "@$expires" '+%Y-%m-%d %H:%M:%S UTC'), 설정자: $(sed -n '3p' "$file"))"
        ;;
esac
