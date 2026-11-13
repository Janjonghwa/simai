# simAI DB 검증 (ERD 4단계)

S15P31A101-210. [`db-erd.md`](db-erd.md)·[`db-tables.md`](db-tables.md) 의 테이블이 API 명세 표 v11(노션, 166행)과 기획서 11.4(리플레이 질의)·11.5(보관)·12.6(REST)을 받쳐 주는지 확인한다.

## 요약

- API 표 166행을 모두 테이블에 대응시켰다. DB 를 읽거나 쓰는 행 115개, DB 를 쓰지 않는 행(Tier 0·노트북 내부 처리 등) 51개.
- 12.6 과 대조해 API 표에 없던 행 3개를 찾아 v11 에 더했다: `POST /api/events/{id}/close`(이벤트 운영자 종료), Tier 0 의 운영자 종료 적용, `GET /api/scale-references`(4장). 이 행이 없으면 `event_close` 를 쓰는 곳이 없었다.
- 쓰는 API 가 없는 테이블 8개(`app_user`, `site`, `camera`, `rule_version`, `equipment_asset`, `rag_clause`, `operator_qa`, `watch_registrant`)와 행을 만드는 경로가 없는 `device`·`worker` 는 API 를 늘리지 않고 Flyway 시드와 EC2 관리 명령으로 채운다(3장, 결정).
- 읽는 API 가 없는 테이블: `site`(MVP), `camera`(MVP), `sim.placement_recommendation`(확장 4), `operator_qa`(확장 3), `watch_registrant`(확장), `watch_impact`(확장), `ppe_state`(확장), `region_state`(확장). 확장 테이블은 확장 착수 때 화면·경로를 정하고, `site`·`camera` 는 다른 API 안에서 함께 읽는다(3장).
- 쓰는 주체가 둘 이상인 테이블 19개. 모두 컬럼이나 상태 단계가 나뉘어 있다(2장 메모).
- 리플레이 질의 Q1~Q4 가 쓰는 컬럼은 모두 있다. T 시점 이벤트 상태는 `risk_event` 가 아니라 `risk_event_update` 로 만든다(Q2).
- 보관: 클립 14일 삭제는 EC2 삭제 작업만 쓰고 객체 저장소 수명 주기 규칙은 클립에 쓰지 않는다(결정, 기획서 11.5 반영). hypertable 은 EC2 디스크 320 GB 기준으로 좌표·골격·health 는 상시 삭제 없이 7일 뒤 압축, 운영 지표는 30일 보관으로 정했다(6장).

## 1. 방법

- API 표 행마다 읽는 테이블, 쓰는 테이블, 쓰는 주체를 정했다(부록). Tier 0·분석 워커·노트북 안에서 끝나는 처리는 DB 를 쓰지 않고, Tier 0·워커가 보낸 메시지를 EC2 가 받아 쓰는 경우 주체를 "Tier 0"·"워커 결과"로 적었다.
- 12.6 REST 표의 (메서드, 경로)와 API 표를 맞대어 빠진 행을 찾았다. 부록 C 확장 경로와 12.5 워커 경로는 12.6 표 밖이라 비교에서 뺐다.
- 질의·출처·보관에 쓰는 컬럼이 `db-tables.md` 에 있는지 생성 스크립트가 검사한다.

## 2. 테이블별 쓰기·읽기

| 테이블 | 단계 | 쓰는 주체(행) | 읽는 행 수 | 판정 |
|---|---|---|---|---|
| `app_user` | MVP | — | 4 | 쓰는 API 없음 |
| `refresh_token` | MVP | EC2: 로그인 | 2 | 맞음 |
| `device` | MVP | 기기: 노트북 하트비트 수신 | 2 | 맞음 |
| `device_heartbeat` | MVP | 기기: 노트북 하트비트 수신 | 4 | 맞음 |
| `worker` | MVP | 워커: 분석 작업 대여 | 2 | 맞음 |
| `site` | MVP | — | 0 | 쓰는 API 없음, 읽는 API 없음 |
| `camera` | MVP | — | 0 | 쓰는 API 없음, 읽는 API 없음 |
| `zone` | MVP | 관리자: 구역 수정(새 버전 추가), 입체 구역 생성 | 7 | 맞음 |
| `static_object` | MVP | 관리자: 정적 객체·작업면 등록, 정적 객체·작업면 새 버전 | 4 | 맞음 |
| `rule_version` | MVP | — | 5 | 쓰는 API 없음 |
| `config_bundle` | MVP | Tier 0: 구성 묶음 동기화<br>관리자: 구성 묶음 발행 | 3 | 쓰는 주체 2 |
| `config_bundle_item` | MVP | 관리자: 구성 묶음 발행 | 1 | 맞음 |
| `config_ack` | MVP | Tier 0: 구성 묶음 동기화 | 3 | 맞음 |
| `tier0_endpoint` | MVP | Tier 0: Tier 0 엔드포인트 등록 | 1 | 맞음 |
| `layout_plan` | M3 | 관리자: 가상 레이아웃 생성, 가상 레이아웃 수정 | 6 | 맞음 |
| `equipment_asset` | 확장 2 | — | 2 | 쓰는 API 없음 |
| `calibration_version` | MVP | Tier 0: I1 `calibration_update`(상태·버전 변화, 4.4·4.5). 표에서는 "자동 보정 계산" 행의 계약에만 적혀 있다<br>관리자: 보정 결과 확정<br>워커 결과: 자동 보정 계산 | 7 | 쓰는 주체 3 |
| `calibration_camera` | MVP | 워커 결과: 자동 보정 계산 | 1 | 맞음 |
| `calibration_run` | M1 | 관리자: 자동 보정 시작<br>워커 결과: 자동 보정 계산 | 1 | 쓰는 주체 2 |
| `calibration_verification` | M1 | 관리자: 보정 검증 | 0 | 맞음 (기록용: 보정 확정 근거(4.5). 확정 응답에 요약이 실린다) |
| `scale_reference` | M1 | 관리자: 실측 정물 등록 | 2 | 맞음 |
| `calibration_scale_check` | M1 | 워커 결과: 자동 보정 계산 | 0 | 맞음 (기록용: 보정 결과 근거(4.3)) |
| `coverage_guide` | MVP | 워커 결과: 자동 보정 계산 | 1 | 맞음 |
| `floor_plan` | MVP | 워커 결과: BEV 평면도 생성 | 1 | 맞음 |
| `scene_version` | MVP | 관리자: 정적 3D 생성 요청<br>워커 결과: 정적 3D 복원, 정적 공간 갱신 | 5 | 쓰는 주체 2 |
| `world_track_state` | MVP | Tier 0: World 상태 송신 | 4 | 맞음 |
| `skeleton_3d` | MVP | Tier 0: World 상태 송신 | 1 | 맞음 |
| `system_health` | MVP | Tier 0: Health 송신 | 5 | 맞음 |
| `system_metric` | MVP | Tier 0: Health 송신 | 1 | 맞음 |
| `observation_summary` | M1 | Tier 0: 2D 관측 요약 송신 | 0 | 맞음 (기록용: 보정 작업 입력(`calibration_run.obs2d_archive_uri`)을 EC2 가 고를 때 읽는다) |
| `risk_event` | MVP | Tier 0: EC2 복구 재동기화, 운영자 상향 적용, 운영자 종료 적용, 이벤트 저널·outbox 기록, 이벤트 전이 전송·수신 | 18 | 맞음 |
| `risk_event_update` | MVP | Tier 0: 운영자 상향 적용, 운영자 종료 적용, 이벤트 저널·outbox 기록, 이벤트 전이 전송·수신 | 4 | 맞음 |
| `event_ack` | MVP | 반장·관리자: 경보 확인(ACK) | 1 | 맞음 |
| `event_close` | MVP | Tier 0: 운영자 종료 적용<br>관리자: 이벤트 운영자 종료 | 1 | 쓰는 주체 2 |
| `event_escalation` | M3 | Tier 0: 운영자 상향 적용<br>관리자: 상향 의견 적용 확인 | 0 | 쓰는 주체 2 (기록용: 상향 근거. 이벤트 상세에 함께 보일 수 있다) |
| `journal_cursor` | MVP | Tier 0: EC2 복구 재동기화, 이벤트 저널·outbox 기록, 이벤트 전이 전송·수신 | 2 | 맞음 |
| `event_frame` | M3 | Tier 0: 이벤트 프레임 업로드 완료 통지 | 3 | 맞음 |
| `action_ticket` | M2 | EC2: 조치 티켓 자동 생성(조치 불일치 판정은 취소, S15P31A101-411)<br>반장: 조치 티켓 상태 변경 | 5 | 쓰는 주체 2 |
| `action_ticket_history` | M2 | 반장: 조치 티켓 상태 변경 | 0 | 맞음 (기록용: 티켓 이력. 상세 조회(`GET /api/tickets/{id}`, 12.6)에서 읽는다) |
| `media_clip` | MVP | EC2: 보관 기간 자동 삭제<br>Tier 0: 이벤트 클립 저장<br>관리자: 클립 보존 표시 변경 | 3 | 쓰는 주체 3 |
| `media_clip_retention` | MVP | Tier 0: 이벤트 클립 저장<br>관리자: 클립 보존 표시 변경 | 0 | 쓰는 주체 2 (기록용: 보존 표시 이력(11.5)) |
| `clip_access_log` | MVP | 관리자: 이벤트 클립 조회 | 0 | 맞음 (기록용: 열람 기록(14장). 감사용) |
| `upload_grant` | MVP | Tier 0: 이벤트 클립 저장, 이벤트 프레임 업로드 완료 통지<br>기기·반장: 임시 서명 업로드 주소 발급 | 2 | 쓰는 주체 2 |
| `notification` | M1 | EC2: 워치 진동 알림 발송, 위험 알림 Web Push 발송<br>반장: 푸시 수신 확인 | 2 | 쓰는 주체 2 |
| `push_subscription` | M1 | 반장: 알림 구독 등록, 알림 구독 해지 | 4 | 맞음 |
| `test_push` | M1 | 관리자: 시험 푸시<br>반장: 푸시 수신 확인 | 1 | 쓰는 주체 2 |
| `push_receipt` | M1 | 반장: 푸시 수신 확인 | 0 | 맞음 (기록용: 수신 확인 원본. `notification.receipt_at` 으로 요약) |
| `analysis_job` | MVP | EC2: 분석 작업 등록·재할당<br>관리자: TBM 보고서 초안 생성, 사고 구간 요약 요청, 일일 일지 생성 요청, 자동 보정 시작, 정적 3D 생성 요청<br>워커: 분석 작업 결과 제출, 분석 작업 대여<br>워커 결과: BEV 평면도 생성, 사고 구간 요약, 외부 LLM 텍스트 생성, 이벤트 근거 설명 생성, 자동 보정 계산, 작업중지 명령서 초안 생성, 정적 3D 복원 | 2 | 쓰는 주체 4 |
| `job_lease` | MVP | EC2: 분석 작업 등록·재할당<br>워커: 분석 작업 결과 제출, 분석 작업 대여, 분석 작업 대여 연장 | 3 | 쓰는 주체 2 |
| `vlm_assessment` | M3 | 관리자: 상향 의견 적용 확인<br>워커 결과: 사고 구간 요약, 심각도 상향 의견 처리, 이벤트 근거 설명 생성 | 2 | 쓰는 주체 2 |
| `report` | M3 | 관리자: TBM 보고서 초안 생성, 일일 일지 생성 요청<br>워커 결과: 외부 LLM 텍스트 생성 | 1 | 쓰는 주체 2 |
| `rag_clause` | M3 | — | 2 | 쓰는 API 없음 |
| `sim.evaluation_run` | M3 | Risk Engine: What-if 판정, 계획 모드 판정, 시뮬레이션 판정<br>관리자: TBM 보고서 초안 생성, What-if 분석 요청, 계획 모드 평가 요청, 배치 추천 요청, 위험 동선 시뮬레이션 요청 | 4 | 쓰는 주체 2 |
| `sim.risk_result` | M3 | Risk Engine: What-if 판정, 계획 모드 판정, 시뮬레이션 판정 | 4 | 맞음 |
| `sim.trajectory_plan` | 확장 2 | 관리자: 위험 동선 시뮬레이션 요청 | 1 | 맞음 |
| `sim.what_if_scenario` | 확장 4 | 관리자: What-if 분석 요청 | 1 | 맞음 |
| `sim.visibility_result` | 확장 4 | Risk Engine: What-if 판정 | 2 | 맞음 |
| `sim.placement_recommendation` | 확장 4 | 관리자: 배치 추천 요청 | 0 | 읽는 API 없음 |
| `agentic_report` | 확장 3 | 관리자: 작업중지 명령서 승인·발행<br>워커 결과: 작업중지 명령서 초안 생성 | 2 | 쓰는 주체 2 |
| `operator_qa` | 확장 3 | — | 0 | 쓰는 API 없음, 읽는 API 없음 |
| `work_schedule` | 확장 5 | 관리자: 공정표 등록 | 1 | 맞음 |
| `watch_registrant` | 확장 | — | 0 | 쓰는 API 없음, 읽는 API 없음 |
| `watch_binding` | 확장 | Tier 0: 워치-사람 움직임 대조 연결 | 2 | 맞음 |
| `channel_state` | 확장 | EC2: 워치 채널 상태 관리 | 1 | 맞음 |
| `watch_feature` | 확장 | 워치: 워치 가속도 전송 | 1 | 맞음 |
| `watch_alert` | 확장 | EC2: 워치 미확인 격상, 워치 진동 알림 발송<br>워치: 워치 수신 확인 | 2 | 쓰는 주체 2 |
| `watch_impact` | 확장 | 워치: 워치 충격 이벤트 전송 | 0 | 읽는 API 없음 |
| `ppe_state` | 확장 | Tier 0: PPE 검출 | 0 | 읽는 API 없음 |
| `region_state` | 확장 | 워커 결과: 정적 공간 갱신 | 0 | 읽는 API 없음 |

**쓰는 주체가 둘 이상인 테이블.** 컬럼이나 단계가 나뉘어 충돌하지 않는다.

| 테이블 | 나뉘는 방식 |
|---|---|
| `calibration_version` | 워커 결과가 후보를 만들고, 관리자가 확정(`state`), Tier 0 가 운영 중 상태 변화(`calibration_update`)를 보낸다. 상태 전이 규칙은 4.4·4.5 |
| `config_bundle` | 관리자가 발행, Tier 0 응답으로 `applied_at` 만 채운다 |
| `calibration_run`·`scene_version`·`vlm_assessment`·`report`·`agentic_report` | 사람이 요청 행을 만들고 워커 결과가 결과 컬럼을 채운다. `vlm_assessment.decision` 은 관리자만 |
| `analysis_job`·`job_lease` | EC2 가 등록·재할당, 워커가 대여·연장·결과. 상태 전이는 12.5 |
| `media_clip`·`media_clip_retention` | Tier 0 가 기록·CRITICAL 자동 보존, 관리자가 보존 표시, EC2 가 삭제 확인(`deleted_at`) |
| `event_escalation`·`event_close` | 관리자가 명령 행, Tier 0 결과(`result`·`event_sequence`·`decided_at`) |
| `action_ticket` | EC2 가 자동 생성(불일치 표시는 취소, S15P31A101-411), 반장이 상태 변경 |
| `notification`·`test_push`·`upload_grant`·`watch_alert` | 발송·발급 주체가 행을 만들고 수신 확인·사용 시각만 다른 주체가 채운다 |

## 3. 쓰는 API 가 없는 테이블 (2026-10-21 결정)

이 테이블은 API 표와 12.6 에 쓰는 경로가 없다. 사용자 화면이 필요 없는 초기 데이터와 관리 작업이라 API 를 늘리지 않고 아래처럼 채운다(기획서 16장). 시드와 관리 명령은 `V1__init.sql` 이슈에서 함께 만든다.

| 테이블 | 단계 | 채우는 방법 |
|---|---|---|
| `app_user` | MVP | EC2 관리 명령: 계정 생성·역할 지정·비활성. 계정 관리 API 는 두지 않는다 |
| `site` | MVP | Flyway V2 시드: `classroom-1`(교실 테스트베드, 계약 예시 `coordinate_frame.site_id` 와 같음). 좌표계 관련 컬럼은 보정 확정 때 갱신 |
| `camera` | MVP | Flyway 시드(V2·V69): `cam_A`~`cam_D`(Tapo C110, 25 fps, 3.3). 계약 예시·코드와 같은 ID 이고 설치 위치는 넣지 않는다. 해상도는 `codec_info` 로 채우고 RTSP 주소·계정은 DB 밖(14장) |
| `rule_version` | MVP | ai 규칙 등록부에서 자동 생성한 Flyway 반복 마이그레이션 `R__rule_versions.sql`(`python -m simai_ai.risk.rule_catalog --write`). 새 버전만 더하고, 같은 버전의 값이 바뀌면 첫 운영 배포 뒤에만 멈춘다(그 전은 갱신, docs/design/rule-seed-preop-exception.md). 등록부와 파일이 어긋나면 ai 테스트가 실패한다. 수정 API 는 없다 |
| `equipment_asset` | 확장 2 | 확장 2 착수 때 정한다(적재 스크립트 후보) |
| `rag_clause` | M3 | 적재 스크립트(M3, FR-20). bge-m3 revision 을 고정한 뒤 조항을 넣는다 |
| `operator_qa` | 확장 3 | 확장 3(C.3) 착수 때 정한다. API 표에 행이 없다 |
| `watch_registrant` | 확장 | 확장(C.6) 착수 때 정한다. API 표에 행이 없다 |
| `device` | MVP | EC2 관리 명령: 기기 등록, 토큰 발급·회전·폐기(14장). 토큰 원문은 발급 때 한 번만 보여 주고 DB 에는 해시만. 하트비트는 `last_heartbeat_at` 만 고친다 |
| `worker` | MVP | EC2 관리 명령: 워커 토큰 발급·폐기(12.5). 대여는 `last_seen_at` 만 고친다 |

`site`·`camera` 는 읽는 API 도 없다. 현장·카메라 정보는 구성 묶음, 보정 상태, 설치 안내 응답 안에 함께 싣는 것으로 보고, 카메라 목록 화면이 필요해지면 `GET /api/cameras` 를 12.6 에 더한다.

## 4. API 표에 더한 행 (v11)

12.6 에 있는데 API 표 v10 에 없던 행이다. 운영자 종료는 상향 적용과 같은 짝(EC2 REST 행 + Tier 0 적용 행)으로 넣었다.

| 기능 ID | API 이름 | 경로 | 읽기 | 쓰기 | 쓰는 주체 | 메모 |
|---|---|---|---|---|---|---|
| FR-78 | 운영자 종료 적용 | `INTERNAL` | — | `event_close`, `risk_event_update`, `risk_event` | Tier 0 | event_close_result, RESOLVED 전이 |
| FR-78 | 이벤트 운영자 종료 | `POST /api/events/{id}/close` | `risk_event`, `event_close` | `event_close` | 관리자 | 멱등 키는 event_close.idempotency_key. 결과 컬럼은 운영자 종료 적용 행(Tier 0) |
| FR-135 | 실측 정물 목록 조회 | `GET /api/scale-references` | `scale_reference` | — | — |  |

- 운영자 종료의 요구사항은 FR-78 이다. 기획서 README 의 "요구사항 FR 갱신 필요" 목록에 이미 올라 있어(S15P31A101-200) 요구사항 명세서를 맞출 때 함께 반영된다.
- **멱등 키(2026-10-21 결정).** REST 요청이 보내는 멱등 키는 `event_close.idempotency_key`(유일)에 따로 저장하고, `close_id` 는 EC2 가 발급하는 고유 ID(PK)로 둔다. 같은 멱등 키로 다시 오면 새 행을 만들지 않고 처음 결과를 돌려준다. I1 `event_close` 는 `close_id` 를 계약의 멱등 키로 쓰므로 계약은 바뀌지 않는다(기획서 12.6 반영).

## 5. 리플레이 질의 (11.4)

### Q1. 시각 T 의 World 상태

GET /api/world?at={t} (M3, 11.4 최소 리플레이).

```sql
SELECT DISTINCT ON (w.world_id) w.*
FROM world_track_state w
WHERE w.observation_at > :t - interval '1 second' AND w.observation_at <= :t
ORDER BY w.world_id, w.observation_at DESC;
```

10 Hz 라 1초 창이면 T 직전 상태가 잡힌다. 창 안에 없는 World ID 는 T 에 보이지 않은 것으로 본다(마지막 상태를 끌어오지 않는다, 2장 "모르면 비운다").

### Q2. 시각 T 에 활성인 이벤트

GET /api/world?at={t}, GET /api/replay (M3).

```sql
SELECT * FROM (
  SELECT DISTINCT ON (u.event_id)
         u.event_id, u.transition, u.severity, u.verification, u.zone_id, u.world_ids, u.rule_id, u.rule_version
  FROM risk_event_update u
  WHERE u.observation_at <= :t
    AND u.transition IN ('RAISED', 'UPDATED', 'RESOLVED')
  ORDER BY u.event_id, u.event_sequence DESC
) s
WHERE s.transition <> 'RESOLVED';
```

`risk_event` 는 지금 상태만 있어 T 의 상태를 알 수 없다. 전이 기록 `risk_event_update` 에서 T 이전 마지막 생명주기 전이를 고른다. `ACKNOWLEDGED` 는 생명주기를 바꾸지 않으므로 뺀다. 시간 축은 World 상태와 같은 `observation_at`(촬영 기준)이다.

### Q3. 구간 [t1, t2]

GET /api/replay?from={t1}&to={t2} (M3).

```sql
SELECT * FROM world_track_state
WHERE observation_at BETWEEN :t1 AND :t2 ORDER BY observation_at;

SELECT * FROM risk_event_update
WHERE observation_at BETWEEN :t1 AND :t2 ORDER BY observation_at, event_id, event_sequence;
-- t1 에 이미 활성이던 이벤트는 Q2(:t = :t1) 로 시작 상태를 만든다
```

구간 시작 전에 열린 이벤트를 놓치지 않으려면 Q2 로 시작 상태를 먼저 만든다.

### Q4. 시각 T 에 유효한 구성(리플레이 확장, 부록 C.1)

확장.

```sql
SELECT * FROM zone
WHERE valid_from <= :t AND (valid_to IS NULL OR valid_to > :t);
SELECT * FROM rule_version
WHERE valid_from <= :t AND (valid_to IS NULL OR valid_to > :t);
SELECT * FROM scene_version
WHERE state = 'COMMITTED' AND valid_from <= :t AND (valid_to IS NULL OR valid_to > :t);
SELECT * FROM config_bundle
WHERE applied_at <= :t ORDER BY applied_at DESC LIMIT 1;
SELECT DISTINCT ON (s.world_id) s.* FROM skeleton_3d s
WHERE s.observation_at > :t - interval '1 second' AND s.observation_at <= :t
ORDER BY s.world_id, s.observation_at DESC;
```

시간으로 고른 버전은 전이·World 상태에 적힌 버전(`risk_event_update.config_version`·`calibration_version`·`scene_version`, `world_track_state.scene_version`)과 같아야 한다. 다르면 기록된 버전을 정본으로 보여 주고 차이를 표시한다(11.4-2).

**V1 인덱스(제안).** `world_track_state (world_id, observation_at DESC)`, `skeleton_3d (world_id, observation_at DESC)`, `risk_event_update (observation_at)`, `risk_event_update (event_id, event_sequence)`(PK), 버전 테이블 `(valid_from, valid_to)`. hypertable 은 시간 인덱스를 기본으로 만든다.

**REPLAY 출처.** `risk_event.source` 는 LIVE·REPLAY 를 받지만 `risk_event_update.source` 는 LIVE 만이고, API 표에는 REPLAY 이벤트를 만드는 행이 없다. 리플레이 재판정(부록 C.1)을 넣을 때 REPLAY 전이를 어디에 쌓을지 정한다. MVP 에서 REPLAY 값은 자리만 있다.

## 6. 보관과 파기 (11.5)

| 데이터 | 보관 | 테이블·컬럼 | 방법 | 판정 |
|---|---|---|---|---|
| 이벤트 클립 | 14일, 보존 표시분은 프로젝트 종료까지 | `media_clip.clip_created_at`·`delete_after`(14일)·`retention_hold`·`deleted_at`, 이력 `media_clip_retention` | EC2 "보관 기간 자동 삭제"가 `delete_after < now()` 이고 보존 표시가 없는 클립의 객체를 지우고 `deleted_at` 을 쓴다 | 됨. **EC2 삭제 작업만 쓴다(2026-10-21 결정)**: 객체 저장소 수명 주기 규칙은 DB 의 보존 표시를 몰라 클립에 쓰지 않는다. 작업이 멈추면 삭제가 밀리므로 주 1회 확인(18.4)에서 `delete_after` 가 지난 미삭제 클립 0건을 본다 |
| CRITICAL 자동 보존 | 자동 보존 표시 | `media_clip_retention.reason = CRITICAL_AUTO` | Tier 0 `media_clip_retention`(I1) | 됨 |
| 클립 열람 기록 | 보존분 접근 기록 | `clip_access_log` | 클립 조회 때 행 추가 | 됨 |
| PPE crop(확장) | 저장 없음 | 없음(`ppe_state` 는 판정 결과만) | — | 됨 |
| 2D 관측 | Tier 0 저장 위치 7~14일 | DB 밖. `observation_summary.archive_uri` 만 | Tier 0 정리 작업 | 됨. 원본이 지워지면 `archive_uri` 가 가리키는 파일이 없다. 보정 작업은 보관 기간 안의 요약만 고른다 |
| 좌표·상태·이벤트 로그 | 발표 후 30일 안 파기 | hypertable 8개, `risk_event*`, `notification` 등 | 18.4 일괄 파기 | 됨. hypertable 의 상시 보관은 아래 표(2026-10-21 결정) |
| 전체 | 발표 후 30일 안 전부 | 만든 테이블 전부(V1~V13 기준 51개, `db-tables.md` 76개 중) + 객체 저장소 | 18.4 파기 확인 | 됨. 파기 목록은 이 문서의 테이블 목록을 쓴다 |

**hypertable 보관·압축(2026-10-21 결정, EC2 디스크 320 GB).** 용량은 10명·운영 8시간·카메라 4대로 잡은 추정이고, 운영 초기에 청크 크기로 다시 잰다.

| hypertable | 쓰는 빈도(추정) | 압축 전 하루(추정) | 상시 보관 | 압축 |
|---|---|---|---|---|
| `world_track_state` | 10 Hz × 10명 ≈ 288만 행 | 약 0.6 GB(11.4 “수백 MB”) | 없음(발표 후 일괄 파기) | 7일 뒤 |
| `skeleton_3d` | 5~10 Hz × 10명 ≤ 288만 행 | 약 1.2 GB(관절 23개 × 3) | 없음. 디스크 70% 를 넘으면 30일 | 7일 뒤 |
| `system_health` | 1 Hz ≈ 2.9만 행 | 약 0.03 GB | 없음 | 7일 뒤 |
| `system_metric` | 지표 약 40개 × 1 Hz ≈ 115만 행 | 약 0.1 GB | 30일 | 7일 뒤 |
| `device_heartbeat` | 1 Hz ≈ 2.9만 행 | 약 0.02 GB | 30일 | 7일 뒤 |
| 확장(`channel_state`·`watch_feature`·`ppe_state`) | — | — | 확장 착수 때 | 확장 착수 때 |

- 합계는 압축 전 운영일 하루 약 2 GB 다. 60 운영일이면 약 120 GB 이고 TimescaleDB 압축(보통 몇 분의 1)을 거치면 그보다 훨씬 작아, 320 GB 에서 OS·컨테이너 이미지·WAL·백업을 빼고도 여유가 크다.
- 좌표·골격·health 는 리플레이(M3)가 프로젝트 기간 전체를 봐야 해서 상시 삭제하지 않는다. 운영 지표·하트비트는 리플레이에 쓰지 않아 30일만 둔다.
- 주 1회 확인(18.4)에서 EC2 디스크 사용률을 보고, 70% 를 넘으면 `skeleton_3d`(리플레이 확장용)부터 30일 보관 정책을 건다.

## 7. 출처 기록 (11.4-2)과 이름 없음 (11.4-5)

| 테이블 | 출처 컬럼 | 대상 |
|---|---|---|
| `world_track_state` | `calibration_version`, `scene_version`, `config_version`, `input_provenance` | 위치 |
| `risk_event_update` | `rule_id`, `rule_version`, `config_version`, `calibration_version`, `scene_version` | 이벤트 |
| `risk_event` | `rule_id`, `rule_version` | 이벤트(현재) |
| `vlm_assessment` | `model_version`, `input_ref` | 근거 |
| `report` | `llm_model`, `citations` | 보고서 |
| `rag_clause` | `embedding_model`, `embedding_revision`, `source_version` | RAG |
| `scene_version` | `calibration_version`, `input_provenance` | Scene |
| `floor_plan` | `calibration_version`, `scene_version` | BEV |
| `sim.evaluation_run` | `config_version`, `engine_version` | 계획 계산 |
| `sim.risk_result` | `rule_id`, `rule_version` | 계획 결과 |
| `ppe_state` | `model_version` | PPE(확장) |

- `skeleton_3d` 에는 버전 컬럼이 없다. 같은 `world_state` 메시지에서 오므로 (`observation_at`, `world_id`)로 `world_track_state` 의 버전을 쓴다.
- 위치·이벤트 테이블(`world_id`·`world_ids`·`event_id` 를 가진 테이블)에 이름 컬럼: 없음. 이름은 `watch_registrant.registrant_name`(확장, 관리자만, C.6)에만 있다.

## 8. 남은 일

- 정한 것(2026-10-21): 초기 데이터·관리 명령(3장), 운영자 종료 멱등 키 분리(4장), 클립은 EC2 삭제 작업만과 hypertable 보관·압축(6장). 기획서 3.1·3.8·11.3·11.4·11.5·11.6·12.6·16장·18.4·19.1 에 반영했다(v1.3 수정 19).
- 노션 API 표: v11 의 행 3개(4장)를 더한다.
- 5단계(S15P31A101-211): MVP·M1 테이블 43개와 위 인덱스·정책으로 `V1__init.sql`, 현장·카메라 V2 시드, 규칙 버전 `R__rule_versions.sql`. 계정·기기·워커 토큰 관리 명령은 서버 코드라 별도 이슈다.

## 부록. API 표 행별 매핑

DB 를 읽거나 쓰는 행만 적는다. "v11" 표시는 4장에서 더한 행이다.

| 기능 ID | API 이름 | 경로 | 실행 위치 | 읽기 | 쓰기 | 쓰는 주체 | 메모 |
|---|---|---|---|---|---|---|---|
| FR-19 | 계획 모드 판정 | `INTERNAL` | EC2 Risk Engine | `layout_plan`, `zone`, `static_object`, `rule_version`, `config_bundle` | `sim.evaluation_run`, `sim.risk_result` | Risk Engine |  |
| FR-22 | BEV 평면도 생성 | `INTERNAL` | 분석 워커 | `calibration_version` | `analysis_job`, `floor_plan` | 워커 결과 | bev_floorplan |
| FR-23, FR-135 | 자동 보정 계산 | `INTERNAL` | 분석 워커 | `calibration_run`, `scale_reference` | `analysis_job`, `calibration_run`, `calibration_version`, `calibration_camera`, `calibration_scale_check`, `coverage_guide` | 워커 결과 | calibration_ba. 이후 상태 변화는 Tier 0 calibration_update |
| FR-30 | 시뮬레이션 판정 | `INTERNAL` | EC2 Risk Engine | `sim.trajectory_plan`, `equipment_asset`, `zone`, `rule_version` | `sim.evaluation_run`, `sim.risk_result` | Risk Engine |  |
| FR-31 | 작업중지 명령서 초안 생성 | `INTERNAL` | 분석 워커 | `risk_event` | `analysis_job`, `agentic_report` | 워커 결과 | job_type 은 확장 3 착수 때 |
| FR-32 | What-if 판정 | `INTERNAL` | EC2 Risk Engine | `sim.what_if_scenario`, `world_track_state`, `layout_plan` | `sim.evaluation_run`, `sim.risk_result`, `sim.visibility_result` | Risk Engine |  |
| FR-34 | 공정 정보 VLM 프롬프트 반영 | `INTERNAL` | 분석 워커 | `work_schedule` | — | — | EC2 가 작업 입력에 넣음 |
| FR-43 | 워치-사람 움직임 대조 연결 | `INTERNAL` | Tier 0 | `watch_feature` | `watch_binding` | Tier 0 | 확장. 계약 미정 |
| FR-59 | 이벤트 클립 저장 | `INTERNAL` | Tier 0 | `upload_grant` | `media_clip`, `media_clip_retention`, `upload_grant` | Tier 0 | CRITICAL 자동 보존은 media_clip_retention |
| FR-80 | 이벤트 저널·outbox 기록 | `INTERNAL` | Tier 0 | — | `risk_event_update`, `risk_event`, `journal_cursor` | Tier 0 | risk_event_transition |
| FR-83 | 구성 묶음 동기화 | `INTERNAL` | Tier 0 | `config_bundle`, `config_bundle_item` | `config_ack`, `config_bundle` | Tier 0 | config_ack, applied_at |
| FR-84 | PPE 검출 | `INTERNAL` | Tier 0 | — | `ppe_state` | Tier 0 | 확장. 계약 미정 |
| FR-86 | 이벤트 근거 설명 생성 | `INTERNAL` | 분석 워커 | `risk_event`, `event_frame` | `analysis_job`, `vlm_assessment` | 워커 결과 | vlm_rationale |
| FR-87 | 심각도 상향 의견 처리 | `INTERNAL` | 분석 워커 | — | `vlm_assessment` | 워커 결과 | proposed_severity |
| FR-88 | 사고 구간 요약 | `INTERNAL` | 분석 워커 | `risk_event_update`, `event_frame` | `analysis_job`, `vlm_assessment` | 워커 결과 | incident_summary |
| FR-90 | 정적 3D 복원 | `INTERNAL` | 분석 워커 | `calibration_version` | `analysis_job`, `scene_version` | 워커 결과 | static_reconstruct |
| FR-93 | 정적 공간 갱신 | `INTERNAL` | 분석 워커 | `scene_version` | `region_state`, `scene_version` | 워커 결과 | 확장. 계약 미정 |
| FR-116 | 운영자 상향 적용 | `INTERNAL` | Tier 0 | — | `event_escalation`, `risk_event_update`, `risk_event` | Tier 0 | event_escalate_result, 전이 |
| FR-78 | 운영자 종료 적용 (v11) | `INTERNAL` | Tier 0 | — | `event_close`, `risk_event_update`, `risk_event` | Tier 0 | event_close_result, RESOLVED 전이 |
| FR-129 | World 상태 송신 | `INTERNAL` | Tier 0 | — | `world_track_state`, `skeleton_3d` | Tier 0 | world_state |
| FR-129 | Health 송신 | `INTERNAL` | Tier 0 | — | `system_health`, `system_metric` | Tier 0 | health |
| FR-130 | 이벤트 프레임 업로드 완료 통지 | `INTERNAL` | Tier 0 | `upload_grant` | `event_frame`, `upload_grant` | Tier 0 | event_frame_ready |
| FR-138 | Tier 0 엔드포인트 등록 | `INTERNAL` | Tier 0 | — | `tier0_endpoint` | Tier 0 |  |
| FR-141 | 2D 관측 요약 송신 | `INTERNAL` | Tier 0 | — | `observation_summary` | Tier 0 | 원본 Parquet 는 DB 밖 |
| FR-142 | 외부 LLM 텍스트 생성 | `INTERNAL` | 분석 워커 | — | `analysis_job`, `report` | 워커 결과 | tbm_report |
| FR-1, FR-35 | 로그인 | `POST /api/auth/login` | EC2 | `app_user` | `refresh_token` | EC2 |  |
| FR-2 | API별 권한 검증 | `INTERNAL` | EC2 | `app_user`, `refresh_token`, `device`, `worker` | — | — |  |
| FR-3 | 현재 Scene 조회 | `GET /api/scenes/current` | EC2 | `scene_version` | — | — |  |
| FR-3, FR-6, FR-7, FR-10, FR-97 | World 상태 수신 | `WS /topic/world` | EC2 | `world_track_state` | — | — | 실시간은 수신 즉시 중계, 재접속 때 최신 상태 |
| FR-3, FR-16 | 구역 목록 조회 | `GET /api/zones` | EC2 | `zone` | — | — |  |
| FR-4 | 이벤트 목록 조회 | `GET /api/events` | EC2 | `risk_event` | — | — |  |
| FR-4, FR-78, FR-97 | 위험 이벤트 전이 수신 | `WS /topic/events` | EC2 | `risk_event` | — | — |  |
| FR-5, FR-97 | System Health 수신 | `WS /topic/health` | EC2 | `system_health`, `device_heartbeat`, `config_ack` | — | — | viewer_health |
| FR-5 | System Health 조회 | `GET /api/health` | EC2 | `system_health`, `device_heartbeat` | — | — |  |
| FR-8, FR-97 | 관제 화면 경보 수신 | `WS /topic/events` | EC2 | `risk_event` | — | — |  |
| FR-9, FR-39 | 경보 확인(ACK) | `POST /api/events/{id}/ack` | EC2 | `risk_event` | `event_ack` | 반장·관리자 | 확정은 Tier 0 전이(ACKNOWLEDGED) |
| FR-9, FR-97 | 경보 확인 수신 | `WS /topic/events` | EC2 | `risk_event`, `event_ack` | — | — |  |
| FR-11, FR-97 | 장애 알림 수신 | `WS /topic/health` | EC2 | `system_health`, `device_heartbeat` | — | — |  |
| FR-11 | 장애 하트비트 감시 | `INTERNAL` | EC2 | `device_heartbeat`, `system_health` | — | — |  |
| FR-12, FR-97 | Scene 버전 불일치 수신 | `WS /topic/health` | EC2 | `scene_version`, `system_health` | — | — |  |
| FR-13 | 시스템 지표 조회 | `GET /api/metrics` | EC2 | `system_metric` | — | — |  |
| FR-14, FR-15 | 입체 구역 생성 | `POST /api/zones` | EC2 | — | `zone` | 관리자 |  |
| FR-16 | 구역 수정(새 버전 추가) | `PUT /api/zones` | EC2 | `zone` | `zone` | 관리자 | 이전 버전 valid_to |
| FR-16 | 구역 보관(지금 버전 닫기) | `DELETE /api/zones/{zoneId}` | EC2 | `zone` | `zone` | 관리자 | 지금 버전 valid_to, 새 버전 없음 |
| FR-16 | 구역 버전 이력 조회 | `GET /api/zones/{zoneId}/versions` | EC2 | `zone` | — | — |  |
| FR-17 | 구성 묶음 발행 | `POST /api/config-bundles` | EC2 | `zone`, `static_object`, `rule_version`, `calibration_version` | `config_bundle`, `config_bundle_item` | 관리자 |  |
| FR-17 | 구성 묶음 적용 상태 조회 | `GET /api/config-bundles/{version}` | EC2 | `config_bundle`, `config_ack`, `config_bundle_item`, `static_object` | — | — | items: 실은 항목 ID·버전 |
| FR-17, FR-97 | 구성 묶음 적용 수신 | `WS /topic/health` | EC2 | `config_ack` | — | — |  |
| FR-18 | 가상 레이아웃 생성 | `POST /api/layouts` | EC2 | — | `layout_plan` | 관리자 |  |
| FR-18 | 가상 레이아웃 단건 조회 | `GET /api/layouts/{layoutId}` | EC2 | `layout_plan` | — | — |  |
| FR-18 | 가상 레이아웃 수정 | `PUT /api/layouts/{layoutId}` | EC2 | `layout_plan` | `layout_plan` | 관리자 | revision |
| FR-18 | 가상 레이아웃 목록 조회 | `GET /api/layouts` | EC2 | `layout_plan` | — | — |  |
| FR-19 | 계획 모드 평가 요청 | `POST /api/layouts/{id}/evaluate` | EC2 | `layout_plan` | `sim.evaluation_run` | 관리자 |  |
| FR-19 | 계획 모드 평가 결과 조회 | `GET /api/layouts/{id}/evaluations/{evaluationId}` | EC2 | `sim.evaluation_run`, `sim.risk_result` | — | — |  |
| FR-20 | TBM 보고서 초안 생성 | `POST /api/reports/tbm` | EC2 | `sim.evaluation_run`, `sim.risk_result`, `rag_clause` | `report`, `analysis_job`, `sim.evaluation_run` | 관리자 |  |
| FR-20, FR-21 | TBM 보고서 조회 | `GET /api/reports/tbm/{reportId}` | EC2 | `report` | — | — |  |
| FR-20 | 안전 수칙 RAG 검색 | `INTERNAL` | EC2 | `rag_clause` | — | — |  |
| FR-22 | BEV 평면도 조회 | `GET /api/floor-plans/current` | EC2 | `floor_plan` | — | — |  |
| FR-23 | 자동 보정 시작 | `POST /api/calibrations` | EC2 | — | `calibration_run`, `analysis_job` | 관리자 |  |
| FR-23 | 보정 결과 확정 | `POST /api/calibrations/{calibrationId}/confirmation` | EC2 | `calibration_version` | `calibration_version` | 관리자 | state |
| FR-24 | 보정 검증 | `POST /api/calibrations/{calibrationId}/verifications` | EC2 | `calibration_version` | `calibration_verification` | 관리자 |  |
| FR-25 | 보정 상태 조회 | `GET /api/calibrations/current` | EC2 | `calibration_version`, `calibration_camera` | — | — |  |
| FR-26 | 설치 안내 조회 | `GET /api/cameras/coverage-guides` | EC2 | `coverage_guide` | — | — |  |
| FR-27 | 정적 3D 생성 요청 | `POST /api/scenes/rebuild` | EC2 | `calibration_version` | `scene_version`, `analysis_job` | 관리자 | CANDIDATE 행 |
| FR-27, FR-108 | Scene 커밋 상태 조회 | `GET /api/scenes/{sceneId}` | EC2 | `scene_version` | — | — |  |
| FR-28 | 리플레이 시각 재현 조회 | `GET /api/world?at={t}` | EC2 | `world_track_state`, `skeleton_3d`, `risk_event_update`, `zone`, `rule_version`, `scene_version` | — | — | 아래 리플레이 질의 |
| FR-28 | 리플레이 구간 조회 | `GET /api/replay?from={t1}&to={t2}` | EC2 | `world_track_state`, `risk_event_update` | — | — |  |
| FR-30 | 위험 동선 시뮬레이션 요청 | `POST /api/simulations` | EC2 | `equipment_asset` | `sim.trajectory_plan`, `sim.evaluation_run` | 관리자 |  |
| FR-30 | 위험 동선 시뮬레이션 결과 조회 | `GET /api/simulations/{simulationId}` | EC2 | `sim.evaluation_run`, `sim.risk_result` | — | — |  |
| FR-31 | 작업중지 명령서 승인·발행 | `POST /api/agentic-reports/{reportId}/approval` | EC2 | `agentic_report` | `agentic_report` | 관리자 |  |
| FR-31 | 작업중지 명령서 조회 | `GET /api/agentic-reports/{reportId}` | EC2 | `agentic_report` | — | — |  |
| FR-32 | What-if 분석 요청 | `POST /api/what-if-analyses` | EC2 | — | `sim.what_if_scenario`, `sim.evaluation_run` | 관리자 |  |
| FR-32 | What-if 분석 결과 조회 | `GET /api/what-if-analyses/{analysisId}` | EC2 | `sim.evaluation_run`, `sim.risk_result`, `sim.visibility_result` | — | — |  |
| FR-33 | 배치 추천 요청 | `POST /api/placement-recommendations` | EC2 | `sim.visibility_result` | `sim.evaluation_run`, `sim.placement_recommendation` | 관리자 |  |
| FR-34 | 공정표 등록 | `POST /api/work-schedules` | EC2 | — | `work_schedule` | 관리자 |  |
| FR-36 | 알림 구독 등록 | `POST /api/push/subscribe` | EC2 | — | `push_subscription` | 반장 |  |
| FR-36 | 알림 구독 해지 | `DELETE /api/push/subscribe` | EC2 | `push_subscription` | `push_subscription` | 반장 | revoked_at |
| FR-37 | 위험 알림 Web Push 발송 | `INTERNAL` | EC2 | `risk_event`, `push_subscription` | `notification` | EC2 | LIVE 만 |
| FR-38 | 알림 상세 조회 | `GET /api/events/{id}` | EC2 | `risk_event`, `notification`, `vlm_assessment`, `action_ticket` | — | — |  |
| FR-40, FR-41 | 조치 티켓 상태 변경 | `PATCH /api/tickets/{id}` | EC2 | `action_ticket` | `action_ticket`, `action_ticket_history` | 반장 |  |
| FR-40 | 조치 티켓 조회 | `GET /api/tickets` | EC2 | `action_ticket` | — | — |  |
| FR-40 | 조치 티켓 자동 생성 | `INTERNAL` | EC2 | `risk_event` | `action_ticket` | EC2 |  |
| FR-42(취소), FR-97 | 조치 불일치 수신(취소 2026-11-02, 토픽·계약 삭제 2026-11-03, S15P31A101-411) | `WS /topic/admin/events`(삭제) | EC2 | `action_ticket`, `risk_event` | — | — |  |
| FR-42(취소) | 조치 불일치 판정(취소 2026-11-02, S15P31A101-411) | `INTERNAL` | EC2 | `action_ticket`, `risk_event` | `action_ticket` | EC2 | mismatch |
| FR-43 | 워치 가속도 전송 | `WS I4 gait_features` | EC2 | — | `watch_feature` | 워치 |  |
| FR-44 | 워치 채널 상태 관리 | `INTERNAL` | EC2 | `watch_binding` | `channel_state` | EC2 |  |
| FR-45 | 워치 진동 알림 발송 | `INTERNAL` | EC2 | `channel_state`, `watch_binding`, `risk_event` | `notification`, `watch_alert` | EC2 |  |
| FR-46 | 워치 수신 확인 | `WS I4 ack` | EC2 | `watch_alert` | `watch_alert` | 워치 | acked_at |
| FR-46 | 워치 미확인 격상 | `INTERNAL` | EC2 | `watch_alert` | `watch_alert` | EC2 | escalated_at |
| FR-47 | 워치 충격 이벤트 전송 | `WS I4 impact` | EC2 | — | `watch_impact` | 워치 |  |
| FR-59 | 이벤트 클립 조회 | `GET /api/clips/{id}` | EC2 | `media_clip` | `clip_access_log` | 관리자 |  |
| FR-81, FR-95 | 이벤트 전이 전송·수신 | `INTERNAL` | EC2 | `journal_cursor` | `risk_event_update`, `risk_event`, `journal_cursor` | Tier 0 |  |
| FR-81 | EC2 복구 재동기화 | `INTERNAL` | EC2 | `journal_cursor` | `journal_cursor`, `risk_event` | Tier 0 | event_state_snapshot |
| FR-88 | 사고 구간 요약 요청 | `POST /api/events/{id}/summary` | EC2 | `risk_event`, `event_frame` | `analysis_job` | 관리자 |  |
| FR-96 | 알림 라우팅 | `INTERNAL` | EC2 | `risk_event`, `push_subscription`, `app_user` | — | — |  |
| FR-97 | 소켓 연결·인증 | `WS /ws` | EC2 | `app_user`, `refresh_token` | — | — |  |
| FR-99 | 클립 보존 표시 변경 | `PUT /api/clips/{id}/retention` | EC2 | `media_clip` | `media_clip`, `media_clip_retention` | 관리자 |  |
| FR-99 | 보관 기간 자동 삭제 | `INTERNAL` | EC2 | `media_clip` | `media_clip` | EC2 | deleted_at. 아래 보관·파기 |
| FR-100 | 임시 서명 업로드 주소 발급 | `POST /api/uploads/sign` | EC2 | — | `upload_grant` | 기기·반장 |  |
| FR-101 | 분석 작업 결과 제출 | `POST /api/worker/jobs/{job_id}/result` | EC2 | `analysis_job`, `job_lease` | `analysis_job`, `job_lease` | 워커 | 결과 테이블은 작업 종류별 행(워커 결과) |
| FR-101 | 분석 작업 대여 | `POST /api/worker/jobs/lease` | EC2 | `analysis_job`, `worker` | `job_lease`, `analysis_job`, `worker` | 워커 | last_seen_at |
| FR-101 | 분석 작업 대여 연장 | `POST /api/worker/jobs/{job_id}/heartbeat` | EC2 | `job_lease` | `job_lease` | 워커 |  |
| FR-101 | 분석 작업 등록·재할당 | `INTERNAL` | EC2 | `job_lease` | `analysis_job`, `job_lease` | EC2 |  |
| FR-112 | 정적 객체·작업면 등록 | `POST /api/static-objects` | EC2 | — | `static_object` | 관리자 |  |
| FR-112 | 정적 객체·작업면 조회 | `GET /api/static-objects` | EC2 | `static_object` | — | — |  |
| FR-112 | 정적 객체·작업면 새 버전 | `PUT /api/static-objects` | EC2 | `static_object` | `static_object` | 관리자 |  |
| FR-112 | 정적 객체·작업면 보관 | `DELETE /api/static-objects/{objectId}` | EC2 | `static_object` | `static_object` | 관리자 | 지금 버전 valid_to, 새 버전 없음 |
| FR-113 | 규칙 버전 조회 | `GET /api/rules` | EC2 | `rule_version` | — | — |  |
| FR-116 | 상향 의견 적용 확인 | `POST /api/events/{id}/escalation` | EC2 | `vlm_assessment`, `risk_event` | `vlm_assessment`, `event_escalation` | 관리자 | decision |
| FR-78 | 이벤트 운영자 종료 (v11) | `POST /api/events/{id}/close` | EC2 | `risk_event`, `event_close` | `event_close` | 관리자 | 멱등 키는 event_close.idempotency_key. 결과 컬럼은 운영자 종료 적용 행(Tier 0) |
| FR-122 | 일일 일지 생성 요청 | `POST /api/reports/daily` | EC2 | `risk_event`, `risk_event_update` | `report`, `analysis_job` | 관리자 |  |
| FR-134 | 노트북 하트비트 수신 | `POST /api/devices/heartbeat` | EC2 | `device` | `device_heartbeat`, `device` | 기기 | last_heartbeat_at |
| FR-135 | 실측 정물 등록 | `POST /api/scale-references` | EC2 | — | `scale_reference` | 관리자 |  |
| FR-135 | 실측 정물 목록 조회 (v11) | `GET /api/scale-references` | EC2 | `scale_reference` | — | — |  |
| FR-136 | 푸시 수신 확인 | `POST /api/push/receipts` | EC2 | `notification`, `test_push` | `push_receipt`, `notification`, `test_push` | 반장 | receipt_at |
| FR-138 | Tier 0 엔드포인트 조회 | `GET /api/endpoints/tier0` | EC2 | `tier0_endpoint` | — | — |  |
| FR-140 | 시험 푸시 | `POST /api/push/test` | EC2 | `push_subscription` | `test_push` | 관리자 |  |

DB 를 쓰지 않는 행: 반복 소리 정지 지시, 사람 검출·keypoint 추출, 카메라별 추적, 프레임 최신성 처리, World ID 연결, 삼각측량, 관절 유효성 판정, 접지 추정, ID 교정 기록, 관측 상태 전환, R1 위험 구역 진입 판정, R2 개구부·가장자리 근접 판정, R3 고소 위치 판정, R4 낙상 의심 판정, R5 진입 예측 판정, R6 구역 내 관측 상실 판정, R7 구역 감시 능력 저하 판정, R8 장비 근접 판정, R9 PPE 미착용 판정, R10 불확실 점유 근접 판정, 히스테리시스·재개, 이벤트 병합, 이벤트 생명주기 관리, Tier 0 재시작 복구, PPE 착용 판정 규칙, VLM 입력 얼굴 흐림, 사람 마스크 제거, SAM 3D Body 관절 보강, Unreal 배경 빌드, 작업 대여·실행 루프, 스트림 수신·손실 집계, 시간 보간·동기 검증, 입력 추상화(파일 모드), Risk Engine 이중 배포 검증, 시계 차이 교환 요청, 2D 관측 보관·정리, VAPID 공개키 조회, 카메라 RTSP 수신, 촬영 시각 헤더 부착, RunPod 프레임 중계, 현장 NTP 시각 기준, 현장 경보 출력, 현장 확인 버튼 처리, 현장 장애별 표시, 시험 경보, 출처 분리, Spatial DB 기록, 스트림 재접속·손실 계측, 카메라 미리보기, 현장 경보 재동기화, 시계 차이 교환 응답.
