# EC2 카메라 각도 안내 중계 (`POST /api/devices/coverage-guides`, `GET /api/cameras/coverage-guides`)

- **티켓**: `S15P31A101-447`
- **관련 명세**: 3.3(영상 수신 및 세팅 1단계 헬퍼), 4.3(초기 세팅 V2), 4.5(보정 상태·설치 안내), 12.2(`messages/coverage_guide` 1.0.0), 12.6(REST API), 13.4(관제 화면 세팅 1단계 카메라 각도 헬퍼), 18.1(현장 설치 순서), 요구사항 FR-26·FR-141
- **연관 설계**: `docs/design/site-setup-pipeline.md`, `docs/design/server-calibration-status.md`
- **계약**: `contracts/messages/coverage_guide.schema.json` (1.0.0, 계약 v7.3.0)
- **코드 위치**:
  - `server/src/main/java/com/ssafy/simai/domain/coverage/CoverageGuideController.java`
  - `server/src/main/java/com/ssafy/simai/domain/coverage/CoverageGuideService.java`
  - `server/src/main/java/com/ssafy/simai/domain/coverage/CoverageGuideProperties.java`
  - `server/src/main/java/com/ssafy/simai/global/exception/ErrorCode.java` (`ErrorCode.CoverageGuide`)
- **시험 위치**:
  - `server/src/test/java/com/ssafy/simai/domain/coverage/CoverageGuideIntegrationTest.java`

---

## 1. 개요 및 흐름

1. **게이트웨이 → EC2 (`POST /api/devices/coverage-guides`, 기기 토큰 `ROLE_DEVICE`)**:
   - 현장 노트북 게이트웨이의 별도 CPU 프로세스(`gateway/camera_helper`, `-446`)가 세팅 1단계 카메라 각도 헬퍼 계산 결과(`contracts/messages/coverage_guide.schema.json` 1.0.0)를 올린다.
   - EC2 는 기기 토큰 인증(`MachineKind.DEVICE`) 뒤:
     1. 기기 종류가 `LAPTOP`(`device.kind = 'LAPTOP'`, `revoked_at IS NULL`)인지 확인한다(워치 등 비노트북 기기는 `403 FORBIDDEN`, `NOT_A_LAPTOP`).
     2. 현장이 유효한지(`SiteLookup.siteId()`) 확인한다.
     3. 서버의 기존 계약 검증기(`ContractSchemas.violations("messages/coverage_guide", rawJson)`)와 의미 불변식(`schema_version` 1.x, `computed_at_ms <= valid_until_ms`, 4대 고유 `camera_id` 및 6쌍·연결 성분·안내 참조 정합)으로 검증한다(위반 시 `400 BAD_REQUEST`, `INVALID_COVERAGE_GUIDE`).
     4. 본문 `source_id` 가 기기 토큰의 `deviceId` 와 일치하는지 확인한다(불일치 시 `400 BAD_REQUEST`, `COVERAGE_GUIDE_SOURCE_MISMATCH`).
   - 검증을 통과하면 **DB(`coverage_guide` 테이블)에 적재하지 않고 서버 메모리에 최신 결과 1건만 원자적으로(`AtomicReference`) 덮어쓴다**.
   - 응답 본문에 `active_demand`(최근 `demand-ttl` 초기 15 s 안에 관리자 조회가 있었는지)를 실어 보내 게이트웨이 헬퍼(`-446`)가 세팅 1단계 화면이 열려 있을 때만 계산을 켜고 닫히면 끄도록 한다.

2. **관리자 → EC2 (`GET /api/cameras/coverage-guides`, 관리자 `ROLE_ADMIN`)**:
   - 관제 앱(`-448`)이 세팅 1단계 화면에서 폴링할 때마다 `lastAdminQueryAt` 시각을 갱신해 `active_demand` 를 켠다.
   - 메모리에 보관된 최신 `coverage_guide` 를 `latest` 필드에 담아 반환한다.
   - 최신 결과 수신 시각(`received_at_ms`)으로부터 `stale-after`(초기 10 s)가 넘었으면 `status: "STALE"`, `stale: true` 로 표시한다(10 s 이내는 `status: "OK"`, `stale: false`, 아직 수신 전이면 `status: "EMPTY"`, `stale: false`, `latest: null`).
   - 기존 보정 버전별 안내 조회(`calibration_version`, `guides` 배열 — `CalibrationViews.coverageGuides()`) 필드를 함께 유지해 하위 호환을 보장한다.

---

## 2. 요청·응답 모양

### 2.1 `POST /api/devices/coverage-guides`

- **인증**: `Authorization: Bearer <device_token>` (`device.kind = 'LAPTOP'`)
- **요청 본문**: `contracts/messages/coverage_guide.schema.json` 1.0.0 그대로(닫힌 값 객체, `additionalProperties: false`).
- **응답 (`200 OK`)**:

```json
{
  "site_id": "classroom-1",
  "source_id": "gw-lobby",
  "received_at_ms": 1793690000120,
  "active_demand": true,
  "demand_ttl_ms": 15000,
  "last_admin_query_at_ms": 1793690000000,
  "demand_until_ms": 1793690015000
}
```

- 관리자 조회가 아직 없었으면 `active_demand: false`, `last_admin_query_at_ms: null`, `demand_until_ms: null` 이다.
- **오류 응답 (`ProblemDetail`)**:
  - `401 Unauthorized`: 기기 토큰 없음·만료·폐기·위조
  - `403 Forbidden` (`code: "NOT_A_LAPTOP"`): `WATCH` 등 `LAPTOP` 이 아닌 기기 토큰
  - `400 Bad Request` (`code: "INVALID_COVERAGE_GUIDE"`): JSON Schema(`messages/coverage_guide` 1.0.0) 위반 또는 카메라·쌍 참조 불변식 위반
  - `400 Bad Request` (`code: "COVERAGE_GUIDE_SOURCE_MISMATCH"`): 요청 `source_id` 가 토큰의 `deviceId` 와 다름

### 2.2 `GET /api/cameras/coverage-guides`

- **인증**: `Authorization: Bearer <admin_jwt>` (`ROLE_ADMIN`)
- **응답 (`200 OK`)**:

```json
{
  "calibration_version": "cal-8",
  "guides": [],
  "site_id": "classroom-1",
  "status": "OK",
  "stale": false,
  "active_demand": true,
  "demand_ttl_ms": 15000,
  "last_admin_query_at_ms": 1793690005000,
  "demand_until_ms": 1793690020000,
  "stale_after_ms": 10000,
  "received_at_ms": 1793690000120,
  "age_ms": 4880,
  "latest": {
    "schema_version": "1.0.0",
    "source_id": "gw-lobby",
    "computed_at_ms": 1793690000000,
    "valid_until_ms": 1793690010000,
    "capture_skew_ms": 18,
    "params": {
      "image_width_px": 1024,
      "max_keypoints": 1024,
      "ransac_threshold_px": 2.0,
      "enough_min_inliers": 30,
      "weak_min_inliers": 15,
      "min_inlier_ratio": 0.1,
      "intrinsics_source": "STORED"
    },
    "cameras": [
      { "camera_id": "cam_A", "status": "OK", "frame_ts_ms": 1793689999990 },
      { "camera_id": "cam_B", "status": "OK", "frame_ts_ms": 1793689999998 },
      { "camera_id": "cam_C", "status": "OK", "frame_ts_ms": 1793690000004 },
      { "camera_id": "cam_D", "status": "OK", "frame_ts_ms": 1793689999986 }
    ],
    "pairs": [
      { "a": "cam_A", "b": "cam_B", "grade": "ENOUGH", "raw_matches": 148, "inliers": 64, "inlier_ratio": 0.4324, "centroid_a": [0.71, 0.52], "centroid_b": [0.28, 0.50] },
      { "a": "cam_A", "b": "cam_C", "grade": "LACK", "raw_matches": 22, "inliers": 4, "inlier_ratio": 0.1818, "centroid_a": null, "centroid_b": null },
      { "a": "cam_A", "b": "cam_D", "grade": "WEAK", "raw_matches": 74, "inliers": 19, "inlier_ratio": 0.2568, "centroid_a": [0.84, 0.48], "centroid_b": [0.19, 0.46] },
      { "a": "cam_B", "b": "cam_C", "grade": "ENOUGH", "raw_matches": 132, "inliers": 51, "inlier_ratio": 0.3864, "centroid_a": [0.66, 0.55], "centroid_b": [0.33, 0.54] },
      { "a": "cam_B", "b": "cam_D", "grade": "LACK", "raw_matches": 18, "inliers": 2, "inlier_ratio": 0.1111, "centroid_a": null, "centroid_b": null },
      { "a": "cam_C", "b": "cam_D", "grade": "WEAK", "raw_matches": 80, "inliers": 21, "inlier_ratio": 0.2625, "centroid_a": [0.79, 0.62], "centroid_b": [0.16, 0.64] }
    ],
    "connectivity": "DISCONNECTED",
    "components": [["cam_A", "cam_B", "cam_C"], ["cam_D"]],
    "guides": [
      {
        "camera_id": "cam_D",
        "direction": "LEFT",
        "magnitude": "SMALL",
        "reason": "WEAK_PAIR_EDGE_CLUSTER",
        "target_pair": ["cam_C", "cam_D"]
      }
    ]
  }
}
```

---

## 3. 명세에 없어 정한 수치와 근거

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 결과 노후화 기준 (`simai.coverage-guide.stale-after`) | 초기 `10s` (`SIMAI_COVERAGE_GUIDE_STALE_AFTER`) | `coverage_guide.schema.json` 의 기본 TTL(`computed_at_ms + 10000`)과 일치. 서버 수신 시각(`receivedAt`) 기준으로 측정해 게이트웨이-EC2 시계 오차나 고정 예시 시각에 영향받지 않게 함 |
| 관리자 수요 유지 시간 (`simai.coverage-guide.demand-ttl`) | 초기 `15s` (`SIMAI_COVERAGE_GUIDE_DEMAND_TTL`) | 관제 화면이 1~3 s 주기로 폴링할 때 일시적 네트워크 지연이나 탭 새로고침에도 게이트웨이 헬퍼가 깜빡이지 않고 유지되며, 화면을 닫으면 15 s 뒤 계산을 멈추게 함(`cctv-remote.demand-linger: 15s` 와 동일 기준) |
| 상태 값 (`status` / `stale`) | `EMPTY`(수신 전), `OK`(10 s 이내), `STALE`(10 s 초과) 및 불리언 `stale` | 프론트엔드(`-448`)가 문자열 상태와 불리언 플래그 중 어느 쪽으로 읽어도 일관되게 동작하며, `STALE` 일 때도 마지막 `latest` 본문을 유지해 화면에서 흐리게 표시할 수 있게 함 |
| `latest` 불변 유지 | `latest` 내부에는 서버 필드를 덧붙이지 않음 | `contracts/messages/coverage_guide.schema.json` 이 모든 객체를 `additionalProperties: false` 로 닫았으므로 `latest` 자체는 항상 계약 1.0.0 원문 그대로 유지 |

---

## 4. 버린 대안

1. **`POST /api/devices/coverage-guides` 마다 DB `coverage_guide` 테이블에 INSERT**:
   - 세팅 1단계 동안 1~2 s 주기로 올라오는 일회성 조준 보조 상태를 DB 에 적재하면 불필요한 쓰기 부하와 쓰레기 행이 쌓인다. 세팅 1단계 헬퍼는 현재 화면 조준용이므로 메모리에 최신 1건만 유지한다.
2. **게이트웨이 수요 전달을 위해 별도 WebSocket 채널 신설**:
   - 헬퍼가 주기적으로 `POST /api/devices/coverage-guides` 를 호출하므로 POST 응답 본문의 `active_demand` 로 수요를 돌려주는 것이 연결·인증 복잡도를 늘리지 않고 가장 단순하다.

---

## 5. 잔여 위험 및 넘긴 일

- **게이트웨이 헬퍼(`-446`) 연동**: 게이트웨이는 유휴 상태에서 느린 주기(또는 시작 직후 1회 probe)로 POST 응답의 `active_demand` 를 확인해 `true` 일 때만 ALIKED+LightGlue 매칭 주기를 켜고 `false` 가 되면 계산을 멈춘다.
- **관제 화면(`-448`) 연동**: 세팅 1단계 화면이 열려 있는 동안 `GET /api/cameras/coverage-guides` 를 폴링해 `status`/`stale` 과 `latest` 를 표시하고, 미충족(`DISCONNECTED`/`UNKNOWN`/`STALE`)이어도 **"그대로 진행"** 버튼으로 2단계(`POST /api/setup-sessions`) 진행을 막지 않는다.
