# 같은 현장 재보정의 World 다시 맞추기 설계 (`S15P31A101-584`)

## 1. 관련 명세 절과 문제

- **관련 명세 절**: `docs/spec/04-coordinates.md` 4.1(World: 미터·오른손·+Z 상방·바닥 Z=0), 4.3 2단계(바닥·스케일), 4장 머리말과 "작업 간 독립성과 계보 조회"(세팅마다 World 자동 정의, 새 `calibration_version`·`scene_version` 발급), 4.5(Scene 버전), `docs/spec/11-data.md` 11.4(버전 추가만).
- **연관 설계**: `docs/design/site-setup-pipeline.md`(세팅 흐름, 4절 World 자동 정의), `docs/design/world-frame-tilt-correction.md`(-542 바닥 기울기 보정), `docs/design/final-map-pipeline.md`(-539), `docs/design/viewer-map-cameras.md`(-541).
- **문제 (13차 현장 P1 #9)**: 세팅마다 World 원점·축을 새로 정하므로(바닥 RANSAC → Z=0, 가장 긴 경계 벽 → +X, 모서리 원점), 같은 로비·같은 카메라·같은 거치대여도 이전에 그린 구역과 TBM 배치가 어긋난다. 12차 → 13차는 +X 벽이 90° 다르게 잡혀 구역이 통째로 돌아갔다.
- **목표**: 재보정할 때 카메라가 실제로 움직이지 않았으면 새 보정을 **이전 채택 보정의 World** 로 표현해 기존 구역·배치를 그대로 쓴다. 움직였으면 새 World 를 그대로 두고 사유를 남긴다.

## 2. 실측 근거 (12·13차 로비 보정 문서)

입력: 12차 채택 보정 `cal-lobby-init-f0ab1c6b-jba2-2f6cc097`(-542 이전, 기울기 보정 없음), 13차 첫 세팅 `cal-lobby-init-878286ab-jba2-21714bf3`, 13차 마지막 세팅 `cal-lobby-init-079f00cd-jba2-55083185`(둘 다 기울기 보정 적용). 문서 위치: `C:/Users/SSAFY/simai-local/session12/results/lobby-s12/state/calibrations/9b03….json`, `session13/results-1109-1811/pod.tgz` 의 `runs/lobby-s13-0724/state/calibrations/`.

| 비교 (이전 → 새) | yaw | 2D 닮음 축척 | 배치 잔차(판정) cm | 적용 잔차(축척 1) cm | 높이 차(새−이전) cm | 방향 변화 ° | 판정 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 12차 → 13차 첫 | −90.30° | 0.9895 | 3.7 ~ 7.1 | 2.7 ~ 11.4 | −7.2 ~ +9.0 | 1.3 ~ 4.9 | 채택 |
| 12차 → 13차 마지막 | +1.14° | 0.9975 | 5.1 ~ 9.1 | 5.2 ~ 9.9 | −8.6 ~ +9.4 | 1.3 ~ 6.0 | 채택 |
| 13차 첫 → 13차 마지막 | +91.44° | 1.0082 | 1.2 ~ 2.7 | 3.7 ~ 5.1 | −1.4 ~ +0.4 | 0.3 ~ 3.9 | 채택 |

- 같은 쌍의 7자유도 3D Umeyama(참고)는 잔차 3.8~7.2 cm, 축척 0.9896, 두 World 사이 기울기 1.13° 로 사용자 관측과 같다. 2D 닮음 잔차가 3D 잔차와 거의 같아(차이 ≤ 0.3 cm) 바닥 기울기가 수평 맞춤에 주는 영향은 작다.
- 높이 차 ±9 cm 는 12차 바닥이 기울어(A·B 2.36~2.38 m, C·D 2.54~2.55 m, -542 이전) 생긴 것이고 카메라 이동이 아니다.

## 3. 방법

### 3.1 변환 모형: yaw + 수평 이동만 (선택과 근거)

새 World → 이전 World 를 $X_{\text{prev}} = R_z(\psi)\,X_{\text{new}} + [t_x, t_y, 0]^\top$ 로 둔다. Z 와 축척은 새 보정 그대로다.

- **바닥은 새 추정을 쓴다**: 새 바닥은 점구름 법선 + 카메라별 실측 높이로 기울기를 바로잡은 값(-542)이다. 이전 World 가 기울기 보정 전이면(12차) 7자유도 맞춤은 새 카메라를 이전의 기울어진 바닥으로 옮겨 높이 검사(`height_checks`)와 +Z 정의를 망친다. yaw·수평 이동만 쓰면 바닥 Z=0·+Z 상방이 정의상 보존된다.
- **축척은 적용하지 않는다**: 축척 정본은 이번 세팅의 실측 카메라 높이(`MEASURED_CAMERA_HEIGHT`)다. 이전 축척을 덮으면 실측과 어긋나고 높이 잔차가 커진다. 추정 축척은 "같은 배치인가" 판정에만 쓴다. 1% 축척 차이가 구역에 주는 영향은 배치 중심에서 거리 × 1% 로, 방 가장자리(약 5 m)에서 약 5 cm 이고 `applied_center_residuals_m` 로 남는다.
- **추정**: 같은 카메라 ID 의 중심을 바닥에 투영한 2D 점으로 Kabsch: $\psi = \operatorname{atan2}\big(\sum (p_y q_x - p_x q_y), \sum (p_x q_x + p_y q_y)\big)$ (중심 뺀 이전 $p$, 새 $q$). 2D 에서는 축척이 있어도 최적 회전이 같다. 축척 $s = \sqrt{\text{dot}^2 + \text{cross}^2} / \sum |q|^2$ (2D Umeyama), 이동 $t = \bar p - R\bar q$.
- **외부 파라미터 갱신**: $x_{\text{cam}} = R X_{\text{new}} + t = (R R_z^\top) X_{\text{prev}} + (t - R R_z^\top T)$. 카메라 높이는 변하지 않는다. 모든 카메라(공통 ID 가 아닌 카메라 포함)를 같은 변환으로 옮긴다.

### 3.2 채택 조건 (`ReanchorConfig`, 명세에 없어 정한 값)

| 한도 | 값 | 근거 |
| --- | --- | --- |
| `min_cameras` | 3 | 2대면 2D 강체 맞춤이 정확히 정해져 잔차가 늘 0 이라 검증할 수 없다. |
| `min_layout_spread_m` | 0.5 m | 수평 배치 RMS 반경이 이보다 작으면 yaw 가 잡음에 휘둘린다(`DEGENERATE_LAYOUT`). 코너 배치 로비는 약 5 m. |
| `max_center_residual_m` (배치 잔차) | 0.15 m | 관측 최대 9.1 cm(움직이지 않은 3쌍)의 약 1.6배. 삼각대를 옮기면 보통 수십 cm 라 그보다 작다. -542 의 높이 잔차 한도처럼 "한 대만 어긋남"을 잡는 기준이며, 축척 차이를 뺀 닮음 잔차로 본다(전체 축척 차이는 아래 한도가 따로 본다). |
| `max_scale_deviation` | 0.03 | 관측 0.25~1.05%. 3% × 방 반경 5 m ≈ 0.15 m 로 배치 잔차 한도와 같은 크기다. 넘으면 실측 높이 입력이나 거치대 높이가 바뀐 것이다. |
| `max_orientation_change_rad` | 10° (0.1745 rad) | 관측 최대 6.0°(이전 바닥 기울기 1.13° + BA 차이 포함)의 약 1.7배. -437 에서 사람이 돌려놓은 `cam_C` 13.2° 는 걸린다. 중심이 그대로면 방향만 바뀌어도 World 맞춤에는 문제가 없지만, 크게 돌아간 카메라는 거치대를 건드린 것이라 "같은 배치" 가정의 안전 확인으로 둔다. |

배치 잔차는 수평만 본다. 높이 차이(`vertical_differences_m`)는 이전 바닥 기울기·실측 높이 입력 차이일 뿐 수평 맞춤과 무관해 판정에 쓰지 않고 기록만 한다.

**끄는 스위치(시연 현장 안전장치)**: 워커 환경 변수 `SIMAI_SETUP_WORLD_ANCHOR` = `auto`(기본, 위 판정대로) | `off`(추정·변환하지 않고 새 World 그대로). `off` 여도 이전 보정이 있으면 `world_anchor` 를 `accepted=false`, `reason=DISABLED` 로 남겨 "일부러 껐다"와 "비교할 이전 보정이 없었다(기록 생략)"를 구분한다. 그 밖의 값은 워커 시작 오류(종료 코드 2)다.

이 밖의 거부: 이전 보정이 `UNCALIBRATED` 이거나 `DATASET_GT`·`SIM` 출처(`PREVIOUS_NOT_USABLE`), 두 문서의 `coordinate_frame.site_id` 가 다름(`SITE_MISMATCH`). 이전 보정이 없으면(첫 세팅) 기록 없이 새 World 그대로다(모르면 비운다).

### 3.3 적용 위치

`ai/services/worker/site_setup.py` `SiteSetupHandler` 의 (e) Stage A 회전 BA 직후, (f) 보정 등록 직전에 주입점 `world_anchor` 를 한 번 부른다. 그 결과 문서를 등록(`calibration_publisher`)·미리보기(`preview_builder`)·굽기(`texture_baker`)에 똑같이 넘긴다. 운영 조립(`site_setup_stages.ProductionSiteSetupStages.anchor_world`)은 이미 들고 있는 **Tier 0 활성 보정**(`load_active_calibration`, 세팅의 내부 파라미터 출처이자 `previous_calibration_version`)을 이전 채택 World 로 쓴다. 새 서버 API·작업 입력은 필요 없다.

- 미리보기·굽기 단계(`reconstruct_tasks.preview`·`bake`)는 작업 폴더의 `calibration.json`(등록 때 옮긴 문서로 씀)에 MapAnything 출력을 닮음 정렬(`align_to_calibration` → `world_alignment.json`)하므로 정적 GLB 와 최종 맵(-539)이 자동으로 이전 World 에 놓인다.
- -541 `map_cameras` 는 그 정렬(`MA_ALIGNED_SIM3`)에서 나오므로 같은 World 다.
- 높이 검사·스케일 기록·Stage A 검증 수치는 yaw·수평 이동에 불변이라 다시 계산하지 않는다. `floor_tilt_correction` 의 `pitch_rad`·`roll_rad` 는 World 를 만들 때(옮기기 전) 축 기준 기록으로 그대로 둔다.

### 3.4 기록 (계약 `providers/calibration` 2.5.0 `world_anchor`)

`accepted`, `reason`, `reference_calibration_version`, `method`(`CAMERA_CENTER_YAW_XY`), `limits`, `used_cameras`, `yaw_rad`, `translation_m`, `scale`, `max_center_residual_m`, `max_orientation_change_rad`, 카메라별 `center_residuals_m`(판정)·`applied_center_residuals_m`(적용 뒤)·`vertical_differences_m`·`orientation_changes_rad`. 각도는 라디안(AGENTS 규칙 3). `world_anchor` 는 보정 내용이라 `content_hash` 에 넣는다(EC2 `CalibrationContentHash.UNHASHED` 를 바꾸지 않아도 된다). 등록 문서의 `schema_version` 은 `world_anchor` 가 있으면 2.5.0 이다.

버전 규칙: 새 `calibration_version`·`scene_version` 은 그대로 새로 발급한다(덮어쓰지 않음, 규칙 6). 바뀌는 것은 새 버전의 World 정의가 이전과 같아진다는 점이다. 같은 묶음은 같은 활성 보정에서만 쓰이므로(`collect` 가 묶음과 활성 보정의 버전 일치를 확인) 재시도해도 같은 문서가 나온다.

## 4. 버린 대안

1. **7자유도 Umeyama 를 그대로 적용**: 이전 바닥 기울기·축척 오차를 새 보정에 들여온다(12차 기울기 1.13°, 축척 1%). 새 바닥 Z=0·실측 높이와 어긋난다.
2. **전체 맞춤 뒤 yaw 만 투영**: 2D 맞춤과 결과가 거의 같고(2D·3D 잔차 차이 ≤ 0.3 cm) 계산·설명만 길다.
3. **기존 구역을 새 World 로 자동 변환해 다시 발행**: 구역·규칙·레이아웃마다 변환 경로가 필요하고(서버·뷰어·Tier 0 구성 묶음), 판정 정본인 구역을 기계가 몰래 바꾸게 된다. 보정 하나만 이전 World 로 옮기면 구역은 그대로 맞는다.
4. **배경 특징점·점구름 정합으로 직접 맞추기**: 카메라 중심 4개로 충분히 잘 정해지고(잔차 수 cm), 추가 매칭 비용·실패 경로가 생긴다.
5. **방향 변화 판정 생략**: 중심만으로도 맞춤은 되지만, 거치대를 건드린 경우를 걸러 낼 안전 확인이 없어진다. 한도를 넉넉히(10°) 두고 남겼다.

## 5. 잔여 위험

- **카메라 ID 를 통째로 바꿔 단 경우**: 4대를 한 칸씩 돌려 꽂으면(배선 순환) 중심·방향이 모두 맞아 90° 돈 World 로 채택된다. yaw 는 벽 선택에 따라 정상적으로 90°·180° 도 나오므로 yaw 크기로 거를 수 없다. 현장에서 카메라 ID 는 IP 예약으로 고정한다.
- **천천히 쌓이는 어긋남**: 매번 이전 World 에 맞추므로 세팅마다 남는 수 cm 차이가 계보를 따라 쌓일 수 있다(맞춤 기준은 직전 활성 보정). 지금은 기록(`applied_center_residuals_m`)으로만 본다.
- **활성 보정이 DRIFTED**: 13차처럼 `DRIFTED` 활성 보정에도 맞춘다. 그 World 가 구역이 그려진 World 이기 때문이다. 이동한 카메라는 잔차로 걸린다.
- **실제 경사·다른 층**: 같은 `site_id` 안에서 다른 공간(층)을 세팅하면 카메라 ID 가 같아도 잔차가 커서 거부되지만, 그 판정에만 기댄다.
- **명세 차이**: 명세 4장은 "World 는 그 작업 안에서만 일관되면 된다"고 적었다. 이 설계는 카메라가 그대로면 작업 사이에도 World 를 유지한다(새 버전은 그대로 발급). 명세 정리 MR 에서 4장 머리말·"작업 간 독립성과 계보 조회"에 반영이 필요하다.

## 6. 코드·시험 위치

- 순수 함수: `ai/src/simai_ai/calibration/world_frame/reanchor.py`(`estimate_reanchor`, `apply_reanchor`, `reanchor_calibration`, `ReanchorConfig`)
- 세팅 연결: `ai/services/worker/site_setup.py`(주입점 `world_anchor`), `ai/services/worker/site_setup_stages.py`(`anchor_world`, `world_anchor_enabled_from_env`, 등록 문서 2.5.0), `ai/services/worker/__main__.py`(환경 변수 읽기)
- 계약: `contracts/providers/calibration.schema.json` 2.5.0, 예시 `contracts/examples/calibration.world_anchor.json`·`calibration.world_anchor_disabled.json`, 반례 `contracts/examples/invalid/calibration.world_anchor_accepted_not_anchored.json`
- 시험: `ai/tests/calibration/world_frame/test_reanchor.py`(합성 리그 + 로컬 12·13차 실제 문서, 없으면 건너뜀), `ai/tests/services/worker/test_site_setup.py`·`test_site_setup_stages.py`

## 7. 넘긴 일

- 현장 검증: 다음 세팅에서 `world_anchor` 기록과 기존 구역이 미리보기 맵·관제 화면에서 맞는지 본다(GPU 세션).
- 뷰어·서버 표시: 세팅 결과 화면에 "이전 World 에 맞춤 / 새 World(사유)" 를 보여 주는 일은 이 티켓 밖이다(보정 문서에만 기록).
- 명세 4장 갱신(위 5절 명세 차이).
