# S15P31A101-682 GPU 세션 빈 구간 배포 보류 (Tier 0 연결·녹화 업로드)

- 앞 판: S15P31A101-676(`docs/design/S15P31A101-676-deploy-freeze-work-linked.md`): 진행 중 작업·세팅이면 보류, 타이머 배포, 12시간 안전장치, 수동 동결 우선, DB 를 못 물으면 배포.
- 코드: `deploy/ec2/deploy.sh`(`ACTIVE_SQL`·`reason_text`·`should_hold`·`env_int`). 주석만: `deploy-pending.sh`·`freeze.sh`. 절차: `deploy/README.md` "작업·세팅 연동 자동 보류".
- 시험: `ai/tests/deploy/test_ec2_deploy_hold.py`(가짜 DB 응답). SQL 은 임시 TimescaleDB 컨테이너(pg17)에 표 모양만 만들어 손으로 확인했다(아래 "확인").

## 문제

-676 은 작업·세팅이 진행 중일 때만 보류한다. GPU 세션 안의 빈 구간(Pod 준비~세팅 전, 세팅 완료~작업 시작 전, 작업 종료 뒤 녹화 업로드)에는 dev 머지가 그대로 배포돼 서버가 재시작한다. Tier 0 의 I1 연결·게이트웨이 업로드가 끊긴다.

## 결정

1. **Tier 0 연결 항목** `tier0 <이어진 구간 시작 epoch> <마지막 수신 epoch>`: `system_health` 의 최근 수신이 `SIMAI_DEPLOY_HOLD_TIER0_MINUTES`(기본 3분) 안이면 보류한다.
   - server 확인: `HealthIngestor`(I1 `health`) → `Tier0TelemetryRepository.insertHealth` 가 받을 때마다 한 행을 넣는다(같은 `sent_at` 재전송만 DO NOTHING). Tier 0 쪽 `I1Publisher` 는 (재)연결 직후 1회, 그 뒤 1 Hz(`HEALTH_INTERVAL_S = 1.0`)로 보내고, `HealthReporter.payload()` 는 작업·세팅 여부와 무관하게 늘 만든다. 재연결 백오프 상한은 30초(`Backoff.max_s`). 그래서 붙어 있으면 1초마다, 끊겨 다시 붙는 중이어도 30초 안에 행이 생긴다. 3분은 이 주기보다 충분히 길어 N 을 늘릴 이유가 없다.
   - 시각은 Lead 기본값의 `reported_at` 대신 **`received_at`**(서버가 받은 시각, EC2 시계)으로 잰다. `reported_at` 은 Tier 0 의 `sent_at`(Pod 시계)이라 Pod 시계가 틀어지면 3분 판정이 흔들린다. `now()` 와 같은 시계인 `received_at` 이 맞다. `reported_at` 은 hypertable 청크를 고르는 데만 쓴다(창 + 1시간 여유).
   - 미래 시각 행(`received_at > now() + 1분`, `seed-mock-history.sh` 같은 가짜 기록)은 보지 않는다.
2. **녹화 업로드 항목** `upload <이어진 구간 시작 epoch> <최근 발급 건수>`: `upload_grant` 에 `purpose = 'RECORDING_SEGMENT'`(V68) 이고 `issued_at` 이 `SIMAI_DEPLOY_HOLD_UPLOAD_MINUTES`(기본 10분) 안인 행이 있으면 보류한다. `issued_at` 은 서버 시계다(`UploadSignService`).
3. **키와 표시를 나눈다.** 항목 줄의 앞 두 칸이 키, 나머지는 표시용이다. `pending/ignored` 는 키를 적고 키로 가린다(awk). -676 의 `work 48`·`setup <uuid>` 는 두 칸이라 예전 `ignored` 파일도 그대로 맞는다.
   - Tier 0·업로드의 키는 **이어진 구간의 시작**이다: 수신(발급) 간격이 각 범위(3분·10분)를 넘은 마지막 지점, 없으면 창 안 첫 행. 마지막 수신·건수는 매번 바뀌지만 같은 연결이면 키가 같다. Pod 를 껐다 켜면(3분 넘게 끊김) 새 키라 다시 보류한다. 게이트웨이 ID 를 키로 쓰지 않은 까닭은 아래 "버린 대안".
   - 구간 시작을 찾는 창은 안전장치의 두 배(`HOLD_LOOKBACK_HOURS = 2 × SIMAI_DEPLOY_HOLD_MAX_HOURS`, 기본 24시간)다. 안전장치가 걸리는 때 연결의 나이는 "안전장치 시간 + 처음 보류 전에 이미 붙어 있던 시간"이라, 창이 이보다 길어야 키가 그 사이 바뀌지 않는다.
4. **안전장치가 넘긴 키를 잃지 않는다.** 안전장치가 걸리면 지금 진행 중인 항목 **전부**(이미 넘긴 것 포함)의 키를 `ignored` 에 쓴다. -676 판은 넘긴 것을 뺀 나머지만 덮어써, 예전에 넘긴 작업(종료를 잊은 작업 48)이 다음 배포를 다시 12시간 보류했다. 끝난 항목은 자연히 빠진다(작업·세팅 ID 와 구간 시작은 다시 나오지 않는다).
5. **사유**: `작업 48, 세팅 <uuid>, Tier 0 연결(마지막 보고 2026-11-13 05:00:12 UTC), 녹화 업로드 3건`. hold 4번째 줄·`freeze.sh status`·Jenkins 로그·`history.log` 에 그대로 남는다(형식 변경 없음). 메시지 머리는 `작업·세팅·GPU 세션 진행 중이라 배포 보류(...)`.
6. **그대로 둔 것**: 수동 동결 우선(동결 중이면 DB 도 묻지 않음), DB 를 못 물으면 경고하고 배포. 새 항목 줄이 형식에 맞지 않으면(칸 수·숫자) "알아볼 수 없는 응답"으로 같은 규칙을 탄다. 같은 SQL 한 번(UNION ALL)이라 `timeout 30`·`statement_timeout=10000` 도 그대로다.

## 명세·지시에 없어 정한 수치

| 값 | 정한 값 | 근거 |
|---|---|---|
| Tier 0 최근 수신 범위 N | 3분(`SIMAI_DEPLOY_HOLD_TIER0_MINUTES`) | Lead 기본값. health 1 Hz·재연결 백오프 상한 30초보다 충분히 길다. Pod 를 끄면 3분 + 타이머 최대 3분 뒤 배포된다 |
| 녹화 업로드 범위 M | 10분(`SIMAI_DEPLOY_HOLD_UPLOAD_MINUTES`) | Lead 기본값. 게이트웨이는 조각(5분·최대 120MB, `plan.py`)마다 서명을 받고 PUT·등록 뒤 다음 조각 서명을 받는다(동시 2개, `runner.py`). 업로드 주소 유효 5분(`StorageProperties.uploadTtl`)에 느린 망(시드니 S3)·재시도를 더해도 다음 발급까지 10분 안이다 |
| 구간 시작 창 | 안전장치 × 2(기본 24시간) | 결정 3. 1 Hz 24시간 ≈ 86,000 행. 임시 DB 에서 30시간 연속 행으로 33 ms |
| 미래 시각 허용 | 1분 | 시계 오차는 받아들이고 가짜(미래) 기록만 뺀다 |
| 설정 형식 | 1 이상 정수, 틀리면 경고하고 기본값 | -676 의 `SIMAI_DEPLOY_HOLD_MAX_HOURS` 와 같다(`env_int` 로 묶었다) |

## 호스트 설치

- 새 패키지·유닛·디렉터리·권한은 없다. SQL 은 기존처럼 `docker exec simai-ec2-db-1 psql` 로 돈다.
- **배포는 설치본 `/opt/simai/bin/deploy.sh` 를 쓴다.** Jenkins 키의 `command=` 가 그 파일을 가리키고, 배포가 푼 `/opt/simai/repo` 의 새 deploy.sh 로 자기 자신을 바꾸지 않는다. 그래서 이 MR 이 머지·배포돼도 동작은 바뀌지 않는다. 머지 뒤 Lead 가 EC2 에서 `cd /opt/simai/repo && cat REVISION` 로 이 커밋 이후 판인지 보고 `sudo sh deploy/ec2/install-host.sh` 를 다시 실행해야 한다(-676 과 같은 절차, 타이머 유닛은 그대로).
- `.env` 에 새 값은 넣지 않아도 된다(기본값).

## 확인

- 가짜 DB 시험: Tier 0 최근(보류·사유·SQL 조건)/오래됨(Pod 끈 뒤 타이머가 배포), 업로드 최근/오래됨, 사유 순서, N·M 설정과 틀린 값, Tier 0 안전장치(같은 연결은 다시 보류하지 않음·Pod 재시작은 다시 보류), 예전에 넘긴 항목 유지, 업로드 키, 형식이 틀린 새 줄.
- SQL 손 확인(임시 `timescale/timescaledb-ha:pg17-all`, 표 모양만): 5시간 전~2시간 전 연결 + 40분 전~30초 전 연결 → `tier0 <now-40분> <now-30초>`. 업로드 두 구간 → `upload <now-50분> 2`. VIDEO_SEGMENT·미래 시각 행은 무시. 4분 전 health·11분 전 발급만 남기면 줄 없음. 빈 표는 줄 없음.

## 버린 대안

- **게이트웨이 ID(`gateway_id`)·기기 ID 를 키로**: 안전장치가 한 번 넘기면 그 게이트웨이의 다음 GPU 세션도 영원히 보류하지 않는다. 구간 시작을 키로 하면 같은 연결 동안만 넘긴다. 게이트웨이 ID 는 자유 텍스트라 응답 형식 검사도 느슨해진다.
- **넘긴 Tier 0 키를 "다음에 안 보이면 지우기"**: 확인은 배포가 올 때만 돈다. 그 사이 Pod 를 껐다 켜도 알 수 없다.
- **`reported_at` 으로 판정**: Pod 시계 기준이라 EC2 `now()` 와 어긋날 수 있다(결정 1).
- **최근 행 존재만 보고 키 없이**: Tier 0 는 Pod 를 끄지 않으면 계속 이어진다. 안전장치가 넘긴 뒤 바로 다음 커밋이 다시 12시간 보류된다.
- **창 없이 전체 이력에서 구간 시작 찾기**: 압축 청크(7일 뒤)까지 풀어 읽어 10초 제한에 걸릴 수 있다.
- **Tier 0 가 따로 하트비트 파일·표를 쓰기**: health 가 이미 1 Hz 로 저장된다. 새 계약·마이그레이션이 필요 없다.

## 잔여 위험

- **Pod 를 켰지만 Tier 0 가 아직 I1 에 붙지 않은 구간**(이미지 받기·모델 적재)은 막지 못한다. health 행이 생기기 전이다. 그 사이 배포가 끝나면 Tier 0 는 다시 붙는다(재연결). 길게 막아야 하면 수동 동결을 건다.
- **서버가 health 를 저장하지 못하는 상태**(I1 등록 거부·계약 오류·서버 고장)면 Tier 0 가 붙어 있어도 행이 없어 보류하지 않는다. 고치는 배포가 막히지 않는 쪽이라 받아들인다.
- Tier 0 가 창(기본 24시간)보다 오래 이어지면 구간 시작이 창 첫 행으로 밀려 키가 바뀐다. 안전장치로 넘긴 뒤에도 다음 커밋이 다시 최대 12시간 보류된다(배포는 12시간마다 나간다).
- Pod 를 켜 둔 채 잊으면 dev 배포가 최대 12시간 늦는다(안전장치). 세션 끝 절차(Pod Stop)를 지키면 3~6분 뒤 나간다.
- 게이트웨이 업로드가 조각 사이에 10분 넘게 쉬면(아주 느린 망·큰 조각 재시도) 그 틈에 배포가 들어갈 수 있다. 업로드는 올리지 못한 조각(uploaded·rejected 가 아닌 것)을 다음 실행에서 다시 올리므로(같은 segment_id, DUPLICATE 허용) 손실은 없다.
- 실제 EC2·운영 DB 에서는 시험하지 않았다. 설치 뒤 GPU 세션 하나에서 `freeze.sh status` 의 `Tier 0 연결(…)` 사유와 Pod Stop 뒤 자동 배포를 Lead 가 확인해야 한다.
