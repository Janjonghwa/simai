# EC2 구역·작업면·구성 묶음 발행

S15P31A101-120(구역 CRUD·구성 묶음 발행·config_ack 수신)·S15P31A101-58(작업면·정적 객체 등록, 구성 묶음 포함).
명세 3.2(구성 묶음), 7.4(교실 시험 규칙 버전), 7.5(작업면), 8.2(구역), 11.1·11.4(덮어쓰지 않음), 12.6(REST), 계약 `config_bundle`·`config_ack`.
코드는 `server/.../domain/zone/`, `domain/geometry/`, `domain/calibration/`, `domain/config/`. 받는 쪽은 Tier 0 `ai/src/simai_ai/config/`(검증·전환)와 `ai/services/tier0/config_fetch.py`(내려받기).

## 흐름

1. 관리자가 구역(`/api/zones`)·작업면(`/api/static-objects`)을 만들거나 새 버전을 더한다. 수정은 지금 버전을 닫고(`valid_to`) 다음 버전을 더한다.
2. 관리자가 Tier 0 의 보정 문서를 올린다(`POST /api/calibrations`). EC2 는 계약(`contracts/providers/calibration`)과 현장을 검사해 `calibration_version`·`calibration_document` 에 둔다.
3. 관리자가 묶음을 발행한다(`POST /api/config-bundles`). EC2 는 지금 유효한 구역·규칙·최근 보정·작업면으로 파일 5개(매니페스트 + 구성 요소 4개)를 만들어 바이트·해시를 저장하고, 열린 I1 연결에 `config_bundle` 을 보낸다.
4. Tier 0 는 서명 주소로 파일을 받아 해시·형식·참조를 검사하고 원자적으로 전환한 뒤 `config_ack` 로 답한다. EC2 는 APPLIED 이고 해시가 같을 때만 `applied_at` 을 채운다. 그 전까지 상태는 PENDING(적용 대기)이다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 구역 본문 | `name`, `zone_kind`, `eval_type`(GROUND_FOOTPRINT·VOLUME_3D), `polygon_xy`(바닥 XY 꼭짓점, 닫는 점 생략 가능), `z_min_m`·`z_max_m`(VOLUME_3D 는 필수, 최저 < 최고), `severity`, `has_guardrail`, `requirements` | V1 `zone` 열. 노션 API 명세는 "예시, 미정" |
| 구역 ID | 서버가 `z-<8 hex>` 로 만든다 | 이벤트의 `zone_id` 와 그대로 맞춘다 |
| 다각형 검사 | 꼭짓점 3~256 개, 유한한 수, PostGIS `ST_IsValid` 이고 넓이 > 0 | 자기 교차 다각형은 Tier 0 판정에서 뜻이 없다 |
| 작업면·정적 객체 본문 | `kind`(WORK_SURFACE·OBSTACLE), `name`, `obb`{`center`[x,y,z], `size`[가로,세로,높이], `yaw_rad`}, `top_height_m`(작업면은 > 0 필수), `top_height_sigma_m`(선택) | Tier 0 work_surface 형식(OBB 바닥 투영 + 상판 높이) |
| 객체 ID | 작업면 `ws-<8 hex>`, 그 밖 `so-<8 hex>` | |
| 보정 문서 | 관리자가 올린다. 같은 버전은 같은 문서(해시)만 다시 받고, 다르면 409 | Tier 0 는 보정을 주소·해시로만 알려 EC2 에 본문이 없다(사용자 결정 2026-10-26) |
| 묶음 버전 | `cfg-<n>`(1부터), 구성 요소 버전 `cfg-<n>-zones`·`-rules`·`-geometry`, 보정은 `calibration_version` | 발행을 advisory lock 으로 한 줄로 세운다 |
| 묶음에 넣는 구역·작업면 | 지금 유효한 버전만(`valid_to` 없음) | Tier 0 는 같은 ID 의 유효 구간이 겹치면 거부한다 |
| 규칙 | `rule_profile` OPERATION·CLASSROOM_TEST. 규칙 ID 마다 하나: 교실 시험이면 그 규칙의 교실 시험 버전, 없으면 운영 버전. 없으면 이전 묶음의 프로필, 처음은 OPERATION | 7.4 별도 rule_version, 등록부(R__rule_versions) |
| 구성 요소 내용 형식 | Tier 0 임시 형식(`ai/src/simai_ai/config/bundle.py` 표) 그대로. 구역은 `Zone.from_json` 키만(`zone_version` 은 문자열) | 내용 형식은 계약 범위 밖 |
| 파일 주소 | `https://<공개 주소>/api/config-bundles/{version}/files/{name}?sig=<HMAC>`. JWT 없이 열리고 서명이 틀리면 404 | Tier 0 는 헤더 없이 https GET 만 한다. 주소가 매니페스트에 들어가 해시가 고정되므로 만료를 두지 않는다 |
| 서명 키 | JWT 서명 키에서 용도 이름으로 뽑은 HMAC 키 | 새 비밀 값을 늘리지 않는다. JWT 키를 바꾸면 이전 묶음 주소가 닫힌다 |
| 공개 주소 | `SIMAI_PUBLIC_BASE_URL`(compose 는 `https://${SIMAI_DOMAIN}`). https 가 아니면 발행 503 | Tier 0 는 https 만 받는다 |
| 알림 보내기 | 발행 때 열린 연결 모두에, 그 뒤 10초마다 가장 최근 묶음에 아직 응답하지 않은 Tier 0 에 30초 간격으로 다시 | 끊겨 있던 Tier 0 도 다시 붙으면 받는다 |
| 적용 상태 | 가장 늦게 받은 `config_ack` 로 PENDING·APPLIED·REJECTED. 화면 응답에는 서명 주소를 싣지 않는다 | |

## 시험

`ConfigBundleIntegrationTest`: 실제 포트로 관리자 API 를 부르고 Tier 0 처럼 I1 에 붙는다. 구역 새 버전·이력, 꼬인 다각형·높이 없는 VOLUME_3D·권한 거부,
작업면 상판 높이, 보정 문서 계약·같은 버전 충돌, 발행 → `config_bundle`(계약 검사) → 서명 주소로 파일 5개를 받아 Tier 0 와 같은 순서로 해시·버전 대조 →
`config_ack` APPLIED(중복 msg_id 한 번) → 상태 APPLIED, 다음 묶음의 이전 버전·프로필 이어받기.

## 남은 일

- Tier 0 가 보정 문서를 EC2 로 직접 올리는 길(`calibration_update` 수신 + 업로드). 그전에는 보정이 바뀔 때마다 관리자가 올린다.
- `/topic/health`(`viewer_health`)의 구성 묶음 적용 상태 표시는 S15P31A101-122.
- 관제 앱 구역·작업면 화면(FE -189·-165)은 이 형식으로 바꾼다(지금 `features/zones/types.ts` 는 DEMO 형식).
- 구역 삭제(비활성화)는 명세 12.6 에 없어 만들지 않았다.
