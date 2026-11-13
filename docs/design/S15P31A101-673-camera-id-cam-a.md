# S15P31A101-673 카메라 ID cam_A~cam_D 통일 (V2 시드·계약 예시·목 데이터)

관련 명세·규칙: 명세 3.3 카메라 장비(`docs/spec/03-system.md`), 11.6 계정·기기·분석 작업(`docs/spec/11-data.md`), 12.3 I1 주요 메시지·12.7 I0 프레임 식별(`docs/spec/12-interfaces.md`), 16장 결정 기록(`docs/spec/16-decisions.md`), `docs/design/db-erd.md`, `docs/design/db-tables.md`, `docs/design/db-verification.md`.

## 배경과 문제

- 실측 녹화 세션(`docs/data/sessions/`), 게이트웨이(`gateway/recorder/sessions/classroom.toml`), AI 보정(`CAMS = ("cam_A", "cam_B", "cam_C", "cam_D")`), 운영 DB(`lobby` 현장의 `cam_A`~`cam_D`), 프론트 표시(`cameraLabels.ts` `cam_A` → `카메라 A`)는 모두 카메라 ID 로 `cam_A`~`cam_D` 를 쓴다.
- 반면 초기 시드(`V2__seed_site_camera.sql`), 계약 예시(`contracts/examples/`), 프론트 목 데이터·개발 하네스, AI LIVE 설정 기본값(`ai/services/tier0/settings.py`), 서버 시험 픽스처에는 초기에 적어 둔 `cam1`~`cam4` 가 남아 있어 시드 DB·계약 예시와 실측·운영 카메라 ID 가 어긋났다.

## 결정과 변경 범위

1. **계약 스키마 무변경 (`contracts/**/*.schema.json`)**
   - `camera_id` 스키마(`^[A-Za-z0-9_-]{1,32}$`)는 `cam_A`~`cam_D` 와 `cam1`~`cam4` 를 모두 허용하므로 스키마 파일과 계약 버전은 바꾸지 않는다.
   - `contracts/examples/` 아래 정식 예시 및 `invalid/` 반례 JSON(190여 개)의 `cam1`~`cam4`(와 로컬 트랙 ID `cam1-t7` 등, 객체 키 `cam1-...`)를 `cam_A`~`cam_D` 로 통일했다.

2. **DB 마이그레이션 (`V69__seed_camera_id_cam_a.sql`)**
   - 이미 적용된 `V1__init.sql`·`V2__seed_site_camera.sql` 은 Flyway 체크섬이 깨지므로 수정하지 않는다.
   - `server/src/main/resources/db/migration/V69__seed_camera_id_cam_a.sql` 을 추가해:
     - `camera.camera_id` 컬럼 주석을 `'예: cam_A'` 로 갱신한다.
     - `site_id = 'classroom-1'` 에 `cam1`~`cam4` 가 있고, `cam_A`~`cam_D` 가 아직 없으며, 그 카메라를 참조하는 기록이 없을 때만 `cam1`~`cam4` 를 `cam_A`~`cam_D` 로 갱신한다. 참조 확인 대상은 `camera` 를 FK 로 가리키는 표(`calibration_camera`, `coverage_guide`)와 `camera_id` 열이 있는 표(`media_clip`, `video_segment`, `system_metric`, `setup_session_artifact`, `setup_zone_opinion`) 전부다(빈 DB 의 information_schema 로 확인, Lead 2026-11-13).
     - 운영 DB 처럼 이미 `lobby` + `cam_A`~`cam_D` 만 있거나 과거 기록이 `cam1`~`cam4` 를 참조하는 환경은 건드리지 않는다.

3. **서버 (`server/`)**
   - `SetupPairHoldout.java`, `VideoController.java` 의 예시 주석과 `server/src/test/resources/db/v1-smoke.sql` 및 서버 통합·단위 테스트 픽스처(17개 테스트 파일)를 `cam_A`~`cam_D` 기준으로 맞췄다.
   - `CameraSeedMigrationIntegrationTest` 로 (1) 빈 시드 DB 에서 `classroom-1` 의 카메라가 `cam_A`~`cam_D` 로 옮겨지는지, (2) 참조 기록이 있을 때 `cam1` 행을 지우거나 바꾸지 않고 보존하는지 검증한다.

4. **프론트엔드 (`front/`)**
   - `front/apps/admin/src/` 의 목 데이터(`controlMocks.ts`, `dashboardMocks.ts`, `CalibrationPage.tsx`, `FloorPlanCard.tsx`), 개발 하네스(`dev-harness/`), 관련 단위 시험의 카메라 ID 를 `cam_A`~`cam_D` 로 통일했다.
   - `cameraLabels.ts`·`setupFailures.ts` 와 그 단위 시험(`cameraLabels.test.ts`, `setupFailures.test.ts`)에 있는 레거시 `cam1`~`cam4` 파싱 호환 분기(`LEGACY_IDS`)는 하위 호환 검증을 위해 의도적으로 유지했다.

5. **AI (`ai/`) 및 문서 (`docs/`)**
   - `ai/services/tier0/settings.py` 의 LIVE 모드 기본 `camera_ids` 를 `["cam_A", "cam_B", "cam_C", "cam_D"]` 로 바꾸고, CLI·서비스·보정 모듈(`intrinsics/cli.py`, `extrinsics/initial.py`, `endpoint_registration.py`, `video_ingest.py`)의 주석·예시를 `cam_A`~`cam_D` 로 맞췄다.
   - 계약 예시(`contracts/examples/`)를 읽는 시험(`test_tier0_log.py`, `test_bev_floorplan.py`, `test_site_setup.py`, `test_site_setup_supplement.py`, `test_vlm_rationale.py`)과 `test_sim_channel_isolation.py` 의 카메라 ID 기대값을 `cam_A`~`cam_D` 로 맞췄다.
   - `docs/spec/11-data.md`, `12-interfaces.md`, `16-decisions.md` 와 `docs/design/db-erd.md`, `db-tables.md`, `db-verification.md` 의 시드·예시 표기를 `cam_A`~`cam_D` 로 갱신했다.

## 의도적으로 유지한 곳 (건드리지 않은 범위)

- **적용 완료된 Flyway 마이그레이션 (`V1__init.sql`, `V2__seed_site_camera.sql`)**: Flyway 체크섬 무결성을 위해 원본 유지, `V69` 로 이관.
- **프론트엔드 레거시 카메라 번호 파서 호환 시험 (`cameraLabels.ts`, `setupFailures.ts` 및 해당 테스트)**: 과거 로그나 외부 입력에서 `cam1`~`cam4` 가 들어올 때 `카메라 A`~`D` 로 변환하는 하위 호환 로직과 그 시험(`LEGACY_IDS`).
- **독립 합성(SIM)·단위 시험 픽스처**: 계약 예시나 DB 시드를 읽지 않고 임의 카메라 키(`cam1`, `cam2`)만 쓰는 순수 단위·합성(SIM) 시험 픽스처.
- **과거 시점 설계·실험 기록 문서 (`docs/design/`, `docs/experiments/`)**: 당시 실험·설계 기록 보존을 위해 DB 기준 문서(`db-erd.md`, `db-tables.md`, `db-verification.md`)만 갱신하고 과거 티켓 문서는 그대로 두었다.

## 검증

- `uv run contracts/scripts/validate_examples.py` 통과
- `cd ai && uv run pytest tests/contracts -q` 및 `uv run python ../contracts/scripts/generate_python_models.py --check` 통과
- `cd ai && uv run pytest` 관련 워커·Tier 0·계약 시험 통과
- `cd front && pnpm format:check && pnpm lint && pnpm typecheck && pnpm test` 통과
- `cd server && sh gradlew build --no-daemon` 통과

## 잔여 위험과 넘긴 일

- 잔여 위험 없음: 계약 정규식(`^[A-Za-z0-9_-]{1,32}$`)과 양쪽 파서가 모두 유지되므로 기존 데이터나 외부 입력 호환성이 깨지지 않는다.
- 넘긴 일 없음.
