#!/usr/bin/env bash
# 사용: load-legal-graph.sh <시드.sql> <시드 sha256이 담긴 manifest 파일>
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo "사용: $0 <시드.sql> <sha256 manifest>" >&2
    exit 2
fi
seed=$1
manifest=$2
[ -f "$seed" ] && [ -f "$manifest" ] || { echo '시드 또는 manifest 파일이 없습니다' >&2; exit 1; }
# sha256sum 형식의 manifest 에서 시드 파일 이름과 일치하는 항목만 쓴다.
expected=$(awk -v name="$(basename "$seed")" '$2 == name || $2 == "*" name { print $1 }' "$manifest")
[[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || { echo 'manifest 에 시드 해시가 하나 있어야 합니다' >&2; exit 1; }
actual=$(sha256sum "$seed" | cut -d' ' -f1)
[[ "${actual,,}" == "${expected,,}" ]] || { echo '시드 sha256 불일치' >&2; exit 1; }
# 접속 정보는 psql 표준 환경변수 또는 .pgpass 로 운영자가 제공한다.
psql -X -v ON_ERROR_STOP=1 --single-transaction -f "$seed"
