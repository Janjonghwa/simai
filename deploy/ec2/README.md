# EC2 운영 절차

## 법령 그래프 시드 적재

V27 마이그레이션을 먼저 적용한다. 시드 SQL과 신뢰할 수 있는 배포 manifest의 SHA-256 항목을 별도로 확보한다. manifest는 `sha256sum` 형식으로 시드 파일 이름과 해시를 담는다. PostgreSQL 접속은 `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`와 `.pgpass` 등 psql 표준 설정을 사용한다.

```bash
bash deploy/ec2/load-legal-graph.sh /path/legal-graph.sql /path/manifest.sha256
```

해시가 일치할 때만 단일 트랜잭션으로 적재한다. 시드와 manifest는 이 저장소에 없다.
