# S15P31A101-676 배포 동결을 작업 시작·종료와 세팅에 자동 연동

- 후속: S15P31A101-682 가 보류 항목에 Tier 0 연결(`system_health` 최근 수신)·녹화 업로드(`RECORDING_SEGMENT` 최근 발급)를 더하고, 안전장치가 넘긴 항목을 키(앞 두 칸)로 가리도록 바꿨다(`docs/design/S15P31A101-682-deploy-hold-gaps.md`). 아래 SQL·항목 형식은 -676 판이다.
- 관련: S15P31A101-582(수동 동결 `freeze.sh`, `docs/design/deploy-freeze-heartbeat.md`), S15P31A101-254(자동 배포, `docs/design/ec2-deploy.md`), 명세 3.12·18(운영).
- 코드: `deploy/ec2/deploy.sh`(판단·보류·배포), `deploy/ec2/deploy-pending.sh`(타이머 입구), `deploy/ec2/freeze.sh`(status 에 보류 표시), `deploy/ec2/systemd/simai-deploy-pending.{service,timer}`, `deploy/ec2/install-host.sh`. 절차: `deploy/README.md` "작업·세팅 연동 자동 보류".
- 시험: `ai/tests/deploy/test_ec2_deploy_hold.py`(가짜 docker·flock·curl·timeout 로 셸 스크립트를 돌린다), `test_ec2_freeze.py`.

## 문제

수동 동결은 GPU 세션 전에 손으로 걸고 풀어야 한다. 잊으면 관제 중에 dev 머지가 서버를 다시 띄우고(14차 세션 위험), 풀고 나서는 건너뛴 커밋을 손으로 다시 배포해야 한다.

## 결정

1. **배포 직전 DB 확인.** deploy.sh 가 `deploy.lock` 을 잡고 수동 동결을 본 다음, 운영 DB 를 읽기 전용으로 묻는다:
   `timeout 30 docker exec -e PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=10000' simai-ec2-db-1 psql -X -q -v ON_ERROR_STOP=1 -U postgres -d simai -tA -c "<아래 SQL>"`.
   ```sql
   SELECT 'work ' || work_id FROM work_unit WHERE actual_started_at IS NOT NULL AND actual_ended_at IS NULL
   UNION ALL
   SELECT 'setup ' || setup_session_id FROM setup_session s WHERE s.status NOT IN ('COMPLETED', 'FAILED')
       OR EXISTS (SELECT 1 FROM setup_supplement_rebake r
                  WHERE r.setup_session_id = s.setup_session_id AND r.status IN ('QUEUED', 'RUNNING'))
   ```
   - 작업: V30 의 CHECK 가 `IN_PROGRESS ⇔ actual_started_at 있음·actual_ended_at 없음` 을 보장한다. 현장 구분 없이 모든 현장을 본다(EC2 한 대가 모든 현장을 띄운다).
   - 세팅: 종결 상태는 V33 CHECK 의 상태 목록 중 `COMPLETED`·`FAILED` 뿐이다(관리자 취소·대기 시간 초과도 `FAILED`, `SetupService.cancel`·`closeAndCancelJobs`). `READY_FOR_ZONES`(구역 그리기 대기)·`BAKING_TEXTURE` 도 진행 중이다. 끝난 세션의 맵 보강 다시 굽기(V53 `setup_supplement_rebake` QUEUED·RUNNING)도 GPU 작업이 서버와 주고받는 중이라 진행 중으로 본다. 이 조건은 `SetupService.start` 가 새 세팅을 막는 조건과 같다.
   - 응답 줄이 `work <숫자>`·`setup <uuid>` 꼴이 아니면 알아볼 수 없는 응답으로 보고 아래 실패 규칙을 따른다.
   - `docker compose exec` 대신 `docker exec <컨테이너>` 를 쓴다. compose 는 `.env` 보간 검사(`SIMAI_DOMAIN:?` 등)에 걸리면 DB 와 무관하게 실패한다. 컨테이너 이름은 compose 의 `name: simai-ec2` 에서 나온다.
2. **보류.** 진행 중이 있으면 Jenkins 입력 tar 를 `/opt/simai/pending/<커밋>.tar` 로 옮기고 `pending/hold`(커밋, 처음 보류 epoch, 마지막 보류 epoch, 사유, 시도 횟수)를 원자적으로 쓴 뒤 0 으로 끝낸다(Jenkins 빌드 성공, 로그 `작업·세팅 진행 중이라 배포 보류(작업 48)`). 보류는 하나만 둔다. dev 머지는 앞으로만 가므로 최신 커밋이 이전 커밋을 포함한다. 대체될 때 처음 보류 시각은 이어받는다(안전장치가 머지마다 다시 시작되지 않게).
   - 입력은 먼저 `pending/.incoming.*` 에 받고 `tar -t` 로 확인한다. 망가진 입력이 멀쩡한 보류를 바꾸지 않는다.
3. **두 번 확인.** bake 는 몇 분 걸린다. 그 사이 작업이 시작될 수 있어, 서비스를 건드리기(덤프·repo 교체·up) 직전에 한 번 더 묻는다. 이때 진행 중이면 같은 방식으로 보류한다(만든 이미지는 캐시로 남아 다음 시도가 빠르다).
4. **보류 배포.** systemd 타이머가 앞 실행이 끝나고 3분 뒤마다 `deploy-pending.sh` → `deploy.sh pending` 을 부른다. 같은 파일·같은 순서를 쓰므로 코드 중복이 없다. 강제 명령 SSH(`command=`)로는 인자를 넘길 수 없어 Jenkins 키로 `pending` 입구를 열 수 없다. `SSH_ORIGINAL_COMMAND` 가 있으면 언제나 Jenkins 입구로만 해석한다.
   - 잠금은 기다리지 않는다(`flock -n`). 다른 배포·동결 설정이 쥐고 있으면 다음 차례.
   - 수동 동결이 우선이다. 동결 중이면 DB 도 묻지 않고 끝낸다.
   - 시작 전에 시도 횟수를 올린다. 중간에 실패해 `set -e` 로 끝나도 횟수가 남는다. 3번 실패하면 자동 실행을 멈춘다(새 커밋이 오면 횟수 0 으로 다시). 실패마다 되돌리기(`up` 이전 판)가 서비스를 다시 띄우므로 3분마다 끝없이 다시 하지 않는다.
   - 성공하면 보류를 지운다. 바로 배포된 새 Jenkins 커밋도 이전 보류를 지운다(더 새 판이 올라갔다).
5. **12시간 안전장치.** 처음 보류에서 `SIMAI_DEPLOY_HOLD_MAX_HOURS`(`/opt/simai/.env`, 기본 12) 시간이 지나면 진행 중이어도 배포하고 경고한다. 그때 진행 중이던 항목은 `pending/ignored` 에 적어 다음 배포부터 그 항목으로는 보류하지 않는다. 적지 않으면 작업 종료를 잊은 작업 하나가 이후 모든 배포를 12시간씩 늦춘다. 새로 시작한 작업은 다시 보류한다.
   - 값을 `.env` 에서 읽는 까닭: 강제 명령 SSH 와 systemd 는 호출자 환경 변수를 넘기지 않는다. deploy.sh 는 이미 `.env` 의 `SIMAI_TAG` 를 읽는다.
6. **DB 를 묻지 못하면 보류하지 않고 배포한다(경고).** 컨테이너가 없거나(처음 설치·DB 재생성), 스키마가 아직 테이블을 모르거나(마이그레이션 전), psql 오류·30초 초과면 보류 근거가 없다. 막는 쪽을 고르면 DB 를 고치는 배포조차 영원히 보류된다. 안전 관제가 막히는 것보다 배포가 막히는 것이 운영에 더 오래 남는다고 보고, 수동 동결을 최종 보호 장치로 둔다.
7. **수동 동결은 그대로.** 동결 중에 온 커밋은 지금처럼 입력을 비우고 건너뛴다(저장하지 않는다). `freeze.sh status` 는 동결 줄 아래 `자동 보류 중 (커밋·사유·처음/마지막 보류·시도)` 또는 `자동 보류 없음` 을 함께 보인다.
8. **기록.** `pending/history.log`(UTC 한 줄씩: 보류·대체·시도·실패·완료·안전장치), 타이머 출력은 journal(`journalctl -u simai-deploy-pending`).

## 명세·지시에 없어 정한 수치

| 값 | 정한 값 | 근거 |
|---|---|---|
| 타이머 간격 | 앞 실행이 끝나고 3분(`OnUnitInactiveSec`), 켠 뒤 1분·부팅 뒤 5분 | 지시 2~5분. 끝난 뒤부터 재므로 긴 배포와 겹치지 않는다 |
| 보류 배포 재시도 | 3번 | 일시 실패(망·레지스트리) 한두 번은 넘기고, 반복 되돌리기로 서비스를 흔들지 않는다 |
| DB 조회 제한 | `timeout 30`, `statement_timeout=10000` | 조회는 인덱스 없는 작은 표 두세 개. 막힌 DB 가 잠금을 오래 쥐지 않게 |
| 서비스 시간 제한 | `TimeoutStartSec=infinity` | 배포 도중 끊으면 반쯤 바뀐 채 남는다. 단계별 제한(up 300초·curl 재시도)이 있다 |

## 버린 대안

- **서버가 호스트 파일(`deploy.freeze`)을 직접 쓰기**: 작업 시작·종료 API 에서 동결 파일을 만들고 지우는 방식. server 컨테이너에 호스트 `/opt/simai` 쓰기 권한(바인드 마운트)을 줘야 하고, 서버가 죽거나 종료를 놓치면 파일이 남는다. 상태의 정본은 이미 DB 에 있으므로 배포 쪽이 DB 를 읽는 편이 하나의 정본을 지킨다.
- **Jenkins 쪽에서 막기**: 배포 단계에서 DB·API 를 물어 실패·대기시키기. 실패로 끝내면 작업이 끝난 뒤 누군가 빌드를 다시 돌려야 하고, 대기시키면 Jenkins 실행기가 몇 시간 묶인다. Jenkins 는 운영 DB 망(ec2-internal)에도 붙어 있지 않다.
- **deploy-pending.sh 에 배포 로직 복사 / 공용 라이브러리 파일**: 복사는 두 판이 어긋나고, 라이브러리는 설치 파일이 늘어 하나만 갱신되는 사고가 난다. deploy.sh 한 파일에 입구 두 개를 둔다.
- **deploy-pending.sh 가 잠금을 쥐고 deploy.sh 를 자식으로 부르기**: deploy.sh 가 같은 파일을 새로 열어 `flock` 하면 자기 부모와 경합해 30분 기다린다.
- **수동 동결 중에 온 커밋도 보류로 저장**: 동결 해제 뒤 자동 배포가 되어 편하지만, 지시가 동결 동작을 그대로 두라고 했고 기존 시험·절차(세션 후 다시 배포)와 어긋난다. 후속으로 고를 수 있다.
- **안전장치를 보류 시각 대신 작업 시작 시각으로**: 오래 열린 작업은 보류 없이 바로 배포하는 방식. 실제로 긴 관제(밤샘)를 끊을 수 있고, 보류 시각 기준이 지시와 같다.

## 잔여 위험

- 두 번째 확인과 `up` 사이(덤프·repo 교체, 보통 몇 초~덤프 시간)에 시작된 작업은 막지 못한다. 서버가 배포 중임을 알고 작업 시작을 막는 방식은 이번 범위 밖이다.
- 12시간 안전장치는 실제 12시간 넘게 이어진 관제도 끊는다(지시대로). 그 항목은 이후 보류하지 않는다. 길게 돌릴 날은 수동 동결을 건다.
- DB 를 묻지 못하면 진행 중이어도 배포한다(경고만). 관제 중 DB 컨테이너만 죽은 경우가 여기에 든다.
- 끝난 세팅이라도 다시 굽기 행이 QUEUED·RUNNING 으로 남으면(워커가 죽고 서버가 회수하지 못함) 안전장치 시간까지 보류된다.
- 호스트 설치·타이머는 실제 EC2 에서 시험하지 않았다(가짜 환경 시험뿐). 머지 뒤 Lead 가 `install-host.sh` 로 설치하고, 보류·자동 배포 한 번을 실제로 확인해야 한다. 타이머 사용자(기본 ubuntu)가 Jenkins 키 사용자와 다르면 잠금·보류 파일 권한이 어긋난다.
- `/opt/simai/deploy.lock` 을 root(`sudo freeze.sh`)가 먼저 만들면 배포 사용자가 열지 못하는 문제는 예전부터 있었다. `install-host.sh` 가 없을 때만 배포 사용자 소유로 만든다.
