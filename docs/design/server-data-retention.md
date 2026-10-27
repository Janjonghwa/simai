# EC2 데이터 보관 정책과 자동 삭제

S15P31A101-72. 명세 11.5(보관과 개인정보: 클립 14일·보존 표시·hypertable 보관·압축, "자동 삭제는 EC2 클립 삭제 작업, TimescaleDB 보관 정책,
Tier 0 저장 위치 정리 작업으로", "객체 저장소 수명 주기 규칙은 클립에 쓰지 않는다"), 18.4(주 1회 데이터 삭제 확인), 16장 결정.
코드는 `server/.../domain/clip/ClipPurgeService`·`ClipPurgeScheduler`, `server/.../global/storage/ObjectStorage.delete`,
점검 쿼리는 `deploy/ec2/retention-check.sql`, 시험은 `ClipPurgeIntegrationTest`.

## 무엇이 지우는가

| 데이터 | 지우는 곳 | 상태 |
|---|---|---|
| 이벤트 클립 객체(14일) | EC2 클립 삭제 작업(이 문서) | 이번에 만듦 |
| 클립 기록이 오지 않은 클립 업로드 객체 | EC2 클립 삭제 작업 | 이번에 만듦 |
| `system_metric`·`device_heartbeat` 30일 | TimescaleDB 보관 정책 | V1 에 있음(`add_retention_policy`). 이번에 점검 쿼리만 더함 |
| `world_track_state`·`skeleton_3d`·`system_health`·위 둘 | TimescaleDB 압축 정책(7일) | V1 에 있음. 상시 삭제 없음(리플레이용, 발표 후 일괄 파기) |
| 2D 관측(RunPod Parquet)·Tier 0 미업로드 클립 | Tier 0 저장 위치 정리 작업 | Tier 0(ai) 일. 이 이슈 범위 밖 |
| 전체(보존 표시분 포함) | 발표 후 30일 안 일괄 파기 | 수동 절차. 아래 "남은 일" |

## 클립 삭제 작업 흐름

`ClipPurgeScheduler` 가 기동 1분 뒤부터 10분마다(`simai.clip.purge-interval`) 전용 스레드(`clipPurgeTaskScheduler`)에서 `ClipPurgeService.purge` 를 부른다. 저장소(`SIMAI_STORAGE_BUCKET`)가 비어 있으면 아무것도 하지 않는다.

1. **CRITICAL 보존**: 삭제 예정일이 지났고 보존 표시가 없지만 이벤트(`risk_event.severity`)가 지금 CRITICAL 인 클립에 `retention_hold = true` 와 `CRITICAL_AUTO` 이력을 남긴다. 지우지 않는다.
2. **클립 삭제**: `deleted_at IS NULL AND NOT retention_hold AND delete_after <= now` 이고 이벤트가 CRITICAL 이 아닌 클립을 하나씩, 클립 행을 `FOR UPDATE`·이벤트 행을 `FOR SHARE`(둘 다 `SKIP LOCKED`)로 잠근 트랜잭션 안에서 객체를 지우고(`DeleteObject`) `deleted_at = now` 를 쓴다.
3. **쓰이지 않은 업로드**: `purpose = 'EVENT_CLIP' AND used_at IS NULL AND expires_at <= now - 14일` 이고 이벤트가 CRITICAL 이 아닌 발급을 같은 방식으로 잠그고 객체와 발급 행을 지운다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 주기·스레드 | 10분(`PT10M`), 기동 뒤 1분. 한 주기 최대 1000건씩(`simai.clip.purge-max-per-run`). 공용 스케줄러가 아닌 전용 스레드 하나 | 보관 기간이 일 단위라 분 단위 지연은 상관없다. 공용 스케줄러(`SchedulingConfig`)는 스레드 하나로 1초 주기 작업(journal_ack·대여 만료·클립 재시도)을 돌려, 저장소가 느릴 때 그 작업들이 밀리지 않게 |
| 저장소 호출 시간 | `DeleteObject` 한 번 10초, 재시도 포함 30초 | 저장소가 멈춰도 행 잠금·DB 커넥션을 오래 쥐지 않게(잠긴 클립에 보존 표시를 붙이는 관리자 요청·Tier 0 보존 기록이 그만큼 기다린다) |
| 잠금 | 클립 하나에 트랜잭션 하나. 행 잠금을 쥔 채 객체를 지우고 `deleted_at` 을 쓴다. 잠긴 행은 건너뛴다(SKIP LOCKED) | 관리자 보존 표시(`ClipService.setRetention`)와 Tier 0 보존 기록(`MediaClipRetentionIngestor`)이 같은 행을 `FOR UPDATE` 로 잠근다. 고른 뒤 보존 표시가 붙은 클립을 지우지 않게 |
| 순서·실패 | 객체를 먼저 지우고 성공해야 `deleted_at` 을 쓴다. 지우지 못한 행은 되돌리고 이번 주기에서만 빼고 다음 행으로 간다. 연달아 3번 실패하면 저장소가 멈춘 것으로 보고 주기를 끝낸다(다음 주기에 이어서). 경고 로그에는 `clip_id`·`grant_id` 와 저장소 상태·오류 코드만 남기고 키는 남기지 않는다 | `deleted_at` 이 있는데 객체가 남는 일이 없게. 반대(객체는 지웠는데 커밋 실패)는 다음 주기에 같은 키를 다시 지운다(DeleteObject 는 없는 키도 성공). 키 하나의 영구 오류(권한·잘못된 키)가 뒤의 클립을 모두 막지 않게 |
| 이벤트가 CRITICAL 로 오른 클립 | 지우지 않고 EC2 가 보존 표시와 `CRITICAL_AUTO` 이력(Tier 0·관리자 칸 비움)을 남긴다. 이후 관리자는 떼지 못한다(`auto_retained`). 지우는 동안 이벤트 행을 `FOR SHARE` 로 잠가 그 사이 CRITICAL 로 오르지 못하게 한다(오르는 쪽이 최대 30초 기다린다) | 11.5 "CRITICAL 이벤트 연결분은 자동 보존". 보통 Tier 0 `media_clip_retention` 이 먼저 붙이지만 그 기록이 사라졌을 때 지워 버리면 되돌릴 수 없다(-261 남은 일). 14일 지난 이벤트라 갱신이 드물다 |
| 쓰이지 않은 업로드 | 마지막 발급 만료 14일 뒤(`simai.clip.orphan-grant-after`) 객체와 `upload_grant` 행을 지운다. 이벤트가 CRITICAL 이면 지우지 않는다(늦게 올 클립이 자동 보존 대상, 발표 후 일괄 파기). 프레임·사진 발급은 건드리지 않는다 | 기록이 오지 않은 클립도 얼굴이 담긴 영상이라 14일 안에 지운다. 14일 안에 기록이 오면 쓰인 발급이 된다. 재발급·기록 수신이 같은 행을 잠그므로 엇갈리지 않는다. 프레임·사진은 11.5 에 보관 기간이 없다(발표 후 파기) |
| 수명 주기 규칙 | 쓰지 않는다. 운영 버킷은 버전 관리를 끈다 | 11.5. 버전 관리가 켜져 있으면 DeleteObject 가 지운 표시만 남기고 이전 판이 남아 실제로 지워지지 않는다 |
| 권한 | EC2 인스턴스 역할에 `s3:DeleteObject` 를 더한다 | 서명만 하던 EC2 가 처음으로 저장소를 직접 부른다 |
| 마이그레이션 | 없음(V14 쓰지 않음) | hypertable 보관·압축 정책은 V1 에 있고, 삭제 작업은 기존 `media_clip_delete_after_idx` 로 충분하다 |
| 점검 쿼리 | `deploy/ec2/retention-check.sql`. 클립은 `delete_after` 가 1시간 넘게 지난 미삭제 0건, 쓰이지 않은 업로드 0건, 보관·압축 정책 7개의 마지막 실행 Success, `system_metric`·`device_heartbeat` 의 37일(30일 + 청크 7일) 넘은 청크 0개, 보존 표시 클립의 마지막 사유 | 이슈 완료 조건 "삭제 예정일이 지난 데이터가 0건". 주기(10분)·청크 간격(7일)만큼 여유를 둔다 |

## 18.4 주 1회 확인

```bash
cd /opt/simai/repo
docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml exec -T db psql -U postgres -d simai < deploy/ec2/retention-check.sql
df -h /   # EC2 디스크 사용률. 70% 를 넘으면 아래 skeleton_3d 정책을 건다(11.5)
```

0 이 아니면 서버 로그에서 `클립 삭제 작업이 멈춤` 을 찾는다(저장소 권한·네트워크). 디스크 70% 초과 때 거는 정책(마이그레이션으로 더한다):

```sql
SELECT add_retention_policy('skeleton_3d', INTERVAL '30 days');
```

## 시험

`ClipPurgeIntegrationTest`(저장소는 MockitoBean, 실제 DB): 지난 클립만 지우고 `deleted_at`(보존 표시·아직인 것은 그대로)과 점검 0건, 지운 클립은 다시 지우지 않음,
이벤트가 CRITICAL 인 클립은 보존 표시·`CRITICAL_AUTO` 이력, 저장소 오류면 `deleted_at` 없이 다음 주기에 지움, 키 하나가 계속 실패해도 뒤 클립·업로드는 지움,
저장소 미설정이면 아무것도 안 함, 다른 트랜잭션이 잠그고 보존 표시를 붙인 클립은 기다리지 않고(5초 안) 건너뛰고 이후에도 지우지 않음,
쓰이지 않은 업로드는 만료 14일 뒤만 지우고 쓰인 발급·CRITICAL 이벤트의 발급은 남김.
점검 쿼리는 임시 DB 에서 직접 돌려 결과 모양을 확인했다.

## 남은 일

- 실제 S3(또는 MinIO)에서 DeleteObject 와 인스턴스 역할 권한을 확인한다(시험은 가짜 저장소). 운영 버킷 버전 관리 꺼짐도 배포 때 확인한다.
- Tier 0 저장 위치 정리 작업(2D 관측 7~14일, 보존 표시 없고 삭제 예정일이 지난 미업로드 클립)은 ai(Tier 0) 쪽 이슈로 한다.
- 발표 후 30일 안 일괄 파기(보존 표시분 포함)와 참여자 삭제 요청 처리 절차·기록: 지금은 수동이다. 절차 문서·관리 명령은 발표 일정이 정해지면 따로 한다.
- 클립 삭제 작업의 결과(지운 수·밀린 수)를 운영 지표로 내는 일은 시스템 지표 이슈(S15P31A101-70)에서 함께 본다.
- 시험이 다루지 않는 것: 객체를 지운 뒤 커밋 실패(다음 주기에 같은 키를 다시 지우는 멱등성에 기댄다), 재발급과 쓰이지 않은 업로드 삭제의 동시 실행(행 잠금 순서로 막는다).
- 같은 이유로 실패하는 클립은 주기마다 다시 시도하고 경고를 남긴다. 실패 횟수를 저장해 영구히 빼는 일은 두지 않았다(18.4 점검에서 0 이 아니면 사람이 본다).
