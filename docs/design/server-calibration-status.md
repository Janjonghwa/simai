# EC2 보정 상태·설치 안내 조회

S15P31A101-263. 명세 4.4(DRIFTED), 4.5(보정 버전·설치 안내), 12.2(I1 `calibration_update`), 12.6(`GET /api/calibrations/current`,
`GET /api/cameras/coverage-guides`), FR-25·26, 계약 `calibration_update` 2.x.
코드는 `server/.../domain/calibration/`(`CalibrationUpdateIngestor`·`CalibrationViews`·`CalibrationContentHash`·`CalibrationController`).
보내는 쪽은 `ai/src/simai_ai/calibration/watch.py`(`calibration_update_payload`), 해시는 `ai/src/simai_ai/calibration/document.py`(`content_hash`).

## 흐름

1. Tier 0 가 보정 상태나 버전이 바뀔 때 I1 로 `calibration_update` 를 보낸다. EC2 는 계약 스키마 전체로 검사하고
   `calibration_update_message` 에 한 행을 추가한다(V10).
2. 처음 보는 `calibration_version` 이면 `calibration_version` 행을 알림 그대로 만든다. 이미 있으면 같은 `calibration_hash` 인지만 보고,
   행은 덮어쓰지 않는다(AGENTS 6). 모르던(null) 이전 버전만 채운다.
3. `GET /api/calibrations/current`(열람)는 `sent_at` 이 가장 늦은 알림의 값을 그대로 준다.
4. `GET /api/cameras/coverage-guides`(관리자)는 현재 보정 버전의 `coverage_guide` 행을 준다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 같은 버전의 상태 변경 | DRIFTED 표시는 새 버전을 만들지 않는다(watch.py). 상태·이유·재투영 오차는 알림 기록에서 읽고 버전 행은 처음 값 그대로 둔다 | 버전은 덮어쓰지 않는다(AGENTS 6, 4.5). 알림 원문은 리플레이·감사 근거 |
| 순서 | Tier 0 의 `sent_at`, 같으면 받은 시각. 늦게 도착한 옛 알림은 기록만 되고 현재 상태가 되지 않는다 | 재전송(12.1)이 현재 상태를 뒤집지 않게. Tier 0 엔드포인트(registered_at)와 같은 방식 |
| 재전송 | 같은 `msg_id` 는 한 행(duplicate) | 명세 12.1 |
| 같은 버전 다른 해시 | 계약 위반으로 버린다(`result=invalid`, 연결 유지) | 버전은 덮어쓰지 않는다(4.5) |
| 해시 | `calibration_hash` 는 문서 원문 해시가 아니라 `content_hash`(버전·생성 시각·상태·계약 버전을 뺀 내용 해시)다. EC2 는 `CalibrationContentHash` 로 Python `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 와 같은 글자를 만들어 다시 잰다(실수는 Python repr 모양). 관리자 업로드(`POST /api/calibrations`)는 문서가 적은 `content_hash` 를 믿지 않고 다시 잰 값으로 버전 행과 맞춘다 | 같은 보정을 Tier 0 알림과 업로드 어느 쪽이 먼저 만들어도 같은 버전으로 보고, 다른 내용의 문서가 해시만 맞춰 붙지 못하게. 원문 해시(`calibration_document.sha256`)는 구성 묶음에 그대로 쓴다 |
| 예전 업로드 행 | 이 변경 전 업로드는 버전 행 해시를 원문 sha256 으로 남겼다. 해시가 다르면 올린 문서의 content_hash 를 다시 재서 같을 때만 해시 칸을 바로잡는다 | 내용은 같고 해시 표기만 틀린 행 |
| 이전 버전 | 업로드는 문서에 이전 버전이 없어 비워 둔다(추측하지 않는다). Tier 0 알림이 오면 채운다. EC2 가 모르는 이전 버전은 FK 때문에 비워 두고, 응답은 알림 값을 그대로 준다 | Tier 0 가 이전 버전의 기준이다 |
| 알림 없음 | `calibration_state` UNCALIBRATED, 나머지 null, `cameras` 빈 배열 | Tier 0 가 아직 보정을 알리지 않았다 |
| `document_uploaded`·`latest_uploaded_version` | 현재 버전의 보정 문서가 EC2 에 있는지, 가장 최근에 올린 문서의 버전(알림 전에 올렸어도) | 구성 묶음은 올린 문서가 있어야 만든다(`CALIBRATION_MISSING`). 화면이 이미 올린 문서를 다시 올리라고 하지 않게 |
| 설치 안내 범위 | 현재 보정 버전의 행. 보정 알림이 없으면 버전 없는 행. `camera_id` 가 null 이면 전체(걷기 경로 안내) | db-tables `coverage_guide` |
| 마이그레이션 번호 | V10. V9 는 S15P31A101-69 가 쓴다 | 두 브랜치가 같은 번호를 쓰면 Flyway 가 뜨지 않는다. -69 를 먼저 merge 한다 |

## 응답

`GET /api/calibrations/current`

```json
{
  "calibration_state": "DRIFTED",
  "calibration_version": "cal-8",
  "previous_calibration_version": "cal-7",
  "provider_source": "G1_AUTO",
  "reason": "DRIFT_DETECTED",
  "effective_at": 1760342401000,
  "updated_at": 1760342400400,
  "cameras": [{ "camera_id": "cam2", "reprojection_error_px": { "median": 3.4, "p95": 8.9 } }],
  "document_uploaded": false,
  "latest_uploaded_version": "cal-7"
}
```

`updated_at` 은 이 상태를 알린 `calibration_update` 의 `sent_at`(Tier 0 시계)이다.

`GET /api/cameras/coverage-guides`

```json
{
  "calibration_version": "cal-8",
  "guides": [
    { "guide_id": 3, "camera_id": "cam3", "message": "카메라 3번이 너무 아래를 봄", "metrics": { "floor_coverage_ratio": 0.42 }, "created_at": 1760342402000 }
  ]
}
```

## 시험

- `CalibrationContentHashTest`: 계약 예시 보정 문서 4개와 지수형 실수·한글·제어 문자·키 순서 문서의 해시가 Python 으로 잰 값과 같다.
- `CalibrationStatusIntegrationTest`(실제 포트, I1 + REST): 알림 → 현재 상태·버전·카메라별 오차, 같은 버전 DRIFTED 와 늦게 온 옛 알림(버전 행은
  그대로), 재보정 새 버전과 이전 버전 참조, 재전송·같은 버전 다른 해시·계약 위반(SIM 인데 CALIBRATED) 버림, 같은 내용 문서 업로드는 받고 다른
  내용은 content_hash 를 맞춰 적어도 409, 업로드로 만든 버전에 알림이 이전 버전을 채움, 예전 원문 해시 행 바로잡기, 설치 안내는 현재 버전만·관리자만.

## 남은 일

- 설치 안내(`coverage_guide`)를 채우는 길이 아직 없다. 커버리지 계산은 자동 보정(걷기 보정, M1 `calibration_run`·`calibration_ba`
  작업)에서 나오는데, 그 결과를 EC2 로 보내는 계약(I1 메시지나 I5 작업 결과의 필드)이 없다. 계약 담당과 정해야 한다. 그때까지 조회는 빈 목록이다.
- 보정 시작·검증·확정(`POST /api/calibrations` 의 걷기 보정 시작, verifications, confirmation)은 M1 이다.
