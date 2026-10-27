# EC2 보정 상태·설치 안내 조회

S15P31A101-263. 명세 4.4(DRIFTED), 4.5(보정 버전·설치 안내), 12.2(I1 `calibration_update`), 12.6(`GET /api/calibrations/current`,
`GET /api/cameras/coverage-guides`), FR-25·26, 계약 `calibration_update` 2.x.
코드는 `server/.../domain/calibration/`(`CalibrationUpdateIngestor`·`CalibrationViews`·`CalibrationController`). 보내는 쪽은
`ai/src/simai_ai/calibration/watch.py`(`calibration_update_payload`).

## 흐름

1. Tier 0 가 보정 상태나 버전이 바뀔 때 I1 로 `calibration_update` 를 보낸다. EC2 는 계약 스키마 전체로 검사하고
   `calibration_update_message` 에 한 행을 추가한다(V9).
2. 처음 보는 `calibration_version` 이면 `calibration_version` 행을 만든다. 이미 있으면 같은 `calibration_hash` 일 때만, 그 버전의
   가장 늦은 알림(sent_at)으로 `state`·`reason`·`effective_at`·`calibration_ref`·`operating_reprojection` 을 바꾼다.
3. `GET /api/calibrations/current`(열람)는 `sent_at` 이 가장 늦은 알림의 상태·버전·이유·카메라별 운영 중 재투영 오차를 준다.
4. `GET /api/cameras/coverage-guides`(관리자)는 현재 보정 버전의 `coverage_guide` 행을 준다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 같은 버전의 상태 변경 | DRIFTED 표시는 새 버전을 만들지 않으므로(watch.py) 버전 행의 상태 칸을 바꾸고, 알림은 별도 표에 추가만 한다 | 버전 행만으로는 순서·재전송을 가릴 수 없다. 원문은 리플레이·감사 근거로 남긴다 |
| 순서 | Tier 0 의 `sent_at`, 같으면 받은 시각. 늦게 도착한 옛 알림은 기록만 하고 상태를 되돌리지 않는다 | 재전송(12.1)이 현재 상태를 뒤집지 않게. Tier 0 엔드포인트(registered_at)와 같은 방식 |
| 재전송 | 같은 `msg_id` 는 한 행(duplicate) | 명세 12.1 |
| 같은 버전 다른 해시 | 계약 위반으로 버린다(`result=invalid`, 연결 유지) | 버전은 덮어쓰지 않는다(4.5) |
| 해시 맞추기 | `calibration_hash` 는 문서 원문 해시가 아니라 `content_hash`(버전·시각·상태를 뺀 내용 해시, ai `calibration/document.py`)다. 관리자 업로드(`POST /api/calibrations`)도 문서의 `content_hash` 로 버전 행과 맞추고, 없으면 원문 해시로 대신한다 | 같은 보정을 Tier 0 알림과 업로드 어느 쪽이 먼저 만들어도 같은 버전으로 본다. 원문 해시(`calibration_document.sha256`)는 구성 묶음에 그대로 쓴다 |
| 이전 버전 | EC2 가 모르는 이전 버전은 버전 행에서 비워 두고(FK), 응답은 Tier 0 가 보낸 값을 그대로 준다 | 이전 버전 알림을 받기 전일 수 있다 |
| 알림 없음 | `calibration_state` UNCALIBRATED, 나머지 null, `cameras` 빈 배열 | Tier 0 가 아직 보정을 알리지 않았다 |
| `document_uploaded` | 현재 버전의 보정 문서가 EC2 에 있는지 | 구성 묶음은 올린 문서가 있어야 만든다(`CALIBRATION_MISSING`). 관리자 화면이 업로드를 안내한다 |
| 설치 안내 범위 | 현재 보정 버전의 행. 보정 알림이 없으면 버전 없는 행. `camera_id` 가 null 이면 전체(걷기 경로 안내) | db-tables `coverage_guide` |

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
  "document_uploaded": false
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

`CalibrationStatusIntegrationTest`(실제 포트, I1 + REST): 알림 → 현재 상태·버전·카메라별 오차, 같은 버전 DRIFTED 와 늦게 온 옛 알림,
재보정 새 버전과 이전 버전 참조, 재전송·같은 버전 다른 해시·계약 위반(SIM 인데 CALIBRATED) 버림, Tier 0 가 알린 버전에 같은
`content_hash` 문서 업로드는 받고 다른 내용은 409, 설치 안내는 현재 버전만·관리자만.

## 남은 일

- 설치 안내(`coverage_guide`)를 채우는 길이 아직 없다. 커버리지 계산은 자동 보정(걷기 보정, M1 `calibration_run`·`calibration_ba`
  작업)에서 나오는데, 그 결과를 EC2 로 보내는 계약(I1 메시지나 I5 작업 결과의 필드)이 없다. 계약 담당과 정해야 한다. 그때까지 조회는 빈 목록이다.
- 보정 시작·검증·확정(`POST /api/calibrations` 의 걷기 보정 시작, verifications, confirmation)은 M1 이다.
