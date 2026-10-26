# EC2 배포: Docker Compose·Nginx·HTTPS (S15P31A101-66)

EC2 한 대에 관제 서버 스택을 올리고 `https://simai.live` 로 연다(명세 3.8, 11.1, 14). 이미지 레지스트리 없이 EC2 에서 빌드해 그 자리에서
띄운다(SSAFY GitLab 은 Container Registry 가 꺼져 있다). dev 에 머지되면 Jenkins 가 검사 뒤 자동으로 배포한다(아래 "자동 배포",
S15P31A101-254).

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
- Tier 0 토큰은 원문을 EC2 에 두지 않고 해시(`SIMAI_TIER0_TOKEN_SHA256`)만 `.env` 에 둔다. 비우면 I1(`wss://<도메인>/ws/tier0`) 연결만 거부되고 서버는 뜬다. 원문은 RunPod Tier 0 의 `SIMAI_TIER0_TOKEN` 에 둔다.
- 외부 이미지는 `deploy/docker-bake.hcl`·compose 에서 다이제스트로 고정한다.

## 정한 것과 근거

- **한 출처**: 관제 앱·REST·STOMP 를 `simai.live` 하나로 묶는다. 갱신 토큰 쿠키가 `SameSite=Strict; Path=/api/auth` 라 다른 출처면
  돌지 않고, CORS 도 필요 없다(`SIMAI_CORS_ALLOWED_ORIGINS` 비움).
- **관제 앱 주소는 빌드 때 박는다**: Vite 가 `VITE_API_BASE_URL=https://simai.live`, `VITE_STOMP_URL=wss://simai.live/ws`,
  `VITE_API_MOCK=false` 로 빌드한다(bake 변수 `SIMAI_DOMAIN`). 운영 빌드는 https·wss 가 아니면 실패한다(`parseEnv`).
- **server 헬스체크는 포트만 본다**: `/actuator/health` 는 지금 SecurityConfig 상 인증이 필요하다. server 코드는 이 티켓에서 바꾸지
  않았다. 자동 배포는 health 대신 컨테이너 healthcheck(`up --wait`)와 `https://simai.live/` 응답으로 성공을 가른다. health 공개는
  server 쪽 일로 남겼다.
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

## 자동 배포 (S15P31A101-254)

dev 에 push(머지)되면 Jenkins 작업 `simai` 가 검사(`docs/design/ci-pipeline.md`)를 모두 돌리고, 다 통과하면 마지막 `배포` 단계에서
그 커밋을 EC2 에 올린다. MR 빌드는 배포하지 않는다. 검사 하나라도 실패하면 배포하지 않는다.

```
Jenkins(컨테이너)  git archive HEAD | ssh ubuntu@host.docker.internal "deploy <커밋>"
호스트 sshd        authorized_keys 의 Jenkins 키 → /opt/simai/bin/deploy.sh 만 실행(command=)
deploy.sh          tar 풀기(repo.new) → bake ec2 → repo 바꾸기 → SIMAI_TAG → up -d --wait → https 확인 → 오래된 이미지 정리
                   실패: repo·SIMAI_TAG 를 이전 판으로 돌리고 up, Jenkins 빌드 실패(GitLab ✘)
```

- **Jenkins 에 Docker 소켓을 주지 않는다**(S15P31A101-28 과 같은 이유: 소켓은 호스트 root 와 같다). 대신 호스트에 할 일을 하나로 묶은
  SSH 키를 준다. 이 키로는 `deploy <커밋 40자>` 만 되고 셸·명령·포트 포워딩은 막힌다(`restrict`). 접속은 Jenkins 가 붙은 edge 망
  (`172.20.0.0/16`)에서만 받는다(`from=`). Jenkins 가 뚫리면 "아무 소스나 배포하기"까지는 된다. 이 위험은 남긴다.
- **소스는 Jenkins 가 tar 로 보낸다**: 호스트에 GitLab 자격 증명을 두지 않는다(수동 배포와 같다).
- **jenkins·ci-db 는 다시 띄우지 않는다**: `up` 에 서비스 이름(web certbot server db risk-engine)을 준다. 배포가 자기를 돌리는
  Jenkins 를 재시작하면 빌드가 끊긴다. compose 의 jenkins 설정을 바꿨으면 손으로 `up -d jenkins` 한다(돌던 빌드가 없을 때).
- **성공 기준**: compose healthcheck 가 healthy(server 는 포트, db 는 `pg_isready`, 5분 안) + `https://simai.live/` 가 2xx.
- **이미지 정리**: server·web·risk-engine 은 최근 3판(지금·이전 판은 늘 남김)만 남기고 지운다. 이름 없는 이미지도 지운다.
- **DB 덤프**: 이번 판의 `server/src/main/resources/db/migration` 이 지금 판과 다르면 띄우기 전에 `pg_dump -Fc` 로
  `/opt/simai/backups/simai-<시각>-<이전 태그>.dump`(디렉터리 700)에 뜬다. 최근 5개만 남긴다. 덤프가 실패하면 배포하지 않는다.
  Flyway 는 앞으로만 가서, 판을 되돌려도 스키마는 돌아오지 않기 때문이다. 마이그레이션이 없는 배포는 덤프하지 않는다.
- **한 번에 하나**: Jenkins 작업이 동시에 돌지 않고(`disableConcurrentBuilds`), deploy.sh 도 `flock` 으로 막는다.
- dev 에 push 하면 바뀐 경로와 관계없이 검사를 모두 돌린다(ai 시험 약 30분). 머지부터 배포까지 약 35~40분 걸린다.

### 준비 (한 번)

```sh
# 1. 배포 스크립트 설치. 저장소의 deploy/ec2/deploy.sh 를 고치면 다시 한다(Jenkins 가 바꿀 수 없게 저장소 밖에 둔다)
install -D -m 755 /opt/simai/repo/deploy/ec2/deploy.sh /opt/simai/bin/deploy.sh

# 2. Jenkins 에 host.docker.internal 을 준다(compose 의 extra_hosts). 돌던 빌드가 없을 때
cd /opt/simai/repo && docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml up -d jenkins

# 3. 키는 Jenkins 컨테이너 안에서 만들어 밖으로 꺼내지 않는다(jenkins_home 볼륨)
J=simai-ec2-jenkins-1
docker exec -u jenkins $J sh -c 'mkdir -p -m 700 ~/.ssh && ssh-keygen -q -t ed25519 -N "" -C simai-jenkins-deploy -f ~/.ssh/simai_deploy'
echo "restrict,command=\"/opt/simai/bin/deploy.sh\",from=\"172.20.0.0/16\" $(docker exec -u jenkins $J cat /var/jenkins_home/.ssh/simai_deploy.pub)" \
  >> ~/.ssh/authorized_keys

# 4. 호스트 키는 호스트 파일에서 바로 넣는다(ssh-keyscan 으로 받지 않는다)
docker exec -u jenkins $J sh -c "echo 'host.docker.internal $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)' >> ~/.ssh/known_hosts"
```

- edge 망을 지우고 다시 만들면(`compose down`) 대역이 바뀔 수 있다. `docker network inspect simai-ec2_edge` 로 대역을 보고 `from=` 을
  맞춘다.
- 손으로 같은 배포를 돌릴 때(개발 PC, 저장소 최상위):
  `git -c core.autocrlf=false archive HEAD | ssh -i ~/.ssh/K15A101T.pem ubuntu@k15a101.p.ssafy.io "SSH_ORIGINAL_COMMAND='deploy $(git rev-parse HEAD)' /opt/simai/bin/deploy.sh"`
- 키를 끊을 때: `~/.ssh/authorized_keys` 에서 `simai-jenkins-deploy` 줄을 지운다.

## 되돌리기

- 자동 배포가 실패하면 deploy.sh 가 이전 판으로 돌린다. 실패한 소스는 `/opt/simai/repo.failed` 에 남는다.
  마이그레이션이 이미 적용된 뒤라 이전 판이 뜨지 않으면 배포 전 덤프로 되살린다(아래).
- 이전 판으로: `/opt/simai/.env` 의 `SIMAI_TAG` 를 이전 태그로 바꾸고 `up -d`. 이미지는 최근 3판이 EC2 에 남는다
  (`docker image ls simai/*`). 이전 판의 소스는 `/opt/simai/repo.old`. 더 오래된 판은 그 커밋으로 손 배포한다.
- DB 스키마는 Flyway 가 앞으로만 간다. 덤프에서 되살리기(그 시점 이후 데이터는 잃는다):

```sh
C="docker compose --env-file /opt/simai/.env -f /opt/simai/repo/deploy/ec2/compose.yaml"
$C stop server
$C exec -T db pg_restore -U postgres -d postgres --clean --create < /opt/simai/backups/<덤프 파일>
# 그 덤프의 이전 태그(파일 이름 끝)로 SIMAI_TAG 를 맞추고 up -d
```

### DB 초기화 (한 번, 첫 운영 배포 전 예외)

이미 적용된 마이그레이션(`V1`·`V2` 등)을 고치면 운영 DB 의 Flyway 체크섬이 맞지 않아 server 가 뜨지 않는다. 명세의 첫 운영 배포 전
규칙 보완 예외(11.4-1)로 그런 변경이 dev 에 들어오면, 그 판을 배포할 때 한 번 DB 를 비우고 V1 부터 다시 만든다. 그 판이 머지되기
전에 비우면 옛 V1 로 다시 만들어져 또 깨지므로, 머지 뒤 배포 직전에 한다. 데이터와 관리자 계정이 모두 사라진다.
첫 운영 배포 MR 은 `rule_catalog.FIRST_OPERATION_DEPLOY` 에 날짜를 넣는다 → `docs/design/rule-seed-preop-exception.md`.

```sh
C="docker compose --env-file /opt/simai/.env -f /opt/simai/repo/deploy/ec2/compose.yaml"
$C stop server
mkdir -p -m 700 /opt/simai/backups
$C exec -T db pg_dump -U postgres -Fc simai > /opt/simai/backups/simai-$(date +%Y%m%d-%H%M%S)-before-reset.dump
$C exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE simai WITH (FORCE)" -c "CREATE DATABASE simai"
# 그 판을 배포한다(자동 배포가 실패했으면 Jenkins 에서 다시 돌리거나 위 "손으로 같은 배포"). Flyway 가 V1 부터 만든다
# 관리자 계정을 다시 만든다("관리자 계정 만들기")
```

`before-reset` 덤프는 자동 정리(최근 5개)에 들어가니 오래 둘 것은 따로 옮긴다.
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
- 자동 배포 키: Jenkins 가 뚫리면 아무 소스나 배포할 수 있다(셸·포트 포워딩은 막힘).
- 정기 백업이 없다. 마이그레이션이 든 배포 전 덤프뿐이고, 덤프와 `db-data` 볼륨 모두 EC2 디스크에만 있다(EC2 를 잃으면 함께 잃는다).
- 이미 적용된 마이그레이션을 고쳐도 CI 는 빈 DB 에서 돌아 통과한다. 운영 배포에서야 server 가 뜨지 않아 되돌려진다.
- server 헬스체크가 포트만 본다. 기동은 됐지만 요청을 못 받는 상태는 잡지 못한다.
- 서버 IP 가 DNS 에 그대로 드러난다(프록시를 끔). 공개 포트는 80·443·22 뿐이다.

## 넘긴 일

- 정기 DB 백업과 EC2 밖 보관.
- CI 에서 이미 dev 에 있는 `V*.sql` 의 수정·삭제·번호 겹침을 막는다. 첫 운영 배포 전 예외(DB 초기화)가 끝난 뒤에 넣는다.
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

