#!/bin/sh
# 보류한 배포를 실행한다(S15P31A101-676). 호스트 타이머(systemd/simai-deploy-pending.timer)가 3분마다 부른다.
# 판단과 배포는 모두 deploy.sh 가 한다(같은 잠금·수동 동결 우선·진행 중 작업·세팅·Tier 0 연결·녹화 업로드 확인·12시간 안전장치·배포·되돌리기).
# 이 파일은 입구만 맡는다. 설치: install-host.sh (deploy.sh 와 같은 디렉터리에 둔다)
set -eu
# Jenkins 입구로 잘못 들어가지 않게 지운다
unset SSH_ORIGINAL_COMMAND
exec "$(dirname "$0")/deploy.sh" pending
