# 서버 전수 점검에서 나온 작은 결함 (S15P31A101-632)

2026-11-12 에 서버 전체(`server/`, Java 약 35,000줄, Flyway 60개)를 8개 구역으로 나눠 읽고 결함을 찾았다.
그중 정책 판단 없이 고칠 수 있는 작은 결함을 이 스토리에서 고친다. 정책 판단이 필요한 것은 아래 "뺀 것"에 남긴다.

## 고친 것

| 항목 | 원인 | 수정 | 위치 |
|---|---|---|---|
| 주기 작업 스케줄러 | 스레드가 1개(기본값)인데 1초 주기 4개와 10초 주기 I1 송신이 같이 썼다. 송신 하나가 5초 막히면 journal_ack·보류 이벤트 만료·대여 만료가 밀린다 | 풀 4, 오류 처리기(로그), 종료 때 10초 기다림 | `SchedulingConfig` |
| 운영자 확인 재전송 | `rs.getInt` 뒤 `wasNull()` 을 다른 열을 읽은 뒤에 불러 늘 false. NULL 이 `event_sequence: 0`(계약 minimum 1 위반)으로 나갔다 | `rs.getObject(…, Integer.class)` | `EventAckForwarder.resendPending` |
| 장면 재생성 동시 요청 | 유일 색인 위반을 catch 한 뒤 같은 트랜잭션에서 조회 → PostgreSQL 이 트랜잭션을 중단해 409 대신 500 | `INSERT … ON CONFLICT DO NOTHING RETURNING id`, 빈 결과면 먼저 들어간 요청을 읽어 409 | `SceneRegenerateService.requestRegenerate` |
| 현장 ID 변경 | 의존 표 고정 목록에 `report`(V36)·`floor_coverage`(V52) 등 site FK 표가 빠져 FK 위반 500 | 고정 목록 + 카탈로그(`pg_constraint`)에서 site 를 가리키는 FK 표를 읽어 함께 검사 | `SiteController.dependentColumns` |
| CCTV 시청자 등록 | 목록에 넣은 뒤 세션 속성을 저장해, 그 사이 닫힌 연결이 목록에 영구히 남았다(슬롯 누수, demand 가 꺼지지 않아 쿼터 소모). pong 타임아웃도 목록에서 빼지 않았다 | 속성을 먼저 두고, 잠금 안에서 이미 닫힌 시청자는 받지 않는다. pong 타임아웃 때 바로 목록에서 뺀다. `source` 는 volatile | `CctvRemoteHub`, `CctvViewer.isClosed` |
| 로그인 | BCrypt 비교(약 100 ms)를 트랜잭션 안에서 해 DB 연결을 쥐었다. 로그인이 몰리면 풀이 말라 Tier 0 수신까지 멈춘다 | `login` 의 `@Transactional` 제거(조회·토큰 발급은 각자 짧은 트랜잭션) | `AuthService.login` |
| 세팅 Safe 래퍼 | 하트비트·작업 제출 트랜잭션 안의 리스너가 SQL 오류를 삼키기만 했다. PostgreSQL 트랜잭션이 중단돼 바깥 커밋이 롤백된다. 쓰려고 만든 `nested`(PROPAGATION_NESTED) 템플릿은 쓰이지 않았고, 써도 HibernateJpaDialect 가 savepoint 를 지원하지 않아 실패한다 | 같은 JDBC 연결에서 SQL `SAVEPOINT`·`ROLLBACK TO SAVEPOINT` 로 감싼다(`withSavepoint`, 바깥 트랜잭션이 없으면 그냥 실행). 쓰지 않던 `nested` 는 지웠다. 구역 초안 항목은 V34 제약(길이·목록·유한한 수, 빈 문자열은 DB 처럼 허용)을 먼저 보고 어긴 항목만 뺀다 | `SetupService.enqueueZoneDraftIfReadySafe`·`handleZoneDraftFinishedSafe`·`draftProblem` |
| 스냅샷 원소 계약 위반 | 활성 이벤트 하나의 `event_id` 가 틀리면 트랜잭션 밖에서 던져 동기화 모드가 남고(이후 전이가 모두 AUDIT_ONLY), 트랜잭션 안에서 던지면 연결을 닫아 같은 스냅샷으로 끊고 붙기를 되풀이했다 | 분류 전에 원소마다 필수 필드를 보고 어긴 원소만 빼고(경고 로그) 동기화를 끝낸다 | `EventStateSnapshotIngestor.ingest` |
| 전이 필수 필드 | `severity`·`confidence`·`verification`·`needs_check`·`world_ids`·`rule_version` 을 null 허용으로 읽어 DB NOT NULL 위반(저장 실패)이 났다. 계약 위반이 아니라 실패로 집계돼 offset 이 확인되지 않고 같은 전이를 끝없이 다시 받았다 | `RiskEventRepository.requireStateFields` 로 먼저 보고 `InvalidI1Message` | `RiskEventRepository.recordTransition`·`ensureFromSnapshot` |
| gateway 없는 이벤트 상향 | 종료는 `CLOSE_NO_GATEWAY` 로 막는데 상향 승인은 저장했다. 재전송이 gateway_id 로 고르므로 영영 닿지 않는다 | 승인(APPROVED) 때 409 `ESCALATION_NO_GATEWAY` | `EventEscalationService.decide`, `ErrorCode.Event` |
| TBM 양식 수정 잠금 순서 | report 만 잠그고 work 를 나중에 바꿔, work → report 순으로 잠그는 서명·제출·AI 초안과 교착이 날 수 있었다 | work 행을 먼저 따로 `FOR UPDATE` 한 뒤 report 를 잠근다(한 문장 `FOR UPDATE OF w, r` 은 순서를 플랜이 정해 보장하지 못한다) | `TbmReportRepository.updateForm` |
| TBM 비동기 마감 | 생성 실패 기록·작업 마무리·AI 초안 실패·종료 미팅 기록이 한 번 실패하면 GENERATING 으로 남아 재시작 전까지 409. 실행 거부도 처리하지 않았다 | `AsyncFinish.withRetry`(2회, 일일 일지와 같은 규칙), 실행 거부 때 INTERRUPTED 로 마감, `Error` 도 잡는다 | `TbmReportService`, `TbmFormAiService`, `AsyncFinish` |
| TBM 대체 마감 문장 | `Map.of` 순회라 초반·중반·후반 순서가 실행마다 달랐다 | 순서 고정 | `TbmReportService.wrapUpFallback` |
| 워치 격상 유실 | `escalated_at` 을 먼저 커밋하고 발행했다. 발행이 실패하면 다시 격상되지 않았다 | 발행 실패 때 표시를 되돌려 다음 주기에 다시 격상 | `WatchAlertService.escalate` |
| 구성 묶음 obb | 손상된 static_object 행 하나가 NPE 로 모든 발행을 500 으로 막았다 | 어느 작업면인지 밝히는 오류 | `ConfigBundleService.obbAxis` |
| 보관 I1 메시지 재시도 | 처리할 수집기가 없는 msg_type 이면 NPE 로 매초 영원히 다시 시도 | 경고 뒤 버린다 | `ClipDeferredRetrier.retry` |
| JWT 형식 | 컨트롤러 30여 곳이 `Long.parseLong(sub)`·`Role.valueOf(role)` 로 바로 읽어 형식이 틀리면 500 | 해독기 검증에서 sub(1 이상 정수)·role(알려진 값)을 보고 401 | `JwtConfig.validateClaims` |
| Tier0Relay 대기열 거부 | 걸지 못한 world_state 항목이 남아 그 Tier 0 의 다음 world_state 가 영영 발행되지 않았다 | 거부되면 항목을 지운다 | `Tier0Relay.onWorldState` |
| 표준 MVC 오류의 code | 본문 검증 실패·읽을 수 없는 본문·없는 경로 응답에 `code` 가 없었다 | `ResponseEntityExceptionHandler` 를 이어받아 HTTP 상태 이름을 code 로 싣는다 | `GlobalExceptionHandler` |
| 아바타 자산 서명 | 워커가 보낸 `object_uri` 의 버킷·접두사를 보지 않고 키만 우리 버킷에서 서명했다(VIEWER 가 아바타 주소로 클립 등 다른 객체를 받을 수 있음) | 업로드 서명이 내주는 꼴(`s3://<버킷>/avatars/…`, `..` 없음)만 서명 | `AvatarController.download` |
| 색인 | refresh_token(user_id, expires_at), event_ack·event_close(event_id), job_lease(만료, ACTIVE), config_bundle(site_id, issued_at DESC) 이 없었다 | V59 | `V59__lookup_indexes.sql` |
| 만료 갱신 토큰 | 회전마다 행이 생기고 지우는 곳이 없었다 | 만료 7일 뒤 행을 6시간마다 지운다 | `RefreshTokenCleaner`, `RefreshTokenRepository.purgeExpiredBefore` |
| open-in-view | 기본 true 라 요청 끝까지 영속성 문맥을 열어 두었다 | false(엔티티 두 개, 지연 연관 없음) | `application.yaml` |

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 스케줄러 풀 크기 | 4 | 1초 주기 작업 4개(journal_ack, 보류 만료, 대여 만료, Scene 참조)가 송신 하나에 묶이지 않을 만큼. 같은 작업끼리는 fixedDelay 라 겹치지 않는다 |
| 마감 재시도 | 2회 | `DailyReportService.fail` 과 같다. 그래도 안 되면 기동 때 복구(INTERRUPTED·결정적 문장) |
| 만료 갱신 토큰 보관 | 만료 뒤 7일, 6시간마다 | 만료 토큰은 어차피 거부된다. 문의 대응 여유 |
| 표준 MVC 오류 code | HTTP 상태 이름(`BAD_REQUEST` 등) | ErrorCode 는 도메인 오류용이라 새 상수를 만들지 않았다 |
| 스냅샷에서 뺀 원소 | 분류에서 빼고 경고만 남긴다 | EC2 는 전이를 만들지 않는다. 다음 전이·스냅샷에서 맞춘다 |

## 확인 뒤 고치지 않은 것

- `I1Json.integer` 의 소수 반올림: 처음에는 소수를 거부했으나, 같은 함수를 쓰는 `pipeline_lag_ms`·`frame_age_ms` 는 계약(`duration_ms`)이 소수를 허용해 health·world_state 가 통째로 버려질 수 있어 되돌렸다(리뷰 지적).

- 이벤트 조회의 `rule_version` NULL → 0: `risk_event.rule_version` 은 NOT NULL(V1)이라 NULL 이 들어갈 수 없다. 이번에 전이·스냅샷에서 필수로 검사하므로 더 막혔다.
- `media_clip(event_id)` 색인: `UNIQUE (event_id, resume_count, camera_id)` 의 앞 열이라 이미 있다.

## 남은 위험

- 아바타 자산: 서버는 이제 `s3://<버킷>/avatars/…` 꼴만 서명한다. 워커는 업로드 서명이 돌려준 `object_uri` 를 그대로 싣는다(`ai/services/avatar/transport.py`). 2026-11-12 운영 DB 확인: `avatar_asset` 주소 466개가 모두 `s3://simai-k15a101-media/avatars/` 아래라 404 가 될 행은 없다. `SIMAI_STORAGE_BUCKET` 을 바꾸면 예전 행은 404 가 된다.
- 워치 격상은 발행 실패 때 다시 격상하므로 최소 한 번(at-least-once)이다. 일부 구독자에게 이미 나갔다면 같은 `alert_msg_id` 가 두 번 올 수 있다. 실패 원인이 SEND_FAILED 였다면 다음에는 NO_ACK 로 격상된다.
- 장면 재생성: 충돌 뒤 조회 사이에 먼저 들어간 요청이 끝나는 경우를 한 번 더 넣어 보고, 그래도 엇갈리면 409(request_id 0)로 답한다.
- 마감 재시도는 대기 없이 2회라 DB 가 몇 초 끊기면 기동 때 복구에 맡겨진다.
- V59 번호: 병합 직전 dev 의 마지막 Flyway 번호를 다시 확인한다.

## 뺀 것 (정책 판단이 필요하거나 큼)

- 결재(`approved_at`) 뒤 양식·AI 초안·종료 미팅 수정 차단, 서명 해시 검증 — 서명·제출 흐름(-611) 의도와 맞춰야 한다.
- 비‑daily 작업 수정·삭제의 상태 가드.
- `world_track_state`·`skeleton_3d` 덮어쓰기(규칙 6 과의 관계, 명세 11.4 예외 여부).
- LIVE 중 배치 작업 대여를 서버에서도 막을지.
- 로그인 시도 제한, 열린 WebSocket·STOMP 세션의 토큰 폐기 반영, 구성 묶음 파일 URL 만료·키 분리.
- 비클립 업로드 객체(EVENT_FRAME 등) 보존·정리 정책.
- 버전 표의 "현재 하나" 부분 유일 색인(기존 데이터 확인이 먼저).
- 세팅 세션 생성 때 `active_setup_session_id` 를 먼저 덮는 문제, 세팅 취소·하트비트 잠금 순서, static_reconstruct 무효 결과의 재실행, 아바타 음성 캐시.
- 구조 정리(SetupService 1,600줄, 컨트롤러 안 SQL, 중복 코드).

## 시험

- 새 시험
  - `JwtClaimsValidatorTest`: sub(계정 번호)·role(알려진 값) 검증
  - `ZoneDraftProblemTest`: 구역 초안 V34 제약(길이·목록·유한한 수·폴리곤)
  - `WrapUpFallbackOrderTest`: 대체 마감 문장의 단계 순서 고정
  - `SceneRegenerateIntegrationTest` 동시 요청 4개 → 202 하나, 나머지 409(500 없음)
  - `SiteSceneIntegrationTest` 일일 일지(report)만 붙은 현장의 site_id 변경 → 409 `SITE_IN_USE`
  - `RefreshTokenIntegrationTest` 만료 7일 지난 갱신 토큰 행만 지움
  - `AuthIntegrationTest` 읽을 수 없는 본문 → 400 `code=BAD_REQUEST`
- 바뀐 전제를 반영한 기존 시험
  - `UploadSignIntegrationTest`: sub 가 숫자가 아닌 토큰은 403 이 아니라 401(해독기에서 거절)
  - `AvatarRelayIntegrationTest`: 계약 예시의 `s3://simai-avatars/…` 를 시험 버킷의 `avatars/` 아래로 바꿔 보낸다(서버는 업로드 서명이 내준 꼴만 서명)
  - 세팅 시험(`SetupZoneDraftIntegrationTest`·`SetupResultRegisterIntegrationTest`)이 savepoint 경로를 지난다
- 전체: `./gradlew build`(임시 DB `simai_test_632`) 762개 통과
- 시험을 따로 만들지 않은 것: ack 재전송 NULL, 스냅샷 원소 제외, CCTV 시청자 경쟁, 워치 격상 되돌림, Tier0Relay 거부, 마감 재시도. 코드로 확인했고 기존 통합 시험이 정상 경로를 지난다
