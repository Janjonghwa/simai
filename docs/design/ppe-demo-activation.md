# PPE 시연 예외 활성화와 이벤트 표시

S15P31A101-556. 관련 명세 §5.5, §7.2, §8.10, §15.1, §16, 부록 E. 선행 -548(계약·Tier 0 R11), -549(서버 vest 발행·워치), -550(V45), -531(PPE 화면 전달). PPE 프로세스 기동·수신 확인 절차는 S15P31A101-587 에서 더했다.

## 승인과 범위

Lead 이원준이 2026-11-09 이 대화에서 **G9·G6-R9·G6-R11 미통과 상태의 시연용 활성화 예외**를 승인했고, Jira 이슈 생성과 프런트 잔여 수정 포함을 지시했다. Jira: <https://ssafy.atlassian.net/browse/S15P31A101-556>.

- 시연·그 준비 검증에서 R9·R11 v1 을 시드에 넣어 활성화한다. G9 상태 품질·함정 사례·동시 부하와 G6 검출률·오경보 게이트는 미통과이며 합격·제품 성능으로 발표하지 않는다.
- 판정 임계값·버전·CAUTION·작업 기준 요구·UNKNOWN 유지·관측 착용 해소와 작업 종료 해소를 유지한다. LIVE 외 출처를 실제 워치·현장 알림으로 보내지 않는다.
- 시드에는 실행 장소를 가르는 장치가 없다. 이 서버가 만드는 새 묶음에 R9·R11 이 포함되므로 예외 적용 대상 서버·세션과 서버 재배포 시점은 팀과 조율한다. 머지·배포만으로 기존 묶음이 바뀌지는 않는다.
- 이 변경으로 운영 배포·GPU 기동·게이트 실측을 수행하지 않는다. 실행 중 Tier 0 판, 운영 DB 적용 상태, config_version 은 배포 검증 때 실제로 확인한다.

## 코드와 시험

| 부분 | 코드 | 검증 |
|---|---|---|
| 활성화 | `ai/src/simai_ai/risk/rule_catalog.py`의 `DISABLED_BY_DEFAULT`에서 R9·R11 제외 해제, 생성 `server/src/main/resources/db/migration/R__rule_versions.sql` | `ai/tests/risk/test_rule_catalog.py`, 생성물 `--check` |
| 기존 판정 유지 | R9·R11 규칙 동작은 그대로, 주석만 승인 정책과 맞춤 | `ai/tests/risk`, `ai/tests/config/test_ppe_policy.py` |
| 서버 전달 | 기존 Flyway·규칙 선택·새 구성 묶음 발행 경로 | `ConfigBundleIntegrationTest`: 시드 → rules 문서 → APPLIED → `/api/rules` R9·R11 active 검증. `RuleSceneIntegrationTest`: 적용 묶음에 없으면 여전히 비활성 |
| 화면 요구 읽기 | `features/zones/types.ts`: 기존 REST 요약 `vest`를 보존 | `__tests__/features/events/ppePolicy.test.ts` |
| 이벤트 표시 | `features/events/{constants,format,ppePolicy,incidentView,EventDetailPanels}`: R11 이름, 장비별 UNKNOWN, 사람 대상, 장비별 해소 | `ppePolicy.test.ts`, `EventPanel.test.tsx` |
| 요구 미수신 | `features/monitor/PpePolicyNotice.tsx`: 전체 PPE 또는 조끼만 미수신 안내 | `PpePolicyNotice.test.tsx` |

계약 스키마 변경은 없다. 이미 -549가 발행하는 REST 요약의 `vest`를 읽도록 프런트 파서를 보완한다. 서버가 `vest: null`을 주면 원래 문서에 조끼 요구가 없다는 뜻으로 R11 요구 해제로 읽는다. 이전 서버가 `vest` 요약 칸 자체를 생략하면 문서에 실렸는지 모르므로 해소 원인을 추측하지 않는다. `ppe_policy: null`은 전체 PPE 요구 미수신, 요약 자체가 undefined·형식 오류이면 모름이다. 해소 원인은 **그 해소 전이의 config_version**으로 찾고 최신 묶음으로 대신 판단하지 않는다.

## 배포와 현장 검증

1. 실행 중 Tier 0의 커밋·이미지에서 -548 포함 판인지 확인한다. 단순 dev 머지 여부로 대신하지 않는다. `ppe_policy` 1.1.0과 R11을 알아야 한다. 이전 Tier 0는 vest가 있는 묶음 전체를 `SCHEMA_INVALID`로 거부해 안전모 요구도 새로 받지 못한다.
2. 서버를 내려도 되는 시점에 팀과 조율해 배포한다. Flyway 반복 시드가 R9·R11 v1 을 넣고, V45 적용도 확인한다. 기존 시계열·이벤트·구성 묶음을 지우지 않는다.
3. 보정·구역·작업면과 워치 연결을 준비한 뒤 새 묶음을 발행한다. 묶음 rules에 R9·R11, PPE 요구 문서에 `format_version: 1.1.0`과 `helmet`·`vest`를 확인한다. 작업 시작·종료는 각각 새 묶음을 발행한다.
4. `GET /api/config-bundles/{version}`의 `status: APPLIED`, `last_ack.reject_reason`, 현장의 적용 config_version을 확인한다. 거부·PENDING·config_version null이면 준비 완료로 보지 않는다. `/api/rules`의 R9·R11 active를 적용 묶음과 대조한다.
5. PPE 프로세스를 띄우고 상태가 들어오는지 확인한다(S15P31A101-587). PPE 는 명시해야 뜨고 `simai-bootstrap lobby`·`preflight` 기본(`tier0,vllm,worker,avatar`)에는 없다(명세 3.12, `deploy/scripts/simai_bootstrap.py`의 `LOBBY_SERVICES`). 안 띄우면 `ppe_state` 가 오지 않아 R9·R11 이 UNKNOWN 에 머물고 발생하지 않는다. 이 상태는 health·관제 화면에 따로 표시되지 않으므로(아래 잔여 위험) 아래 항목을 직접 확인한다.
   - 가중치: `simai-bootstrap check --sets ppe` 가 0 으로 끝나는지 본다(`deploy/weights.json`의 `ppe-t1-v4-all-e08`). 통합 이미지는 이 체크포인트를 이미지에 구워 두므로 상태가 `baked` 로 나오고 Pod 에서 받지 않는다(S15P31A101-577). 구운 사본이 없는 이미지에서만 HF 비공개 저장소에서 받으므로, 그때는 읽기 권한이 있는 계정의 `HF_TOKEN` 이 필요하다.
   - 기동: lobby 에 `--services tier0,vllm,worker,avatar,ppe` 를 준다(아바타를 빼지 않는다). lobby 는 `SIMAI_SERVICES` 를 보지 않는다. GPU 예산은 Tier 0 0.20 + 분석 워커 0.70 + PPE 0.05 = 0.95 로 상한 안이다.
   - 기동 로그: `$SIMAI_ROOT/logs/ppe.log` 에 `PPE 시작: stream_epoch=… model=T1-v4-ALL-seed20261021-e08-e373a1048057` 이 있는지 본다. `PPE 시작 실패`(종료 코드 2)면 가중치·sha256·GPU 상한 설정을 먼저 고친다.
   - 추론: 사람이 카메라에 보이는 상태에서 60초마다 남는 `PPE 상태: seq=… counts=…` 의 `inferred` 가 늘어나는지 본다. `seq` 만 늘고 `inferred` 가 그대로면 입력이 막힌 것이다. 같은 줄의 `gate_*`(Tier 0 지연 접수 문), `wait_exceeded`, `metadata_rejected`, 그리고 timing 의 `publication_unavailable`·`read_error` 를 보고 원인을 가린다.
   - 수신: 관제 앱에 로그인한 상태에서 `GET /api/ppe/current` 응답에 이 Tier 0 의 사람별 안전모·조끼 항목이 있는지 본다. 비어 있으면 준비 완료로 보지 않는다. 3D 뷰어의 장비 표시(`PpeGear`)만으로 판단하지 않는다. 미착용이나 상태 없음이면 아무것도 그리지 않아 둘을 구분할 수 없기 때문이다.
6. LIVE에서 같은 World ID·identity_revision의 BOUND·CONNECTED 워치로 안전모 미착용 R9와 조끼 미착용 R11을 각각 발생시켜 “안전모”·“조끼” CAUTION 진동을 확인한다. 한 장비를 다시 착용하면 그 이벤트만 해소되는지, UNKNOWN·만료가 새 발생이나 자동 해소를 만들지 않는지 확인한다. 두 장비를 따로 검사해 높은 등급의 다른 경보가 워치 표현을 가리지 않게 한다.
7. 작업 종료의 NOT_REQUIRED 묶음 APPLIED로 열린 PPE 이벤트가 해소되는지 확인한다. 워치 ACK 자체는 착용·해소 근거가 아니다. REPLAY에서는 실제 워치·현장 알림이 없어야 한다.

## 예외 종료·문제 시 비활성 복구

시드 생성은 `ON CONFLICT DO NOTHING`으로 새 규칙 판을 더한다. **목록에 다시 넣고 시드를 재생성하거나 서버 코드만 되돌려도 이미 삽입된 rule_version 행은 사라지지 않는다.** 구성 묶음도 추가형이므로 이전 APPLIED 묶음은 계속 돈다.

1. 시연을 멈추고 작업을 정상 종료해 NOT_REQUIRED 새 묶음을 APPLIED로 만든다. 이미 열린 PPE 이벤트의 해소를 확인한다(그냥 rules에서 빼는 것으로 해소됐다고 추측하지 않는다).
2. 후속 Jira·MR에서 R9·R11을 `DISABLED_BY_DEFAULT`에 다시 넣고 시드를 재생성한다. 별도 **새 Flyway 마이그레이션**으로 R9·R11 v1의 열린 적용 기간만 `valid_to`로 닫는다. DB·이벤트·기존 규칙 행은 삭제하지 않는다. 기존 Flyway 파일을 고쳐 체크섬을 바꾸지 않는다. 재활성화는 새 버전을 검토한다.
3. 배포 뒤 PPE 두 규칙이 빠진 새 구성 묶음을 발행해 APPLIED와 `/api/rules` 비활성을 확인한다. `RuleSelection.ACTIVE_FOR_PROFILE`은 `valid_to IS NULL`인 판만 고르므로 이 단계까지 해야 비활성 복구가 끝난다.
4. Tier 0까지 이전 판으로 되돌려야 하면 먼저 EC2가 vest를 발행하지 않는 호환 판으로 돌아가고, 호환 문서의 새 묶음 적용을 확인한 뒤 Tier 0를 되돌린다(-549 순서). 묶음 거부 중이면 접속·스키마를 먼저 복구하며 이전 묶음이 멈췄다고 가정하지 않는다.

## 실행한 검증 (2026-11-09)

- `ai/`: `.venv/Scripts/python.exe -m pytest tests/risk tests/config/test_ppe_policy.py -q --tb=short` — **1,176 통과**. 첫 시도의 샌드박스 임시 폴더 생성 오류는 정상 임시 폴더에서 재실행해 해소했다. SIM 벡터·회귀 시험 결과이며 G9·G6 PPE 실측 합격이 아니다.
- `ai/`: `.venv/Scripts/python.exe -m simai_ai.risk.rule_catalog --check ../server/src/main/resources/db/migration/R__rule_versions.sql` — 생성물 일치.
- `front/`: `pnpm --filter admin test src/__tests__/features/events src/__tests__/features/monitor/PpePolicyNotice.test.tsx src/__tests__/features/zones` — **32파일 569 통과**. 이후 R11 상세 해소·요구 없음 사례를 더한 두 파일을 같은 명령의 파일 인자로 재실행해 **2파일 34 통과**(앞 결과와 중복 포함).
- `front/`: `pnpm --filter admin typecheck`, 변경한 TS·TSX 10파일 `eslint` 및 `prettier --check` — 통과.
- `server/`: JDK 21, 일회용 로컬 `simai-ppe-556-test-db`(localhost:55456, 운영과 분리)에서 `gradlew.bat test --tests 'com.ssafy.simai.domain.config.ConfigBundleIntegrationTest' --tests 'com.ssafy.simai.domain.rule.RuleSceneIntegrationTest' --tests 'com.ssafy.simai.domain.watch.WatchAlertIntegrationTest' --console=plain` — **32 통과**(12·8·12, 실패·오류·건너뜀 0). Flyway 전체 적용 후 실제 HTTP·WebSocket 경로로 검사했다.
- 시험 DB 직접 조회에서도 R9·R11 v1 CAUTION·열린 적용 기간, Flyway V45·반복 규칙 시드 성공을 확인했다. 검증 뒤 시험 DB 컨테이너를 종료했다. 실제 워치 하드웨어·실행 중 GPU·운영 서버 검증은 하지 않았다.
- `git diff --check` — 통과. 이 검증 단계에서는 커밋·push·MR·운영 배포를 수행하지 않았다. 후속 커밋·MR은 사용자 요청으로 진행하고, 운영 배포는 별도로 조율한다.

## 대안과 잔여 위험

- 게이트 통과로 표시하는 안은 버렸다. 실측 없는 시연 동작 확인이므로 합격 분모·성능 근거가 없다.
- R9에 조끼를 합치는 안은 버렸다. 한 사람에게도 장비별 발생·해소·워치 단어가 따로 필요하다.
- 최신 config_version만으로 해소 원인을 정하는 안은 버렸다. 작업 종료 전후·거부 묶음과 섞여 착용 해소를 잘못 표시할 수 있다.
- 조끼 근거가 UNKNOWN이면 경보가 안 생길 수 있다. 시연에 맞추려고 UNKNOWN을 미착용으로 바꾸거나 등급을 올리지 않는다. 현장 리허설·동시 GPU 부하는 미확정이다.
- lobby 기본 서비스에 `ppe` 를 넣는 안은 이번에 하지 않았다(S15P31A101-587). 기본에서 뺀 것은 G9 동시 실행 판정 전이라는 -507 결정이고, 바꾸려면 Lead 결정이 필요하다. 그래서 시연 예외 기간에는 배포 절차 5단계에서 명시적으로 띄운다.
- PPE 프로세스가 없거나 입력이 끊긴 상태(완료 표식 쓰기 실패, 접수 문 차단 지속 등)가 health·관제 화면에 표시되지 않는다. health 계약에 PPE 항목이 없고, 서버는 상태별 10초 만료만 보며, 화면의 사람별 PPE 표시는 상태가 없으면 비어 있을 뿐이다. 그래서 운영 중 PPE 가 멈추면 R9·R11 이 조용히 발생하지 않는다. 지금은 PPE 60초 로그로만 알 수 있다. 화면 안내나 health 항목 추가(I1 LIVE 계약, Lead 승인 필요)는 별도 스토리 후보다.
- 요구사항 FR-74 갱신 필요: R11·장비별 해소 및 Lead 시연 예외(정식 게이트는 미통과).
