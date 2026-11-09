# 작업 시작·종료 때 안전모 요구(ppe_policy) 발행 (S15P31A101-518)

기획서 §7.2 R9 행·설명(작업 기준 요구, `ppe_policy` 없음은 요구 없음), §8.10(작업 시작·종료 때 새 `config_version` 발행), §12.3
(`config_bundle` 1.3.0, `contracts/data/ppe_policy` 1.0.0, -549 부터 1.1.0), §12.4(작업 시작·종료 API), §3.2(구성 묶음·`config_ack`)가 기준이다.

Jira 본문의 `work_unit.ppe_helmet`·`ppe_vest` 칸, 공종 기본값, `zone.requirements` 검사는 -517(v1.3 수정 66)에서 폐기됐다.
요구는 진행 중 작업(`IN_PROGRESS`)이 있으면 현장 전체 REQUIRED, 없으면 NOT_REQUIRED 이고, 작업별·공종별·구역별 설정은 없다. 조끼 요구(`vest`)는 -549 에서 안전모와 같은 규칙으로 더했다(`format_version` 1.1.0, 늘 싣는다, 명세 v1.3 수정 68, [`server-vest-r11.md`](server-vest-r11.md)).
그래서 작업 칸과 작업 등록·수정 API 변경은 없다. DB 는 묶음 파일 이름 제약에 `ppe_policy` 를 더하는 것만 바꾼다(V44).

## 코드와 시험

| 부분 | 코드 | 시험 |
|---|---|---|
| `ppe_policy` 구성 요소 | `server/.../domain/config/ConfigBundleService.java`(`build`, `ppePolicy`), `ConfigFiles.java`(`PPE_POLICY`, `COMPONENTS`) | `ConfigBundleIntegrationTest` |
| 묶음 파일 이름 제약 | `server/src/main/resources/db/migration/V44__config_bundle_ppe_policy.sql`(`config_bundle_file.name` 에 `ppe_policy`) | 같은 시험 |
| 알림 버전 1.3.0 | `ConfigBundleSender.CONFIG_BUNDLE_SCHEMA_VERSION` | 같은 시험(알림을 계약으로 검사) |
| 작업 시작·종료 발행 | `server/.../domain/work/WorkSessionService.java`(`publishWith`), `WorkUnitController.java`(발행자 = 로그인 사용자) | `ConfigBundleIntegrationTest`, `WorkSessionIntegrationTest` |
| 묶음 보기 요약 | `ConfigBundleViews.java`(`ppe_policy`: helmet·vest·work_id·effective_from_ms, 없으면 null. 1.0.0 으로 발행한 이전 묶음의 vest 는 null) | `ConfigBundleIntegrationTest` |

## 동작

- 묶음을 만드는 경로(직접 발행 `POST /api/config-bundles`, 구역 저장 `POST /api/zones/commit`, 세팅 결과, 작업 시작·종료)는 모두
  `ConfigBundleService.build()` 를 지난다. `ppe_policy` 를 거기서 만들어 어느 경로로 내도 빠지지 않는다. Tier 0 는 `ppe_policy` 가 없는
  묶음을 요구 없음으로 보므로(7.2), 작업 중 구역을 저장했는데 요구가 빠지는 일을 막는다.
- 작업 상태는 발행 잠금(`pg_advisory_xact_lock('simai.config_bundle')`) 뒤에 읽는다. 작업 시작·종료 트랜잭션은 상태를 바꾼 뒤 같은
  트랜잭션에서 잠금을 잡고 커밋할 때까지 쥐므로, 동시에 낸 다른 묶음은 시작·종료가 커밋된 뒤의 상태를 싣고 더 큰 `config_version` 을 받는다.
- 작업 시작·종료는 상태 바꾸기와 묶음 만들기를 한 트랜잭션으로 하고(`publishWith`, 구역 저장과 같은 방식), Tier 0 로 보내기는 커밋 뒤에
  한다. `config_ack` 는 기다리지 않는다. 응답 전까지 묶음은 PENDING(적용 대기)이다(3.2). 응답 형식은 바꾸지 않았다.
- 세팅 결과 발행은 진행 중 작업이 있으면 하지 않으므로(기존 동작) 늘 NOT_REQUIRED 를 싣는다.

## 명세에 없어 정한 것과 근거 (사용자 승인 2026-11-06)

| 항목 | 정한 값 | 근거 |
|---|---|---|
| D1 묶음을 못 만들 때 | 작업 시작·종료를 모두 거절하고 상태를 되돌린다(보정 없음 409, 공개 주소 미설정 등 기존 Config 오류 그대로) | 12.4 "현장 세팅·구역 발행이 완료된 경우에만 시작 허용". 종료 묶음이 빠지면 작업이 없는데 Tier 0 가 계속 REQUIRED 로 보고 R9 를 낸다 |
| D2 `config_ack` 대기 | 기다리지 않는다 | 3.2 "ACK 전에는 적용 대기 표시". Tier 0 가 끊겨도 작업 시작·종료가 막히지 않는다(송신기가 다시 보낸다) |
| D3 "PPE 요구 미수신" 전달 | 계약 변경 없음. `GET /api/config-bundles` 보기에 `ppe_policy` 요약을 더한다(없으면 null) | -519 설계: EC2 가 적용 묶음(`config_ack` APPLIED)으로 판단. 화면 판단·표시는 -520 |
| D4 `effective_from_ms` | 진행 중 작업의 `actual_started_at`, 없으면 이 현장의 가장 최근 `actual_ended_at`, 끝난 작업도 없으면 묶음 `issued_at` | 계약은 시작·종료 묶음만 정함(그 경우 위 규칙과 같은 값). Tier 0 는 기록·표시에만 쓴다 |
| `ppe_policy` version | `cfg-N-ppe` | 다른 구성 요소(`cfg-N-zones` 등)와 같은 이름 방식. 묶음마다 새 version(덮어쓰지 않음, 규칙 6) |
| `config_bundle_item` | `ppe_policy` 는 넣지 않는다 | 항목 표는 구역·규칙·정적 객체의 ID·버전 대응용이고, 요구는 작업 상태에서 매번 만든다 |

## 버린 대안

- 작업 시작·종료 커밋 뒤 따로 발행(세팅 결과 방식): 발행이 실패하면 작업 상태와 Tier 0 요구가 어긋난 채 남는다.
- 작업 시작·종료 때만 `ppe_policy` 를 싣기: 그 사이 구역 저장 묶음에 요구가 빠져 Tier 0 가 요구 없음으로 돌아간다.
- 서버 설정 스위치로 `ppe_policy` 싣기를 끄기(D6): dev Tier 0 는 `ppe_policy` 를 무시하고 묶음을 적용하므로 위험한 것은 -517 이전 이미지뿐이다. MR 에 배포 전제를 적는다.

## 잔여 위험과 넘긴 일

- 배포 전제: `components` 는 닫힌 객체라 -517(계약 v7.16.0) 이전 Tier 0 이미지는 이 서버의 묶음을 통째로 거부한다(12.3). -517 이후
  dev Tier 0 는 `ppe_policy` 를 읽지 않고 적용하며, R9 판정은 -519(MR !466) 머지 뒤에 된다.
- 공개 주소(`SIMAI_PUBLIC_BASE_URL`)가 없는 로컬 환경에서는 작업을 시작·종료할 수 없다(D1).
- 계약 v7.17.0(`PPE_UNKNOWN`) 수용 확인은 -519 머지 뒤, R9 를 켜기 전에 한다(서버는 계약에서 생성한 클래스로 받는다). 이 이슈 범위 밖.
- R9 를 켜는 시드(rule_version)는 -518·-520 머지 뒤 별도. 관제 화면 "PPE 요구 미수신"·요구 해제 해소 표시는 -520.
