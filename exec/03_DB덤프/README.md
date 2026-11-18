# DB 덤프

- DBMS: **PostgreSQL 17** (`timescale/timescaledb-ha:pg17-all`) + 확장 `timescaledb`, `postgis`, `vector`(pgvector), `pg_trgm`
- DB(스키마) 이름: `simai`, 스키마 `public` (Flyway 기본, 이력 테이블 `flyway_schema_history`)
- 계정: `postgres` / `<DB_PASSWORD>` (EC2 `/opt/simai/.env` 의 `POSTGRES_PASSWORD`)
- 문자셋: `UTF8` / 콜레이션·Ctype: 아래 명령으로 확인한다
  ```sh
  docker compose --env-file /opt/simai/.env -f /opt/simai/repo/deploy/ec2/compose.yaml exec -T db \
    psql -U postgres -d simai -c "SELECT datname, pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database WHERE datname='simai';"
  ```
- 스키마 정본: `server/src/main/resources/db/migration/V*.sql` (빈 DB 에서 서버를 띄우면 Flyway 가 그대로 만든다)

파일명: `dump_YYYYMMDD.sql` (이 폴더에 넣는다). 저장소에는 gzip 으로 압축한 `dump_YYYYMMDD.sql.gz` 를 둔다.

## 들어 있는 덤프: `dump_20261118.sql.gz`

- 원본: 운영 EC2 `simai` DB, 2026-11-18 12:36 KST. 압축 약 30 MB(풀면 약 138 MB), sha256 `270d9ca1eb2e4a3df22dfd9dde23bb772f4b6e15238d3be7739513a3328520a9`
- 스키마 전체(확장·하이퍼테이블·인덱스·Flyway 이력)와 기준 데이터: 현장·카메라·기기·계정, 보정·맵(scene)·구역·규칙,
  작업·보고서·서명, 위험 이벤트·이벤트 변경 이력, 법령 RAG(노드·간선·조항), 분석·GPU 세션 기록 등.
- 데이터를 뺀 것(표 정의는 남아 복원 뒤 빈 표로 생긴다):
  - 관제 시계열 하이퍼테이블 8개(`world_track_state`, `skeleton_3d`, `system_metric`, `system_health`, `device_heartbeat`,
    `ppe_state`, `watch_feature`, `channel_state`). 운영 DB 2.0 GB 중 약 1.8 GB 라 저장소에 넣을 수 없다.
  - 로그인 세션·임시 권한: `refresh_token`, `upload_grant`, `job_lease`
  - 열람 기록·작업용 사본: `video_access_log`, `clip_access_log`, `risk_event_stale_backup_20261115`
- 계정(`app_user`)은 비밀번호 bcrypt 해시째 들어 있어 복원하면 운영 계정으로 그대로 로그인된다. 기기·워커·GPU 부팅 토큰은
  해시만 저장하므로 원문은 없다(원문은 각 장비의 `.env`).
- 영상·맵(glb)·사진 원본은 S3 에 있고 DB 에는 객체 키만 있다.
- 만든 명령(EC2):

  ```sh
  docker exec simai-ec2-db-1 pg_dump -U postgres -d simai --no-owner --no-privileges \
    --exclude-table-data='_timescaledb_internal._hyper_*' --exclude-table-data='_timescaledb_internal.compress_hyper_*' \
    --exclude-table-data='public.refresh_token' --exclude-table-data='public.upload_grant' \
    --exclude-table-data='public.video_access_log' --exclude-table-data='public.clip_access_log' \
    --exclude-table-data='public.job_lease' --exclude-table-data='public.risk_event_stale_backup_20261115' \
    > dump_20261118.sql && gzip -9 dump_20261118.sql
  ```

- 복원 확인: EC2 `simai-ec2-ci-db-1`(같은 이미지, TimescaleDB 2.31.0)의 임시 DB 에 아래 「복원」 절차대로 넣어 오류 0건,
  `timescaledb_post_restore()` 정상, 하이퍼테이블 8개가 생기고 표 데이터가 들어간 것을 확인했다.
- **TimescaleDB 확장 판이 덤프와 같아야 한다(2.31.0).** 이미지 태그가 같아도 받은 시점에 따라 판이 다를 수 있다
  (예: 2.30.2 이미지에서는 `timescaledb_post_restore()` 가 `catalog version mismatch` 로 실패). `docker pull` 로 최신 이미지를 받는다.

## 덤프 생성 (EC2, 컨테이너 기준)

```sh
C="docker compose --env-file /opt/simai/.env -f /opt/simai/repo/deploy/ec2/compose.yaml"
$C exec -T db pg_dump -U postgres -d simai --no-owner --no-privileges > dump_$(date +%Y%m%d).sql
```

컨테이너 이름으로 직접 할 때(compose 프로젝트 이름 `simai-ec2`):

```sh
docker exec -t simai-ec2-db-1 pg_dump -U postgres -d simai --no-owner --no-privileges > dump_$(date +%Y%m%d).sql
```

EC2 → 개발 PC 로 가져오기: `scp -i <EC2_PEM_PATH> ubuntu@<EC2_HOST>:~/dump_YYYYMMDD.sql ./exec/03_DB덤프/`

## 복원

TimescaleDB 하이퍼테이블이 있으므로 복원 전후에 `timescaledb_pre_restore()` / `timescaledb_post_restore()` 를 호출한다.
복원할 DB 는 같은 이미지(`timescaledb-ha:pg17-all`)여야 확장이 모두 있다.

```sh
# 1) 빈 DB 와 확장 준비
docker exec -i <DB_CONTAINER> psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS simai WITH (FORCE)" -c "CREATE DATABASE simai"
docker exec -i <DB_CONTAINER> psql -U postgres -d simai -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()"

# 2) 덤프 적용 (압축본은 풀면서 넣는다)
gzip -dc dump_YYYYMMDD.sql.gz | docker exec -i <DB_CONTAINER> psql -U postgres -d simai
# 압축하지 않은 파일이면: docker exec -i <DB_CONTAINER> psql -U postgres -d simai < dump_YYYYMMDD.sql

# 3) 마무리
docker exec -i <DB_CONTAINER> psql -U postgres -d simai -c "SELECT timescaledb_post_restore()"
```

- 운영 EC2 에서 복원할 때는 먼저 `$C stop server` 로 서버를 멈추고, 끝나면 `$C up -d server`.
- 운영 자동 백업은 `deploy.sh` 가 마이그레이션 변경 시 `/opt/simai/backups/*.dump`(custom 형식)로 남긴다. 그 파일은 `pg_restore -U postgres -d postgres --clean --create` 로 복원한다(`docs/design/ec2-deploy.md` 「되돌리기」).
