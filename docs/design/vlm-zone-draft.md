# VLM 위험구역 초안 (`vlm_zone_draft`) 구현 설계

- **티켓**: `S15P31A101-455`
- **관련 명세 절**:
  - `docs/spec/05-processing.md` (5.3 VLM 보조 판단 및 현장 세팅 위험구역 초안 제안)
  - `docs/spec/08-zone-rule.md` (8.2 위험구역 종류 `OPENING`, `FLOOR_EDGE`, `ELEVATED_WORK_SURFACE`, `HAZARD_AREA` 및 게시 전 초안 규칙)
  - `docs/spec/12-api-ws.md` (12.5 현장 세팅 세션 및 I5 비동기 작업)
- **계약 버전**: `contracts` `v7.6.0` (`vlm_zone_draft` `1.0.0`, `job_request` `3.8.0`, `job_callback` `2.7.0`)

---

## 1. 코드 및 시험 위치

| 구분 | 경로 | 역할 |
|---|---|---|
| 구간 계약 | `contracts/messages/vlm_zone_draft.schema.json` | `vlm_zone_draft` I5 요청(`$defs/input`)·결과(`$defs/result`)·기하 후보(`$defs/geometry_candidate`)·초안 항목(`$defs/draft_item`) 정본 스키마 |
| 구간 계약 | `contracts/messages/job_request.schema.json` (`3.8.0`), `contracts/messages/job_callback.schema.json` (`2.7.0`) | `job_type: "vlm_zone_draft"` (`priority: "BACKGROUND"`) 분기 추가 |
| 계약 예시 | `contracts/examples/job_request.vlm_zone_draft.json`, `contracts/examples/job_callback.vlm_zone_draft.json`, `contracts/examples/job_callback.vlm_zone_draft_no_opinion.json` | 정상 요청·성공 콜백·VLM 미응답(`NO_OPINION`) 콜백 예시 및 `invalid/` 반례 3종 |
| AI 기하·VLM | `ai/src/simai_ai/vlm/zone_draft.py` | 3D 점군/메시 기하 후보 결정론적 추출(`extract_geometry_candidates`), 카메라 투영 크롭(`select_candidate_crops`), Cosmos3-Nano 프롬프트·응답 검증(`assess_geometry_candidates`) |
| AI 얼굴 게이트 | `ai/src/simai_ai/vlm/face_blur.py` | `VlmFrame.with_crop(x1, y1, x2, y2)` — 이미 얼굴 비식별(`BLURRED`/`EXCLUDED`) 처리된 `VlmFrame` 픽셀만 슬라이스하여 크롭 생성 |
| AI 워커 핸들러 | `ai/services/worker/vlm_zone_draft.py`, `ai/services/worker/__main__.py`, `ai/services/worker/client.py` | `VlmZoneDraftHandler` I5 핸들러 등록 및 오브젝트 스토리지 점군/보정/프레임 로드 |
| AI 단위 시험 | `ai/tests/vlm/test_zone_draft.py`, `ai/tests/services/worker/test_vlm_zone_draft.py` | 평탄 바닥 빈 배열, 작업대·개구부·단차 추출, 얼굴 게이트 강제, VLM 실패 시 `"AI 의견 없음"` 폴백 시험 |
| 서버 마이그레이션 | `server/src/main/resources/db/migration/V34__setup_zone_draft.sql` | `setup_zone_draft_run`, `setup_zone_draft` append-only 테이블(`UNIQUE (setup_session_id, preview_scene_version)`) |
| 서버 도메인 | `server/src/main/java/com/ssafy/simai/domain/setup/SetupService.java`, `SetupController.java` | 프리뷰 맵 준비 시 `vlm_zone_draft` 자동 발행, 콜백 저장, `GET /api/setup-sessions/{id}/zone-drafts` 조회, 실패 격리 |
| 서버 통합 시험 | `server/src/test/java/com/ssafy/simai/domain/setup/SetupZoneDraftIntegrationTest.java` | 프리뷰 준비 전/후 상태, 중복 발행 방지, 계약 검증, `NO_OPINION` 및 작업 실패 격리 통합 시험 |

---

## 2. 핵심 불변식 준수

1. **Tier 0 판정 미투입 (`safety_use: false`, AGENTS.md 규칙 1·4)**
   - 모든 초안 응답과 개별 초안 항목은 `safety_use: false` 상수로 고정되며, 관리자가 확인·수정 후 `POST /api/zones/commit`으로 게시하기 전까지 Tier 0 위험 판정에 절대 사용되지 않는다.
2. **근거 없는 폴리곤 생성 금지 ("모르면 비운다", AGENTS.md 규칙 2)**
   - 3D 점군·메시가 비어 있거나 바닥(`Z ≈ 0`)만 있는 평탄한 점군이면 VLM을 호출하지 않고 즉시 `drafts: []`를 반환한다.
   - 폴리곤(`footprint_polygon_xy`)과 3D OBB(`obb_3d`)는 오직 3D 기하 후보 추출기(`extract_geometry_candidates`)가 계산한 미터 좌표만 사용하며, VLM이 임의 좌표를 생성하지 못한다.
3. **얼굴 비식별 게이트 및 인물 식별자 금지 (AGENTS.md 규칙 8)**
   - 요청 스키마는 `face_policy: "BLURRED" | "EXCLUDED"`만 허용하고 `person_id`·`person_name`·`worker_name` 필드를 스키마 차원에서 차단한다.
   - 워커는 `gate_input_frames`로 얼굴 블러 게이트를 통과한 `VlmFrame`에서만 `VlmFrame.with_crop`으로 후보 투영 영역을 잘라 VLM에 전달한다.
4. **심각도 등급 및 폴백 규칙 (AGENTS.md 규칙 4)**
   - 구역 기본 심각도 추천(`recommended_severity`)은 `CRITICAL` 또는 `HIGH`만 허용한다(`CAUTION` 금지).
   - VLM 미주입·타임아웃·파싱 실패 시 작업을 실패시키는 대신 결정론적 기하 후보를 유지한 채 `status: "NO_OPINION"`, `vlm_status: "NO_OPINION"`, `rationale: "AI 의견 없음"`, `model_version: null`로 응답한다.

---

## 3. 명세에 없어 정한 수치와 근거

### 3.1 3D 기하 후보 추출 (`ZoneGeometryConfig`)

| 파라미터 | 기본값 | 설정 근거 |
|---|---|---|
| `grid_res_m` | `0.10 m` | 10 cm XY 격자. 교실 내 작업대(폭 0.6~1.5 m)와 개구부(폭 0.4 m 이상)의 경계를 과도한 노이즈 없이 보존하는 해상도 |
| `floor_z_Band_m` | `±0.12 m` | 바닥 `Z=0` 기준 ±12 cm 이내를 바닥 지지면으로 간주(바닥 보정 잔차 및 카펫·케이블 단차 허용) |
| `min_elevated_z_m` / `max_elevated_z_m` | `0.25 m` / `2.20 m` | 발판·작업대 상판 하한(25 cm)부터 천장·조명 구조물 제외 상한(2.2 m)까지를 상부 작업면(`ELEVATED_WORK_SURFACE`) 후보로 분류 |
| `max_surface_z_std_m` | `0.08 m` | 셀 내부 및 클러스터 상판의 평탄도 기준(표준편차 8 cm 이하)으로 벽면·기둥을 배제하고 수평 상판만 추출 |
| `min_surface_area_m2` | `0.20 m²` | 사람 두 발이 올라설 수 있는 최소 상판 면적(예: 0.45 m × 0.45 m) |
| `min_opening_depth_m` | `0.15 m` | 함몰 점(`Z <= -0.15 m`)이 관측될 때 개구부(`OPENING`)로 판정하는 최소 깊이 |
| `min_opening_area_m2` / `max_opening_area_m2` | `0.12 m²` / `6.0 m²` | 소형 센서 노이즈 공백(< 0.12 m²)과 카메라 미관측 대형 사각지대(> 6.0 m²)를 배제하고 실제 바닥 내부 개구부만 추출 |
| `min_edge_length_m` | `0.60 m` | 상승 평면 외곽 단차(`FLOOR_EDGE`) 후보의 최소 유효 길이 |
| `max_candidates` | `8` | 현장 세팅 화면에서 관리자가 한눈에 검토 가능한 최대 초안 개수 및 VLM 호출 지연 상한 관리 |

### 3.2 Cosmos3-Nano (`nvidia/Cosmos-Reason2-2B`) 프롬프트 및 응답 규약

- **프롬프트 버전**: `vlm-zone-draft-1.0`
- **입력 구성**: 기하 후보 1건당 결정론적으로 계산된 종류(`candidate_kind`), 면적(`area_m2`), 상판 높이(`top_height_m`), 단차 깊이(`drop_depth_m`), 중심 좌표(`center_xyz`) 요약 텍스트 + 얼굴 블러를 거친 최대 2장의 카메라 투영 크롭(`crop_pad_px = 32`).
- **출력 스키마(`RESPONSE_FORMAT`)**:
  - `recommended_name`: 한국어 구역 이름(2~60자)
  - `recommended_zone_kind`: `OPENING | FLOOR_EDGE | ELEVATED_WORK_SURFACE | HAZARD_AREA`
  - `recommended_severity`: `CRITICAL | HIGH`
  - `rationale`: 1~2문장 한국어 추천 이유(5~240자)

### 3.3 서버 발행 및 실패 격리

- `SetupService.applySnapshot`에서 `calibration_version`과 `preview_scene_version`이 모두 준비되고 세션 상태가 `PREVIEW_AND_VLM_DRAFT`, `READY_FOR_ZONES`, `BAKING_3DGS`, `COMPLETED` 중 하나일 때 `PROPAGATION_NESTED`(Savepoint) 안에서 `vlm_zone_draft` 작업을 자동 발행한다.
- `setup_zone_draft_run` 테이블의 `UNIQUE (setup_session_id, preview_scene_version)` 제약으로 동일 프리뷰 맵 버전에 대해 중복 발행되지 않는다.
- `vlm_zone_draft` 워커 부재나 작업 실패(`FAILED`/`TIMED_OUT`)는 `setup_zone_draft_run.status`에만 기록되며 `setup_session`의 진행·완료를 절대 실패시키지 않는다.

---

## 4. 버린 대안

1. **VLM에게 이미지 전체를 주고 2D/3D 폴리곤 좌표를 직접 예측하게 하는 방식**
   - **기각 사유**: VLM이 예측한 픽셀/3D 좌표는 다중 뷰 기하 정합성이 보장되지 않아 허공이나 엉뚱한 바닥에 위험구역을 그릴 위험이 크다(AGENTS.md 규칙 1·2 위반). 기하 후보는 3D 점군에서 결정론적으로 추출하고 VLM은 이름·등급·이유만 제안하도록 분리했다.
2. **`site_setup` 완료를 `vlm_zone_draft` 완료까지 블로킹하는 방식**
   - **기각 사유**: VLM 추론 지연이나 일시 장애가 현장 세팅 자체를 가로막으면 안 된다. 별도 `BACKGROUND` I5 작업(`vlm_zone_draft`)과 중첩 트랜잭션 격리로 분리하여 세팅 진행과 독립적으로 초안을 조회(`GET /api/setup-sessions/{id}/zone-drafts`)하도록 설계했다.

---

## 5. 잔여 위험 및 넘긴 일

- **후속 티켓 연동 (`S15P31A101-453`)**:
  - 프런트엔드 현장 세팅 마법사 UI에서 `GET /api/setup-sessions/{id}/zone-drafts`를 폴링하여 초안 폴리곤을 점선(`safety_use: false`)으로 표시하고, 관리자가 수락·수정한 항목만 기존 `POST /api/zones/commit` 요청으로 변환해 게시한다.
- **12차 GPU 세션 측정 절차**:
  1. 교실 4카메라 `site_setup` 실행으로 생성된 `preview_scene_version` 점군(`.ply`/`.glb`)과 `calibration.json`에 대해 `vlm_zone_draft` 워커를 실행한다.
  2. 책상 상판(`ELEVATED_WORK_SURFACE`)과 바닥 개구부 모형(`OPENING`) 후보 추출 정밀도/재현율 및 `Cosmos-Reason2-2B` 후보당 추론 지연(목표: 후보당 1.5 s 이하, 전체 8 후보 이내 10 s 이하)을 `run_record.schema.json` 형식으로 기록한다.
