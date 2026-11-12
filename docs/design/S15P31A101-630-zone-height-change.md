# 세팅 구역별 상면 높이 차이 판정 (S15P31A101-630)

`-629` 새 모델 + World 이어짐 세팅의 구역 확인(`docs/design/S15P31A101-629-setup-zone-confirm.md`) 후속이다.
관련 명세:
- **3.2** 작업 시작 전 세팅 (`docs/spec/03-architecture.md`)
- **8.2** 위험 구역 (`docs/spec/08-zones-and-surfaces.md`)
- **9.1** 정적 3D 배경 생성 및 세팅 아티팩트 (`docs/spec/09-static-3d.md`)
- **12.6** 세팅 세션 REST (`docs/spec/12-api.md`)

앞선 약속:
- `-612` 세션 구역 목록 (`docs/design/setup-session-zones.md`)
- `-614` 이전 모델 재사용 및 World 다시 맞추기 (`docs/design/S15P31A101-614-work-setup-flow.md`)
- `-615` 세팅 완료 C17 (`docs/design/S15P31A101-615-setup-complete.md`)
- `-629` 새 모델 + World 이어짐 구역 확인 (`docs/design/S15P31A101-629-setup-zone-confirm.md`)

---

## 1. 왜 필요한가

새 모델(`NEW`) 세팅에서 World 다시 맞추기(`world_anchor.accepted = true`)가 통과하면 이전 관제 구역을 세션 구역 목록에 `needs_review = true` 로 채운다(`-629`).
그러나 구역이 여러 개일 때 관리자가 어느 구역에 자재 적치·단상 이동·개구부 덮개 변화가 생겼는지 맨눈으로 먼저 찾기 어렵다.

따라서 이전 관제 Scene GLB와 이번 세팅 Scene GLB(미리보기·정식·다시 굽기)의 구역 바닥 영역 상면 높이를 비교해 구역마다 **「변경 의심(`CHANGE_SUSPECTED`) / 변화 없음(`UNCHANGED`) / 모름(`UNKNOWN`)」** 을 판정·표시한다.
- **사람 확인 원칙 유지(`-629`)**: 판정은 확인 순서와 묶음 확인을 돕는 참고 표시일 뿐, 서버가 `needs_review` 를 자동으로 풀거나 위험 판정(Tier 0)에 넣지 않는다(AGENTS 규칙 1·4).
- **모르면 비운다(AGENTS 규칙 2·7)**: World가 이어지지 않았거나, 이전 Scene이 없거나, 이전 모델 재사용(`REUSE_SCENE`)이거나, 구역 안 관측 격자가 부족하면 수치 필드를 지어내지 않고(`null`) `UNKNOWN` 과 사유(`reason`)만 기록한다.

---

## 2. 비교 위치와 버린 대안

| 대안 | 결정 | 근거 |
|---|---|---|
| **A. 워커(`ai/`)가 `site_setup` 중에 비교해 진행·결과 스냅샷에 싣기** | **채택** | 워커는 이미 이번 세팅의 미리보기 GLB(`preview_scene.glb`)와 정식 GLB(`scene.glb`)를 로컬 작업 폴더에 갖고 있고, NumPy 기반 삼각형 격자 래스터화 도구를 갖추고 있다. 이전 관제 Scene GLB 1개만 객체 저장소에서 내려받으면 미리보기 직후(`READY_FOR_ZONES` 진입 전)와 정식 굽기(`COMPLETED`)·보강 다시 굽기(`SUPPLEMENT_REBAKE`)에서 일관되게 계산할 수 있다. |
| **B. 서버(`server/` Java)에서 두 GLB를 내려받아 직접 파싱·비교** | **버림** | 정식 GLB는 수십 MB 규모이며 EC2 Spring Boot 서버는 지금까지 GLB 본문을 내려받지 않고 객체 저장소 `HEAD`(크기·SHA-256)만 확인한다(`SetupArtifactRegistrar`). Java 서버에서 대형 GLB 두 벌을 내려받아 3D 메쉬 래스터화를 수행하면 세팅 하트비트/조회 트랜잭션 지연과 힙 메모리 부담이 크다. |
| **C. 브라우저(`front/`)에서 두 GLB를 모두 로드해 비교** | **버림** | 뷰어는 현재 선택된 Scene 1개만 그리며, 모바일/저사양 관리자 환경에서 이전 Scene GLB까지 이중 다운로드·래스터화하면 초기 진입이 느려지고 서버·후속 VLM 작업(`-631`)이 판정 결과를 재사용할 수 없다. |

---

## 3. 상면 높이 차이 계산 방식 (`ai/src/simai_ai/scene/zone_height_diff.py`)

1. **관측 표면만 추출 (`extract_observed_surface`)**
   - GLB의 `nodes`·`meshes`를 순회하며 glTF Y-up 정점·삼각형을 World Z-up(`X = x_gltf`, `Y = -z_gltf`, `Z = y_gltf`)으로 역변환한다(`GLTF_Y_UP.T`).
   - `glb_enhance.py`가 시각 보강용으로 덧붙인 추정 바닥 평면(`node.name == "estimated_floor_plane"` 또는 `extras.simai.surface_source == "ESTIMATED_PLANE"`)은 관측된 표면이 아니므로 **제외**한다(AGENTS 규칙 7).
2. **구역 다각형 내부 격자 생성 (`polygon_grid_mask`)**
   - 각 구역의 바닥 다각형(`polygon_xy`) 축 정렬 경계 상자(AABB)를 `cell_m = 0.10` m(10 cm) 간격 격자로 나누고, 칸 중심점 $(x_c, y_c)$가 다각형 내부(경계 포함)에 들어오는 칸을 구역 전체 칸(`total_cells`)으로 삼는다.
3. **칸별 상면 높이 $z_{\max}(x_c, y_c)$ 래스터화 (`rasterize_top_height_grid`)**
   - 수평 투영 면적이 퇴화하지 않은 삼각형($|n_z| \ge 0.15$)에 대해 무게중심 좌표(barycentric coordinates)로 칸 중심 $(x_c, y_c)$의 보간 높이 $z$를 구하고, 같은 칸에 여러 표면이 겹치면 가장 높은 상면 높이 $\max z$를 취한다.
   - 삼각형이 비어 있고 정점 포인트만 있는 경우 칸 안에 들어온 정점의 $\max z$를 쓴다.
4. **구역별 비교 및 판정 (`evaluate_zone_height_changes`)**
   - 이전 관제 Scene과 이번 세팅 Scene에서 **둘 다 관측된 칸**을 비교 칸(`compared_cells`)으로 센다.
   - 비교 칸 비율이 `min_coverage_ratio`(기본 `0.30`) 미만이거나 `compared_cells < min_compared_cells`(기본 `4`)이면 근거 부족으로 `status = "UNKNOWN"`, `reason = "INSUFFICIENT_COVERAGE"`, 수치 필드 `null`.
   - 충분한 비교 칸이 있으면 각 비교 칸의 높이 차이 $\Delta z = |z_{\text{curr}} - z_{\text{prev}}|$를 계산한다:
     - 변화 칸(`changed_cells`): $\Delta z \ge \text{height\_diff\_threshold\_m}$ (기본 `0.15` m = 15 cm) 인 비교 칸 수
     - 변화 면적 비율(`changed_area_ratio`): `changed_cells / compared_cells`
     - 최대 높이 차(`max_height_diff_m`) 및 그 칸 중심 World XY(`max_diff_xy`)
   - `changed_area_ratio >= changed_area_ratio_threshold`(기본 `0.10` = 10%)이면 **`CHANGE_SUSPECTED`**, 미만이면 **`UNCHANGED`**.

### 초기 기준값과 실제 로비 맵 재측정 절차

| 설정 항목 (`ZoneHeightDiffConfig`) | 초기값 | 의미 및 근거 |
|---|---|---|
| `cell_m` | `0.10` m (10 cm) | 사람 발바닥·소형 자재/박스를 잡으면서도 구역당 수백~수천 칸으로 수십 ms 안에 계산되는 격자 크기 |
| `height_diff_threshold_m` | `0.15` m (15 cm) | MapAnything + 바닥 맞춤의 일반 재구성 잡음(수 cm)을 넘고, 발판·적치물·개구부 덮개 단차(15 cm 이상)를 잡는 초기 문턱 |
| `changed_area_ratio_threshold` | `0.10` (10%) | 단일 스파이크 정점 몇 개로 구역 전체가 변경 의심으로 뜨는 오탐을 막기 위한 최소 면적 비율 |
| `min_coverage_ratio` | `0.30` (30%) | 가림이나 시야 밖으로 구역 바닥의 30% 미만만 관측된 경우 `UNCHANGED`로 오판하지 않고 `UNKNOWN`으로 두는 하한 |
| `min_compared_cells` | `4` 칸 | 아주 작은 구역에서 1~3칸만으로 비율을 계산하지 않도록 하는 최소 비교 칸 수 |

> **재측정 절차 (현장 로비 맵 2벌 확보 시)**:
> 1. 변화 없는 로비 세팅 GLB 2벌(같은 날 재촬영)과 박스를 옮긴 로비 세팅 GLB 1벌에 대해 `evaluate_zone_height_changes(...)`를 오프라인 실행한다.
> 2. 변화 없는 쌍의 구역별 95백분위 $\Delta z$ 분포에서 `height_diff_threshold_m`를(오탐 0건 기준), 박스 이동 구역의 면적 대비 `changed_area_ratio_threshold`를 다시 맞춘다.
> 3. 설정값은 `ZoneHeightDiffConfig` 기본값으로 한곳에 모여 있어 계약 스키마 변경 없이 조정할 수 있으며, 실행 당시 쓴 기준값(`cell_m`, `height_diff_threshold_m`, `changed_area_ratio_threshold`)은 `zone_height_changes` 요약에 함께 기록된다.

---

## 4. 계약·서버·화면 흐름

### 계약 (`contracts/` v7.42.0)
- `contracts/messages/site_setup.schema.json` (**1.15.0**):
  - `$defs/input`에 선택 필드 `previous_scene`(`{scene_version, calibration_version, uri, sha256?}`)과 `previous_zones`(`[{zone_id, polygon_xy}]`, 최대 100개) 추가.
  - `$defs/progress` 및 `$defs/result`에 선택 필드 `zone_height_changes`(`#/$defs/zone_height_changes`) 추가.
  - `$defs/zone_height_change_item`에 상태별 불변식(`UNKNOWN`이면 `reason` 필수·수치 필드 `null`, `UNCHANGED`/`CHANGE_SUSPECTED`이면 `reason = null`·수치 필드 필수, `CHANGE_SUSPECTED`이면 `max_diff_xy` 필수)을 `allOf`로 강제.
- 상위 봉투 버전: `job_request` **3.11.0**, `job_heartbeat` **2.14.0**, `job_callback` **2.22.0**. I1 계약은 건드리지 않는다.

### 서버 (`server/`)
- **마이그레이션 `V64__setup_session_zone_height_changes.sql`**: `setup_session`에 `zone_height_changes jsonb` 열 추가.
- **`SetupService.create`**: 현재 현장의 활성 관제 Scene(World 일치 검증이 `MISMATCH`가 아니고 `s3://` 주소를 얻을 수 있을 때)과 현재 유효한 구역(`zone.valid_to IS NULL`)을 `site_setup` 입력의 `previous_scene`·`previous_zones`로 넣는다. `JobObjectSignService`는 입력에 실린 `previous_scene.uri`에 대해 워커 GET 서명을 허용한다.
- **`SetupService.applySnapshotCore` / `SetupSupplementService.onRebakeFinished`**: 워커가 보낸 `zone_height_changes`를 `SetupZoneHeightChanges.fromSnapshot`으로 검증해 `setup_session.zone_height_changes`에 저장한다(미리보기 판정이 먼저 저장되고, 정식 굽기·보강 다시 굽기 성공 시 새 맵 기준 판정으로 갱신).
- **`SetupZonePlanService`**: `GET·PUT /api/setup-sessions/{id}/zones`와 `POST /api/setup-sessions/{id}/zones/seed` 응답의 각 이어 쓴 구역 항목(`zone_id != null`)에 `height_change`를 붙이고, 워커 항목이 아직 없으면 세션 상태에 맞는 `UNKNOWN`(`REUSE_SCENE` / `WORLD_NOT_ANCHORED` / `NO_PREVIOUS_SCENE` / `INSUFFICIENT_COVERAGE`)을 채운다. `needs_review`는 절대 자동으로 `false`로 바꾸지 않는다.

### 화면 (`front/apps/admin`)
- **`SetupZoneReviewCard.tsx` / `setupZonePlan.ts` / `useSetupZoneReview.ts`**:
  - 미확인 구역 안에서 **`변경 의심`(`CHANGE_SUSPECTED`) → `모름`(`UNKNOWN`) → `변화 없음`(`UNCHANGED`)** 순으로 정렬하고, `변경 의심` 행은 호박색 테두리·배지로 강조한다.
  - 미확인 `변화 없음` 구역이 있으면 **「변화 없음 N개 모두 확인」** 단추를 노출해 `UNCHANGED` 구역만 한 번에 확인하도록 권유한다.
  - `변경 의심`과 `모름` 구역은 일괄 확인에서 제외되어 관리자가 하나씩 확인·수정·삭제한다.

---

## 5. `-631`(VLM 의견·전후 사진 비교)이 받아 쓸 지점

`-631`은 이 티켓(`-630`)이 만든 기하 판정과 좌표를 그대로 입력으로 받아 VLM 설명과 전후 카메라 크롭 비교를 덧붙인다:
1. **후보 필터링**: 모든 이어 쓴 구역에 VLM을 부르지 않고, `setup_session.zone_height_changes.zones`에서 `status == "CHANGE_SUSPECTED"`(또는 요청 시 `UNKNOWN`)인 `zone_id`만 골라 VLM 전후 비교 대상으로 삼는다.
2. **크롭 중심 좌표**: `zone_height_change_item.max_diff_xy`(최대 높이 차이가 난 칸의 World XY)와 구역 다각형 `polygon_xy`를 사용해 이전 관제 Scene의 `bake_images`와 이번 세팅의 `camera_frames`/`bake_images`에서 같은 World 영역을 투영·크롭한다.
3. **판정 보존**: `-631`의 VLM 의견은 설명 문구와 참고 표시만 더하며, `-630`의 기하 판정(`status`, `changed_area_ratio`, `max_height_diff_m`)이나 구역의 `needs_review` 상태를 덮어쓰지 않는다(AGENTS 규칙 4).

---

## 6. 남은 위험과 한계

- **수직 아래 가림**: 책상 밑이나 천장 구조물 아래에 가려진 변화는 상면 높이($\max z$)만 보는 2.5D 격자에서 드러나지 않을 수 있다(명세 8.2의 바닥 투영 구역은 상면 변화가 주 대상).
- **얇은 봉·난간**: 10 cm 격자 중심점 사이를 비켜가는 아주 얇은 폴대·난간은 칸 중심 샘플링에서 빠질 수 있으며, 면적 비율(`10%`)보다 작아 `UNCHANGED`가 될 수 있다. 그래서 `UNCHANGED`도 자동 완료하지 않고 사람이「변화 없음 N개 모두 확인」을 눌러 최종 승인하게 두었다.
