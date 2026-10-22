# EC2 배포: Docker Compose·Nginx·HTTPS (S15P31A101-66)

EC2 한 대에 관제 서버 스택을 올리고 `https://simai.live` 로 연다(명세 3.8, 11.1, 14). 이미지 레지스트리 없이 EC2 에서 빌드해 그 자리에서
띄운다(SSAFY GitLab 은 Container Registry 가 꺼져 있다). 자동 배포(CI 가 빌드·배포)는 별도 티켓이다.

- 도메인: `simai.live` 는 호스팅.kr 에 등록돼 있고(2026-10-15, 등록기관 이전은 60일 뒤부터 가능) DNS 는 Cloudflare 가 맡는다
  (네임서버 `arvind`·`may.ns.cloudflare.com`, Free 플랜). A 레코드 `@`·`www` → EC2 공인 IP, **프록시 끔(DNS only)**.
- HTTPS 는 EC2 의 Nginx 가 Let's Encrypt 인증서로 직접 종료한다.

## 구성

| 서비스 | 이미지 | 하는 일 | 망 |
|---|---|---|---|
| `web` | `deploy/web/Dockerfile` (Nginx 1.30.6 + 관제 앱 정적 파일) | 80·443 공개, HTTPS 종료, `/api/`·`/ws` → `server:8080`, 그 밖은 관제 앱(SPA) | edge |
| `certbot` | `certbot/certbot:v5.8.0` | 12시간마다 갱신 확인(webroot). 처음 발급은 아래 절차 | 없음(볼륨만) |
| `server` | `deploy/server/Dockerfile` (Spring Boot, JRE 21, uid 10002) | REST·STOMP. 기동 때 Flyway 가 스키마를 만든다 | edge, ec2-internal |
| `db` | `timescale/timescaledb-ha:pg17-all` (명세 11.1) | PostgreSQL + TimescaleDB + PostGIS. 볼륨 `db-data` | ec2-internal |
| `risk-engine` | `deploy/risk-engine/Dockerfile` (`deploy/compose.yaml` 을 `extends`) | TBM PLANNED 판정(명세 12.2). 포트 공개 없음 | ec2-internal |

- `ec2-internal` 은 internal 망이라 db·risk-engine 은 밖으로 나가지 못한다. server 만 두 망에 붙는다.
- 공개 포트는 web 의 80·443 뿐이다. EC2 ufw 는 22·80·443 만 연다.
- 비밀 값(`POSTGRES_PASSWORD`, `SIMAI_JWT_SECRET`)은 EC2 의 `/opt/simai/.env`(권한 600)에만 있다. 키 이름은 `deploy/ec2/.env.example`.
- 외부 이미지는 `deploy/docker-bake.hcl`·compose 에서 다이제스트로 고정한다.

## 정한 것과 근거

- **한 출처**: 관제 앱·REST·STOMP 를 `simai.live` 하나로 묶는다. 갱신 토큰 쿠키가 `SameSite=Strict; Path=/api/auth` 라 다른 출처면
  돌지 않고, CORS 도 필요 없다(`SIMAI_CORS_ALLOWED_ORIGINS` 비움).
- **관제 앱 주소는 빌드 때 박는다**: Vite 가 `VITE_API_BASE_URL=https://simai.live`, `VITE_STOMP_URL=wss://simai.live/ws`,
  `VITE_API_MOCK=false` 로 빌드한다(bake 변수 `SIMAI_DOMAIN`). 운영 빌드는 https·wss 가 아니면 실패한다(`parseEnv`).
- **server 헬스체크는 포트만 본다**: `/actuator/health` 는 지금 SecurityConfig 상 인증이 필요하다. server 코드는 이 티켓에서 바꾸지
  않았다. 자동 배포 티켓에서 health 를 열고 그 응답으로 배포 성공을 가른다.
- **Nginx 의 upstream 은 변수로**: `resolver 127.0.0.11` + `set $server_upstream`. server 가 없거나 다시 만들어져도 Nginx 가 뜨고
  새 주소를 찾는다.
- **WebSocket**: `/ws` 에 Upgrade 헤더와 1시간 읽기 제한. server 의 STOMP 엔드포인트는 아직 없다(실시간 티켓에서 `/ws` 로 맞춘다).
- **HSTS** `max-age=31536000`(서브도메인 미포함). location 에 `add_header` 가 있으면 server 의 것을 물려받지 않아 다시 적었다.
- **인증서**: Let's Encrypt. 처음은 certbot standalone(80 포트, web 을 띄우기 전), 갱신은 webroot(`/.well-known/acme-challenge/`
  를 web 이 `certbot-www` 볼륨에서 내준다). web 은 6시간마다 `nginx -s reload` 로 새 인증서를 읽는다. 인증서가 없으면 web 이
  시작하지 않고 이유를 남긴다(`40-simai-certs.sh`).
- **DB 계정**: Flyway 가 확장(timescaledb, postgis)을 만들어야 해서 server 가 `postgres` 관리자 계정으로 붙는다(잔여 위험).
- **메모리 상한**: server 2 GB(힙 75%), db 4 GB. EC2 는 15 GB 이고 Jenkins·CI 잡이 같은 서버에 올라온다(S15P31A101-28).

## 처음 한 번 (EC2: Ubuntu 24.04, `ubuntu` 계정)

1. DNS: Cloudflare 에 `simai.live`·`www.simai.live` A 레코드 → EC2 공인 IP, 프록시 끔(DNS only), TTL Auto.
2. Docker Engine·compose 플러그인(공식 apt 저장소), `ubuntu` 를 docker 그룹에, `sudo ufw allow 443/tcp`, `/opt/simai` 생성.
3. `/opt/simai/.env`: `deploy/ec2/.env.example` 의 키를 채운다. 비밀 값은 서버에서 만든다(`openssl rand -hex 24`,
   `openssl rand -base64 48`). `chmod 600`.

## 배포

```sh
# 개발 PC (저장소 최상위). 서버에 GitLab 자격 증명을 두지 않으려고 커밋된 내용만 옮긴다. -c core.autocrlf=false: 셸 스크립트를 LF 로
git -c core.autocrlf=false archive HEAD | ssh -i ~/.ssh/K15A101T.pem ubuntu@k15a101.p.ssafy.io \
  "rm -rf /opt/simai/repo && mkdir -p /opt/simai/repo && tar -x -C /opt/simai/repo"
ssh ... "echo $(git rev-parse --short=12 HEAD) > /opt/simai/repo/REVISION"

# EC2
cd /opt/simai/repo
TAG=$(cat REVISION) docker buildx bake -f deploy/docker-bake.hcl ec2       # server·web·risk-engine
sed -i "s/^SIMAI_TAG=.*/SIMAI_TAG=$(cat REVISION)/" /opt/simai/.env
docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml up -d
docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml ps
```

### 인증서 처음 발급 (web 을 띄우기 전, 80 포트가 비어 있을 때)

```sh
cd /opt/simai/repo && set -a && . /opt/simai/.env && set +a
C="docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml"
ARGS="certonly --standalone -d $SIMAI_DOMAIN -d www.$SIMAI_DOMAIN --email $CERTBOT_EMAIL --agree-tos --no-eff-email --non-interactive"
$C run --rm -p 80:80 --entrypoint certbot certbot $ARGS --dry-run   # 먼저 시험 서버로. 성공했을 때만 아래를 돌린다
$C run --rm -p 80:80 --entrypoint certbot certbot $ARGS
```

- 실제 발급 실패는 계정·도메인당 시간당 5회로 막힌다. 그래서 `--dry-run`(시험 서버)이 성공했을 때만 실제 발급을 돌린다.
- DNS 를 바꾼 직후에는 Let's Encrypt 가 옛 응답을 들고 있을 수 있다. 2026-10-22 호스팅.kr DNS 에서는 권한 네임서버 ns1 만 루트 도메인을
  옛 값(파킹 주소)으로 15분 넘게 줬고, 그 뒤에도 실제 발급 서버가 옛 주소로 확인해 2회 실패했다(시험 서버는 성공). DNS 를 Cloudflare 로
  옮긴 뒤 바로 발급됐다.
- 갱신은 `certbot` 서비스가 webroot 로 한다. 손으로 점검할 때는 갱신 루프와 잠금이 겹치지 않게 새 컨테이너로 돌린다:
  `$C run --rm --no-deps --entrypoint certbot certbot renew --dry-run --webroot -w /var/www/certbot --non-interactive`

### 관리자 계정 만들기

```sh
docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml run --rm server admin create-user --login <아이디> --role ADMIN
```

비밀번호는 터미널에서 두 번 묻는다(`docs/design/server-auth.md`).

## 되돌리기

- 이전 판으로: `/opt/simai/.env` 의 `SIMAI_TAG` 를 이전 태그로 바꾸고 `up -d`. 이미지는 태그별로 EC2 에 남는다
  (`docker image ls simai/*`). 오래된 태그는 손으로 지운다(자동 정리는 자동 배포 티켓).
- DB 스키마는 Flyway 가 앞으로만 간다. 마이그레이션이 든 판을 되돌릴 때는 먼저 `pg_dump` 로 백업한다.
- 전부 내리기: `docker compose ... down`(볼륨은 남는다). `down -v` 는 DB·인증서까지 지우니 쓰지 않는다.

## 버린 대안

- **Cloudflare 프록시 + Origin 인증서**: 서버 IP 숨김·DDoS 방어·캐시가 붙지만, 국내(SSAFY 망)에서 Free 플랜이 미국 거점(LAX)을 거쳤다.
  2026-10-22 측정, 같은 API 요청 5회: 직접 연결 0.01초·첫 응답 약 0.05초, Cloudflare 경유 연결 약 0.15초·첫 응답 0.6~1.0초.
  관제 화면은 WebSocket 으로 10 Hz 상태와 경보를 받으므로 버렸다. Free 플랜은 거점을 고를 수 없다. Origin 인증서(2041년 만료)와 키는
  EC2 `/opt/simai/certs` 에 남겨 뒀다(다시 켤 때 쓴다).
- **호스팅.kr DNS**: 위 Let's Encrypt 발급 실패 때문에 Cloudflare DNS 로 옮겼다. 등록은 호스팅.kr 에 그대로다.
- **이미지 레지스트리**: SSAFY GitLab Container Registry 가 꺼져 있고, Docker Hub 무료는 비공개 저장소가 하나뿐이다.

## 잔여 위험

- server 가 DB 관리자 계정으로 붙는다. 확장을 미리 만들고 앱 전용 계정으로 바꾸는 일은 남았다.
- 배포가 수동이다(소스 복사 → 빌드 → up). 자동 배포는 별도 티켓.
- 백업이 없다. `db-data` 볼륨은 EC2 디스크에만 있다.
- server 헬스체크가 포트만 본다. 기동은 됐지만 요청을 못 받는 상태는 잡지 못한다.
- 서버 IP 가 DNS 에 그대로 드러난다(프록시를 끔). 공개 포트는 80·443·22 뿐이다.

## 넘긴 일

- 자동 배포: master 머지 → Jenkins 가 EC2 에서 bake·up → health 확인 → 실패 시 이전 태그. DB 백업, 오래된 이미지 정리.
- Jenkins 를 Nginx 뒤(`/jenkins`)에 둔다(S15P31A101-28).
- STOMP 엔드포인트 `/ws`(server), `/actuator/health` 공개(server).

## 검증

2026-10-22, EC2(Ubuntu 24.04, 4 vCPU, 15 GB), 커밋 `6b0912877b7a` 이미지.

- `docker buildx bake ec2`: server 522 MB, web 103 MB, risk-engine 226 MB.
- `up -d`: db·server·risk-engine healthy, web·certbot 기동. server 기동 12.6 s, Flyway 가 V1·V2·R 적용. 메모리 server 317 MB,
  db 183 MB, risk-engine 40 MB.
- `https://simai.live/` 200(nginx, HSTS), `/zones`(SPA 경로) 200, `https://www.simai.live/` → 301 `https://simai.live/`,
  `http://simai.live/a` → 301 `https://simai.live/a`.
- `POST /api/auth/login`(없는 계정) 401 `LOGIN_FAILED`: Nginx → server → DB 경로가 돈다.
- 인증서: Let's Encrypt, `simai.live`·`www.simai.live`, 만료 2027-01-20. 갱신 시험(webroot `--dry-run`) 성공.
- 국내에서 첫 응답 0.06~0.09 s.

