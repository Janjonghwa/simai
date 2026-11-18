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

파일명: `dump_YYYYMMDD.sql` (이 폴더에 넣는다)

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

# 2) 덤프 적용
docker exec -i <DB_CONTAINER> psql -U postgres -d simai < dump_YYYYMMDD.sql

# 3) 마무리
docker exec -i <DB_CONTAINER> psql -U postgres -d simai -c "SELECT timescaledb_post_restore()"
```

- 운영 EC2 에서 복원할 때는 먼저 `$C stop server` 로 서버를 멈추고, 끝나면 `$C up -d server`.
- 운영 자동 백업은 `deploy.sh` 가 마이그레이션 변경 시 `/opt/simai/backups/*.dump`(custom 형식)로 남긴다. 그 파일은 `pg_restore -U postgres -d postgres --clean --create` 로 복원한다(`docs/design/ec2-deploy.md` 「되돌리기」).
