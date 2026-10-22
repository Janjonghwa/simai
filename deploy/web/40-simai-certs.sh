#!/bin/sh
# Nginx 이미지 진입점이 nginx 를 띄우기 전에 실행한다(/docker-entrypoint.d).
# 1) 인증서가 없으면 이유를 남기고 멈춘다. 처음 발급 절차는 docs/design/ec2-deploy.md
# 2) certbot 이 갱신한 인증서를 읽도록 6시간마다 설정을 다시 읽는다(갱신은 만료 30일 전부터라 충분하다)
set -eu

cert="/etc/letsencrypt/live/${SIMAI_DOMAIN}/fullchain.pem"
if [ ! -f "$cert" ]; then
    echo "인증서가 없다: $cert (docs/design/ec2-deploy.md 의 '인증서 처음 발급'을 먼저 한다)" >&2
    exit 1
fi

(
    while :; do
        sleep 6h
        nginx -s reload
    done
) &
