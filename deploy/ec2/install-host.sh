#!/bin/sh
# EC2 호스트에 배포 스크립트와 보류 배포 타이머를 설치한다(S15P31A101-676). 저장소의 deploy.sh·freeze.sh·deploy-pending.sh·
# systemd/ 를 고치면 다시 실행한다. 절차: deploy/README.md "배포 동결"
#   sudo sh /opt/simai/repo/deploy/ec2/install-host.sh [배포 사용자, 기본 ubuntu]
# 배포 사용자는 Jenkins 배포 키(authorized_keys 의 command=)가 들어오는 사용자다. 타이머도 그 사용자로 돈다
set -eu
fail() { echo "install-host: $*" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || fail 'sudo 로 실행한다'
user=${1:-ubuntu}
id "$user" > /dev/null 2>&1 || fail "사용자가 없다: $user"
src=$(cd "$(dirname "$0")" && pwd)
SIMAI=/opt/simai
for f in deploy.sh freeze.sh deploy-pending.sh; do
    install -D -m 755 "$src/$f" "$SIMAI/bin/$f"
    cmp "$src/$f" "$SIMAI/bin/$f"
done
# 보류 tar·기록. 배포 사용자가 쓰고 freeze.sh status(sudo)가 읽는다
install -d -m 750 -o "$user" -g "$(id -gn "$user")" "$SIMAI/pending"
# 잠금 파일을 root 가 먼저 만들면 배포 사용자가 열지 못한다
[ -e "$SIMAI/deploy.lock" ] || install -m 644 -o "$user" -g "$(id -gn "$user")" /dev/null "$SIMAI/deploy.lock"
sed "s/^User=.*/User=$user/" "$src/systemd/simai-deploy-pending.service" > /etc/systemd/system/simai-deploy-pending.service
install -m 644 "$src/systemd/simai-deploy-pending.timer" /etc/systemd/system/simai-deploy-pending.timer
systemctl daemon-reload
systemctl enable --now simai-deploy-pending.timer
systemctl list-timers simai-deploy-pending.timer --no-pager
"$SIMAI/bin/freeze.sh" status
echo "install-host: 설치 완료 (사용자 $user). 기록: journalctl -u simai-deploy-pending, $SIMAI/pending/history.log"
