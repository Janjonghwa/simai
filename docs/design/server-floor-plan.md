# EC2 BEV 평면도 작업 등록·조회

S15P31A101-240. 명세 4.1(BEV 평면도, 보조 화면), 12.5(`bev_floorplan` 은 CALIBRATED 가 되거나 보정 버전이 바뀌면 EC2 가 등록), 12.6(`GET /api/floor-plans/current` 열람), FR-22, 계약 `bev_floorplan.schema.json`(job_request 3.2.0 input, job_callback 2.2.0 result).
코드는 `server/.../domain/floorplan/`(FloorPlanService·Controller·Scheduler), 관리 명령 `global/command/FloorPlanCommand`, 작업 종료 이벤트 `domain/job/JobFinished`. 워커 쪽은 `ai/services/worker/bev_floorplan.py`(-239), 객체 서명은 -248.

평면도는 보조 화면이다. 좌표·스케일의 기준이 아니고 안전 판단에 쓰지 않는다(결과 `purpose` OVERVIEW_DISPLAY_ONLY, `safety_use` false).

## 흐름

1. 관리자가 카메라마다 사람 없는 프레임을 버킷에 올리고 등록한다: `admin floorplan frames --frames cam_A=s3://<버킷>/…,cam_B=… [--source LIVE]`.
2. 대조(10초 주기 + 등록 직후): 현재 보정(가장 늦은 `calibration_update`)이 CALIBRATED 이고 (보정 버전, 최신 프레임 묶음)의 평면도가 없으면
   `bev_floorplan` 작업(BACKGROUND, 시도 2, 10분)을 넣고 `floor_plan` 을 PENDING 으로 만든다. input 은 보정 알림의 `calibration_ref`·`calibration_hash`, 프레임 묶음의 `frame_source`·`frames`.
3. 워커가 작업을 대여해 보정 문서·프레임을 받고(-248 서명) PNG·JSON 을 `floor-plans/{job_id}/{lease_id}/` 에 올린 뒤 결과를 제출한다.
4. 작업이 끝나면 `JobFinished` 로 `floor_plan` 을 READY(이미지 키·결과 원문, `site.current_floor_plan_id`) 또는 FAILED 로 바꾼다.
5. 화면은 `GET /api/floor-plans/current` 로 받는다.

```
GET /api/floor-plans/current   (열람 이상)
→ 200 { "floor_plan_id", "calibration_version", "current_calibration_version", "stale", "pending",
        "image_url": "<서명 주소>", "image_url_expires_at_ms", "content_type", "image_sha256", "scene_version", "created_at",
        "purpose", "safety_use", "coordinate_frame", "raster", "cameras_used", "cameras_skipped", "coverage_ratio", "room_outline",
        "generated_at" }
→ 404 FLOOR_PLAN_NOT_READY   (READY 평면도가 하나도 없음)
```

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 프레임 출처 | 관리 명령으로 등록한 묶음(`bev_frame_set`, 추가만, 가장 늦은 것 사용). 버킷 안 `s3://` 주소·카메라마다 한 장만 | 명세·계약에 사람 없는 프레임 주소를 EC2 에 주는 길이 없다. 사용자 결정 2026-10-27(관리 명령). 워커 서명 API 는 우리 버킷만 내려 준다 |
| 등록 시점 | 보정 수신 코드에 끼우지 않고 주기 대조(10초, `simai.floor-plan.reconcile-interval`)와 프레임 등록 직후 대조 | 보정 알림·관리자 업로드·프레임 등록 중 무엇이 먼저 와도 한 규칙으로 판단. BACKGROUND 라 몇 초 늦어도 된다 |
| 한 번만 | (보정 버전, 프레임 묶음)당 작업 한 번. 트랜잭션 잠금(`pg_advisory_xact_lock`) + 고유 색인 | 완료 조건 "보정 버전이 바뀌면 작업이 한 번 등록". 작업이 실패해도(워커 실패는 같은 입력이면 같다) 다시 넣지 않는다. 다시 만들려면 프레임을 새로 등록 |
| 등록 거부 | 보정 문서 주소가 버킷 밖(`CALIBRATION_REF_NOT_IN_BUCKET`)이거나 계약 검사에 걸리면(그 오류 코드) 작업 없이 FAILED 행을 남긴다. 이 행은 막지 않아 설정을 고치면 다음 대조가 넣는다. 같은 이유는 경고를 한 번만 남긴다 | 워커가 반드시 실패할 작업으로 시도를 쓰지 않게, 그러나 설정 오류로 그 버전의 평면도가 영영 막히지 않게(코드 리뷰 반영) |
| 옛 버전 작업 | 새 보정 버전 작업을 넣을 때 다른 버전의 PENDING 작업은 취소한다(FAILED·CANCELLED) | 쓸모없는 작업이 워커 시간을 쓰고 새 작업이 기다리지 않게 |
| 결과 저장 | `floor_plan.result` 에 결과 원문, `image_key` 는 `image_uri` 의 키. 이 작업의 출력 자리(`floor-plans/{job_id}/`) 밖이면 FAILED `IMAGE_NOT_IN_JOB_OUTPUT`. 실패면 `error` 에 워커 실패 코드(없으면 TIMED_OUT·CANCELLED) | 조회는 열람 권한으로 이 키를 서명한다. 다른 객체(클립·보정 문서)를 가리키는 결과를 받으면 버킷의 아무 객체나 내주게 된다. `scene_version` 칸은 비운다(FK 대상이 아직 없음, 결과 원문에 있음) |
| 현재 평면도 | `site.current_floor_plan_id` 는 결과가 현재 보정 버전의 것일 때만 바꾼다 | 늦게 끝난 옛 버전 작업이 덮어쓰지 않게 |
| 종료 알림 | `JobLeaseService`·`AnalysisJobService.cancel`(트랜잭션)이 같은 트랜잭션에서 `JobFinished`(result·error)를 발행 | 리스너를 주입하면 FloorPlanService ↔ AnalysisJobService 순환 의존. 듣는 쪽이 실패하면 제출·취소도 되돌아가 다시 시도된다 |
| 조회 | 현재 보정 버전의 READY 가 있으면 그것, 없으면 가장 최근 READY 를 `stale: true` 로. `pending` 은 현재 버전 작업이 도는 중인지 | 보정이 바뀌어 새로 만드는 동안 화면이 비지 않게 |
| 권한 | 열람 이상(JWT 인증만) | 명세 12.6 열람 |

## 시험

`FloorPlanIntegrationTest`: 프레임·CALIBRATED 보정이 모두 있을 때 한 번 등록(입력이 계약 검사 통과·BACKGROUND), 같은 조합 다시 넣지 않음, 결과로 READY·`current_floor_plan_id`·조회(서명 주소·stale false), 토큰 없음 401 /
버전 변경(PROVISIONAL 은 넣지 않음, 새 CALIBRATED 에 새 작업, 그동안 이전 평면도 stale·pending), 취소 → FAILED·다시 넣지 않음·새 프레임이면 다시 /
보정 문서·결과 이미지가 버킷 밖이면 FAILED, 다른 유형 종료는 무시 / 프레임 검증 6가지·관리 명령 등록·reconcile.

## 남은 일

- 프레임 자동화: Tier 0 가 보정 뒤 카메라별 빈 장면 프레임을 올리고 알리게 하면(계약 변경) 관리 명령 없이 돈다.
- 작업면 등록 화면(S15P31A101-165)·관제 보조 화면이 `raster.pixel_to_world` 로 픽셀 ↔ World 를 바꾼다.
- 실제 S3·워커로 종단 확인(-69·-248 과 같이).
- "현재 보정" 쿼리(LEAST(sent_at, received_at) 순서)가 `CalibrationViews`(웹 전용)와 여기 두 곳에 있다. 웹 조건 없는 공용 조회로 합친다.
- 만료 묶음 처리(`expireDue`)는 한 트랜잭션이라, 평면도 갱신이 DB 오류로 실패하면 같은 묶음의 다른 작업 만료도 되돌아간다(다음 주기에 다시 시도).
