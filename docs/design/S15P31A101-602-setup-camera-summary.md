# S15P31A101-602 세팅 세션 카메라별 요약·쌍별 홀드아웃·Scene 호환 카메라별 값

관련 명세: 4장(보정), 3.2(Tier 0 정본, Visual 층은 판정에 들어가지 않음), 12.5(I5 작업), 12.6(현장 세팅 REST), 13장(관제 3D 뷰어). 앞 문서 `docs/design/site-setup-pipeline.md`(세팅 워커·서버 등록), `docs/design/world-reanchor.md`(-584 `world_anchor`), 공통 `contracts/common/scene_compatibility.schema.json`(-381).

## 문제

front 가 현장 세팅 화면을 설계하는데, AI 가 이미 계산하고 서버가 이미 받는 값 가운데 어느 API 도 돌려주지 않는 것이 있었다.
- 이번 세팅 보정 문서(K·R·t, 카메라별 `reprojection_error_px`, `camera_height_m`, `world_anchor`)는 `calibration_document` 에 저장되지만 세션 조회에는 버전 문자열만 있다.
- 쌍별 홀드아웃 오차(`metrics.joint_ba.pair_holdout`·`pair_holdout_median_px`)는 하트비트로 오지만 `SetupService.applySnapshotCore` 가 읽지 않고 버린다.
- `SceneCompatibility.evaluate` 는 카메라별 중심·회전 변화를 계산하지만 최대값 둘만 낸다.

AI 코드는 바꾸지 않는다. 서버와 EC2 → 관제 앱 응답만 바꾼다.

## 한 일

- `GET /api/setup-sessions/{id}`(생성·취소·재시도 응답도 같은 모양)에 `cameras`·`pair_holdout` 을 더한다.
- 마이그레이션 `V49__setup_session_pair_holdout.sql`: `setup_session.pair_holdout jsonb`(NULL 허용).
- `GET /api/scenes/current` 의 `compatibility` 에 `cameras`(카메라별 `center_delta_m`·`rotation_delta_rad`)를 더한다. viewer_health 의 `scene.compatibility` 에는 싣지 않는다.
- 계약 v7.28.0(마이너): `common/scene_compatibility` 에 선택 `cameras`(정의 `$defs/camera_delta`). Python·TS 생성 타입 갱신. 세팅 세션 조회 응답은 계약 스키마가 없어 판에 들지 않는다. `risk_event`·`viewer_health`·I1·I5 계약은 그대로다.

## 세션 조회 새 필드와 출처

```json
{
  "setup_session_id": "…",
  "status": "READY_FOR_ZONES",
  "calibration_version": "cal-2-jba2-setup",
  "cameras": [
    {
      "camera_id": "cam1",
      "position_xyz": [0.2001, 0.2001, 2.4998],
      "forward_xyz": [0.7574, 0.5581, -0.3388],
      "yaw_rad": 0.6350,
      "pitch_rad": -0.3456,
      "hfov_rad": 1.5200,
      "image_size": [1920, 1080],
      "camera_height_m": 2.5098,
      "reprojection_error_px": {"median": 1.2, "p95": 3.4},
      "world_anchor": {
        "reference_calibration_version": "cal-1-jba2-setup",
        "accepted": true,
        "reason": "ANCHORED",
        "orientation_change_rad": 0.009472,
        "center_residual_m": 0.017,
        "applied_center_residual_m": 0.0339,
        "vertical_difference_m": 0.0096
      }
    }
  ],
  "pair_holdout": {
    "holdout_worsening_tolerance_px": 1.0,
    "pairs": {
      "cam1-cam2": {
        "before": {"n": 40, "invalid": 1, "median": 2.4, "p90": 4.0, "p95": 5.1},
        "after": {"n": 40, "invalid": 0, "median": 1.1, "p90": 2.0, "p95": 2.6},
        "median_px": 1.1
      }
    }
  }
}
```

| 필드 | 출처 | 계산 |
|---|---|---|
| `cameras[].position_xyz` | 세션 `calibration_version` 의 `calibration_document` R·t | C = −Rᵀt. `CalibrationViews.cameraPoses` 를 그대로 쓴다(관제 보정 상태 `camera_poses` 와 같은 값) |
| `cameras[].forward_xyz` | 같은 문서 R | R 셋째 행 단위 벡터(카메라 +Z 의 World 방향) |
| `cameras[].yaw_rad` | forward | atan2(f_y, f_x). World +X 에서 +Y 쪽 반시계. 정면이 거의 수직(√(f_x²+f_y²) < 1e−9)이면 null |
| `cameras[].pitch_rad` | forward | asin(f_z). +Z 상방이라 아래를 보면 음수 |
| `cameras[].hfov_rad` | K·image_size | 2·atan(W / 2f_x) |
| `cameras[].image_size` | 문서 `image_size` | 그대로([width, height] 픽셀). 이 보정을 만든 스트림 해상도 |
| `cameras[].camera_height_m` | 문서 `camera_height_m` | 그대로. 없으면 null |
| `cameras[].reprojection_error_px` | 문서 `reprojection_error_px`(세팅 홀드아웃 median·p95, `ai/src/simai_ai/calibration/document.py`) | 그대로. 객체가 없으면 null, 한쪽만 있으면 다른 칸 null |
| `cameras[].world_anchor` | 문서 `world_anchor`(-584) | 문서 수준 `reference_calibration_version`·`accepted`·`reason` 과 이 카메라의 `orientation_changes_rad`·`center_residuals_m`·`applied_center_residuals_m`·`vertical_differences_m` 값. 이름은 계약 이름의 단수형 |
| `pair_holdout` | 하트비트·결과 `metrics.joint_ba` 의 `pair_holdout{before,after}`·`pair_holdout_median_px`·`holdout_worsening_tolerance_px` | 두 맵을 쌍 키로 합친다. 쌍 키는 워커 그대로(`<camera_id>-<camera_id>`). 카메라 ID 에 '-' 가 있을 수 있어 나누지 않는다 |

`median_px` 는 워커가 `after.median` 을 소수 넷째 자리로 반올림한 값이다(`joint_ba_v2.py`). 1.1.0 시절 스냅샷처럼 `pair_holdout` 없이 중앙값만 오면 `before`·`after` 는 null 이다.

## 언제 채워지나

| 시점 | `cameras` | `pair_holdout` | Scene `compatibility.cameras` |
|---|---|---|---|
| 세션 생성 ~ INITIAL_POSE_AND_WORLD | null | null | 무관 |
| JOINT_BA_AND_VERIFY(65%) 하트비트 커밋 뒤 | 배열. 워커가 이 스냅샷에 `calibration_version` 과 `artifacts.calibration_document` 를 함께 싣고, 서버가 같은 트랜잭션에서 문서를 `calibration_document` 에 넣고 세션 `calibration_version` 을 채운다 | 같은 스냅샷의 `metrics.joint_ba` | 이 보정이 아직 Tier 0 에 적용되지 않았으면 현재 Scene 기준 판정은 이전과 같다 |
| PREVIEW_AND_VLM_DRAFT · READY_FOR_ZONES · BAKING_TEXTURE · COMPLETED | 같은 값(보정 버전은 불변) | 값을 실은 마지막 스냅샷 것. 지표 없는 스냅샷은 이전 값을 지우지 않는다 | READY_FOR_ZONES 부터 세션 Scene 이 현재 Scene 이 되고, 새 구성 묶음을 Tier 0 가 적용하면 그 Scene 과 같은 보정이라 `SAME_CALIBRATION`(cameras null) |

화면이 진행률 화면에서 65% 직후부터 카메라 요약을 그릴 수 있다. READY_FOR_ZONES 까지 기다릴 필요는 없다. 단 세션이 실패해도 이미 등록된 보정의 요약은 남는다(보정 버전은 지우지 않는다, 규칙 6).

## null 로 남는 것과 이유

- 보정 문서 전(65% 전): `cameras` null. 빈 배열은 '카메라가 없다'로 읽힐 수 있어 '아직 모름'은 null 로 둔다.
- 문서가 있어도 `cameraPoses` 가 거부하면(좌표 규약이 다름, 회전이 직교가 아님, image_size·K 가 틀림) 한 대만 빼지 않고 전체 null. 관제 보정 상태와 같은 규칙이다(지어내지 않음).
- `yaw_rad`: 정면이 수직이면 방위가 정의되지 않는다.
- `camera_height_m`·`reprojection_error_px`: 계약상 선택 필드라 문서에 없을 수 있다.
- `world_anchor`: 문서에 기록이 없거나(첫 세팅, 2.5.0 이전 워커) `reference_calibration_version` 이 null 이면 카메라별 객체 전체가 null. 기록은 있지만 그 카메라 값이 없으면(DISABLED·거부 사유, 그 카메라 제외) 그 칸만 null.
- `pair_holdout`: 받은 적 없으면 null. 형식이 어긋난 스냅샷은 저장하지 않고 이전 값을 둔다(표시용 지표라 세팅 하트비트를 거부하지 않는다).
- Scene `compatibility.cameras`: 같은 보정(모두 0 이 자명하지만 자세를 읽지 않음)·다른 World·자세 미상(카메라 집합 다름, 회전 오류)이면 null.

## 명세에 없어 정한 값

| 값 | 정한 것 | 근거 |
|---|---|---|
| 세션 요약 대상 보정 | 세션 `calibration_version` 의 문서(이 세션이 등록했든, 이미 있던 버전을 참조했든) | 세션이 쓰는 보정이 곧 '이번 보정'이다. 계보 검사(`validateVersionLineage`)가 다른 세션 귀속 버전을 이미 막는다 |
| 쌍별 값 보관 | 세션 행 jsonb 한 칸, 마지막 값 | `height_checks`·`object_cross_checks` 와 같은 '세션의 최신 표시 값'이다. 시계열이 아니고 재시도는 새 세션이라 섞이지 않는다 |
| 쌍 수 상한 | 256 | 카메라 16대 전부 쌍(120)보다 넉넉하다. 넘으면 저장하지 않는다 |
| 홀드아웃 통계 검사 | n·invalid 는 0 이상 정수, median·p90·p95 는 0 이상 유한수 또는 null(계약 `holdout_stats`) | 서버는 하트비트를 스키마로 검사하지 않으므로 표시 전에 모양만 확인한다 |
| Scene 카메라별 값 위치 | `current()`(GET /api/scenes/current)만, `compare()`(viewer_health)는 뺀다 | viewer_health 는 주기 송신이고 -595 가 1.4.0 을 선점했다. 같은 공통 스키마라 넣어도 검증은 통과하지만 송신 모양을 바꾸지 않았다 |
| 카메라별 값 순서 | 카메라 ID 순 | 화면·시험이 안정적으로 읽는다 |

## 코덱(해상도·nominal fps) 결정: 싣지 않음

`codec_info`(`width`·`height`·선택 `nominal_fps`)는 `CctvRemoteHub.codecTexts` 메모리에만 있다. 세션 요약에 싣지 않은 이유:
- 원격 시청 중계는 시청자가 있어 수요(demand)가 켜졌을 때만 게이트웨이가 송출한다. 아무도 보지 않으면 코덱 정보가 없고, 게이트웨이가 다시 붙으면 지워진다. 세션 조회 결과가 '누가 CCTV 를 보고 있었나'에 따라 바뀐다.
- 원격 미리보기 스트림은 Tier 0 가 보정에 쓴 스트림과 다를 수 있다(별도 송출 큐). 보정과 같은 해상도라는 보장이 없다.
- 세팅 시점 값으로 남기려면 새 상태(세션별 저장)가 필요하다. 이번 범위 밖이다.

보정이 실제로 쓴 해상도는 `cameras[].image_size` 에 있다. 실시간 코덱이 필요하면 front 가 `/ws/cctv/view` 의 `codec` 메시지를 읽는다.

## 결정으로 뺀 것

- 보정 결과 기반 방향 안내(세팅 뒤 `coverage_guidance` / `GET /api/cameras/coverage-guides` 의 `guides[]` 로 카메라를 돌리라고 안내): **사용자 결정 2026-11-11 로 뺐다(미룬 것이 아님).** 보정 뒤 카메라를 움직이면 그 보정이 무효가 되어 세팅 전체를 다시 해야 한다. 시연은 카메라를 미리 맞춰 두고 진행하고, 겨누기는 세팅 전 쌍별 안내가 이미 맡는다. 코드 변경 없음.

## 범위 밖으로 남긴 것

- 카메라별 보정 통과·조정 판정: 문서에는 카메라별 오차 수치만 있고 통과·조정 판정 기준과 값이 없다. 기준을 정하는 일은 명세·AI 쪽 결정이 먼저다.
- 쌍별 겹침 %: AI 가 계산해 보내지 않고 서버도 받지 않는다. 새 계약 필드와 워커 계산이 필요하다.
- 이전 모델(보정·Scene) 목록과 재사용: 목록 API·재사용 흐름이 없다. 이번 요약은 이번 세션 보정과, `world_anchor` 가 비교한 이전 보정 하나와의 차이만 준다.

## 버린 대안

- `cameras` 를 빈 배열로 시작: '카메라 0대'와 '아직 모름'이 구분되지 않는다.
- `pair_holdout` 을 배열(`[{pair, before, after}]`)로 바꾸기: 계약이 쌍 키 맵이라 그대로 두는 편이 추적이 쉽다. 키를 카메라 둘로 나누는 것은 '-' 가 든 ID 때문에 안전하지 않다.
- viewer_health 에도 카메라별 값: 위 표 근거로 뺐다.
- 세션 요약을 SetupArtifactRegistrar 등록 때 미리 계산해 저장: 문서가 정본이고 조회 때 계산이 수 μs 라 사본을 두지 않았다.

## 코드·시험 위치

- `server/src/main/java/com/ssafy/simai/domain/setup/SetupCameraSummary.java`: 문서 → `cameras`.
- `server/src/main/java/com/ssafy/simai/domain/setup/SetupPairHoldout.java`: 스냅샷 → `pair_holdout`.
- `server/src/main/java/com/ssafy/simai/domain/setup/SetupService.java`: `applySnapshotCore` 저장(COALESCE 로 이전 값 유지), `getInSite` 조회.
- `server/src/main/java/com/ssafy/simai/domain/calibration/CalibrationViews.java`: `cameraPoses(ObjectMapper, byte[])` 정적 공개(계산 그대로).
- `server/src/main/java/com/ssafy/simai/domain/scene/SceneCompatibility.java`: `compareWithCameras`, `compare` 는 `cameras` 를 뺀다.
- `server/src/main/resources/db/migration/V49__setup_session_pair_holdout.sql`.
- 계약: `contracts/common/scene_compatibility.schema.json`, 예시 `contracts/examples/scene_compatibility.cameras.json`, 반례 `contracts/examples/invalid/scene_compatibility.camera_negative_center.json`, 생성 `ai/src/simai_ai/contracts/models.py`, `front/packages/contracts/src/generated/`.
- 시험: `SetupCameraSummaryTest`(값·없는 값 null·문서 없음·수직 정면), `SetupPairHoldoutTest`(병합·옛 형식·어긋난 형식), `SceneCompatibilityTest`(카메라별 값·null 경우·viewer_health 제외), `SetupResultRegisterIntegrationTest`(65% 전 null → 등록 뒤 값 → 지표 없는 스냅샷이 값 유지), `CalibrationStatusIntegrationTest`(REST 와 viewer_health 가 cameras 말고 같음).

## 남은 위험

- 보정 문서가 큰 현장(카메라 수십 대)도 조회마다 문서를 읽어 계산한다. 문서 상한 4 MiB, 보통 수십 KB 라 무시할 만하다.
- `pair_holdout` 키·값은 워커 출력에 묶여 있다. 워커가 키 형식을 바꾸면 화면이 그대로 따라 바뀐다.
- -595(v7.27.0)와 README 첫 줄이 겹친다. 먼저 머지되는 쪽 뒤에 rebase 로 순서만 맞추면 된다.
