# S15P31A101-532: GLB 빈 곳 채우기 2차 — 바닥·벽 받침 평면 굽기 + 텍스처 인페인팅 비교 (`hole_fill_v2`)

`-527`(시점 의존 텍스처, View-Dependent Texture)에서 검증한 **바닥·벽 받침 평면 + 깊이 5 cm 일치 게이트 + 다중 카메라 Oklab 메도이드 외톨이 제거 + 구멍 경계 소프트 블렌딩**을 실시간 커스텀 GLSL 셰이더 없이 **표준 GLB 파일 자체의 메시·텍스처·정점색에 직접 구워 넣는(bake)** 실험 파이프라인 및 비교 뷰어다.

> **주의 (AGENTS.md 규칙 2·7 준수)**:
> - 본 파이프라인이 생성하는 받침 평면(`backing_floor`, `backing_wall_0..2`) 및 채움 텍셀(`filled_tone`, `filled_lama`, `filled_classic`)은 **시각 보강 전용(`purpose: "VISUAL_ONLY"`, `safety_use: false`)** 이며, 로봇 경로 계획·충돌 판정·안전 구역 계산에 절대 사용하지 않는다.
> - 모든 받침 평면은 알베도 텍스처(`backing_{role}_albedo`)와 동일한 해상도의 출처 지도(`backing_{role}_surface_source`) 및 `extras.simai.hole_fill_v2` 메타데이터에 **카메라 관측 투영(`observed_projection = 3`)** 과 **추정 채움(`filled_<variant> = 4`)** 을 엄격히 구분해 기록한다.

---

## 1. 디렉터리 구성

```text
ai/experiments/hole_fill_v2/
├── __init__.py
├── core.py            # Oklab 외톨이 제거, 위상 용접 구멍 경계 거리, 작은 조각 제거, 톤/LaMa 채우기, 경계 블렌딩 순수 함수
├── bake_glb.py        # 4개 받침 평면 생성·관측 투영·3종 인페인팅(tone/lama/classic)·표준 GLB 굽기 스크립트
├── index.html         # 표준 Three.js 재질(MeshBasic / MeshStandard) 4칸 비교 뷰어 UI
├── viewer.js          # 표준 GLTFLoader + MeshBasicMaterial/MeshStandardMaterial 비교 렌더러 (특수 셰이더 없음)
├── capture_shots.py   # Headless Chrome/Edge WebGL 8시점 1900x1000 비교 스크린샷 자동 캡처
└── README.md          # 실험 문서 및 운영 편입 가이드
```

단위 테스트:
- `ai/tests/experiments/hole_fill_v2/test_hole_fill_v2.py`

로컬 산출물 (`C:/Users/SSAFY/simai-local/hole-fill-v2/`):
- `scene_orig.glb` (`10.40 MB`, 12차 원본 사본)
- `scene_tone.glb` (`30.02 MB`, 방안 a: 평면 관측 평균/중앙 톤 + 거리에 따른 완만한 색 번짐 + 미세 저주파 얼룩, 격자 없음)
- `scene_tone_grid.glb` (`30.12 MB`, 방안 a-grid: 방안 a + 은은한 1m/0.5m 격자)
- `scene_lama.glb` (`32.24 MB`, 방안 b: `big-lama.pt` FFC 전역 가이드 + 768×768 겹침 타일 인페인팅)
- `scene_classic.glb` (`32.97 MB`, 방안 c: `simai_ai.scene.glb_inpaint.classic_inpaint_plane` Telea + 다중 스케일 패치 합성)
- `planes/plane_{floor,wall_0,wall_1,wall_2}_compare.jpg` (평면별 직교 투영 `[관측+마스크 | a 톤 | b LaMa | c 고전]` 4칸 비교 시트)
- `shots/compare_{cam_A..D,top,obl1..3}.jpg` (8개 시점 `1900×1000` 표준 Three.js 4칸 비교 캡처)
- `shots/tone_vs_grid_{top,obl1}.jpg` (방안 a 자연 톤 vs 방안 a-grid 1m 격자 2칸 비교 캡처)
- `shots/compare_obl1_pbr_standard.jpg` (순수 `MeshStandardMaterial` PBR 조명 검증 캡처)
- `report.json` (정량 지표·소요 시간·출처 비율·조각 제거·해상도 표 전체 기록)

---

## 2. 실행 명령

```powershell
# 1) 단위 테스트 실행
C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-532-glb-hole-fill-v2/ai/.venv/Scripts/pytest.exe -q C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-532-glb-hole-fill-v2/ai/tests/experiments/hole_fill_v2/test_hole_fill_v2.py

# 2) 린트 검사 (ruff F, E9)
uvx ruff@0.15.0 check --select F,E9 ai/experiments/hole_fill_v2 ai/tests/experiments/hole_fill_v2

# 3) 받침 평면 굽기 및 5종 GLB + 평면 비교 시트 + report.json 생성
C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-532-glb-hole-fill-v2/ai/.venv/Scripts/python.exe ai/experiments/hole_fill_v2/bake_glb.py

# 4) Headless WebGL 8시점 1900x1000 4칸 비교 캡처
C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-532-glb-hole-fill-v2/ai/.venv/Scripts/python.exe ai/experiments/hole_fill_v2/capture_shots.py
```

---

## 3. LaMa 가중치 내려받기 경로 및 해시

- **로컬 저장 경로**: `C:/Users/SSAFY/simai-local/hole-fill-v2/weights/big-lama.pt`
- **출처 URL**: `https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt` (TorchScript JIT 모델, Apache-2.0 호환)
- **파일 크기**: `196.27 MiB` (`205,803,911 bytes`)
- **SHA-256**: `sha256:7ba7aa7ac37a4d41fdbbeba3a2af7ead18058552997e3a3cd1a3b2210c9e6b4c`

---

## 4. 핵심 설계 및 기하·색상 근거

### 4.1 바닥·벽 받침 평면 생성 및 표준 깊이 버퍼 간섭 방지
- `-527` 뷰어는 `renderer.clearDepth()` 로 받침 평면을 먼저 그린 뒤 깊이 버퍼를 초기화했으나, 일반 GLB 뷰어(Three.js `GLTFLoader`, Blender, Babylon.js 등)는 단일 깊이 버퍼로 모든 메시를 함께 렌더링한다.
- 12차 TSDF 메시는 약간의 전역 기울기와 곡률을 가지고 있어 단순 평면을 놓으면 낮은 바닥 삼각형이나 벽면과 교차할 수 있다. 이를 해결하기 위해:
  1. **바닥 받침 평면(`backing_floor`)**: 기본 오프셋을 `World Z = -0.015 m` (바닥 중앙값 `+0.0461 m` 아래 `6.1 cm`)로 두되, `6 cm` 기하 격자의 각 정점이 반경 `14 cm` 내 TSDF 바닥 삼각형 최소 높이보다 항상 `1.5 cm` 이상 아래(`min(-0.015, z_tsdf_min - 0.015)`)에 놓이도록 구성했다.
  2. **벽 받침 평면(`backing_wall_0..2`)**: `find_wall_planes` 가 수용한 3개 경계 벽에 대해:
     - `wall_0` (돌출 창틀이 없고 `cam_C`·`cam_D`가 바로 앞에 붙어 있는 평면 우드월): 로컬 벽 표면 `d_surf(u, z)` 보다 바깥쪽으로 `2.2 cm` 물리되 `[d_orig - 0.038 m, d_orig - 0.018 m]` 범위로 유지하여 우드월 액자를 가리지 않으면서 `cam_C`(`0.0042 m`) 뒤로 접히지 않게 했다.
     - `wall_1`, `wall_2` (벽면 바깥쪽으로 오목하게 들어간 창문 베이가 있는 벽): 구멍 영역에서는 `d_nom = d_orig - 0.018 m` 평면을 유지하되, TSDF 메시가 존재하는 영역에서는 각 셀의 최외곽 삼각형 깊이보다 `1.8 cm` 더 바깥쪽(`min(d_nom, tri_min_d - 0.018)`)으로 물러나 오목한 창문 유리를 절대 가리지 않게 했다.
     - 모든 벽 받침 평면 재질을 `doubleSided: false` (방 안쪽 법선 `+n` 만 렌더링하는 `FrontSide`)로 설정해 벽 바깥이나 벽 가까이에서 바라볼 때 시야를 가리지 않게 했다.

### 4.2 평면 텍스처 해상도 선택 (해상도별 용량 비교 표)
카메라 4대(`2048×1152`, `fx ≈ 1084 px`)에서 바닥·벽까지의 거리 `3.5 ~ 6.0 m` 기준 원본 사진 1 픽셀의 물리 크기는 `3.3 ~ 5.7 mm` 이다.

| 텍셀 크기 | 바닥 해상도 (`floor`) | `wall_0` 해상도 | `wall_1` 해상도 | `wall_2` 해상도 | 총 텍셀 수 | 평면 PNG 합계 | 추정 GLB 크기 | 채택 여부 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `10.0 mm/texel` | `824×1024` | `976×256` | `568×256` | `680×248` | `1.41 M` | `1.95 MB` | `12.78 MB` | 미채택 (벽 글씨·창틀 계단 현상) |
| **`5.0 mm/texel`** | **`1640×2048`** | **`1960×512`** | **`1136×504`** | **`1360×496`** | **`5.61 M`** | **`6.72 MB`** | **`30.02 MB`** (출처 지도 포함) | **채택 (원본 사진 물리 해상도 정합)** |
| `4.0 mm/texel` | `2048×2560` | `2448×640` | `1424×632` | `1696×616` | `8.75 M` | `9.92 MB` | `36.3 MB` | 미채택 (화질 이득 대비 용량·시간 증가) |

### 4.3 깊이 5 cm 일치 게이트 및 카메라 간 Oklab 외톨이 제거
- **5-tap 깊이 일치 게이트**: 각 카메라의 `1536×864` 깊이 그림자 맵에서 중심 및 상하좌우 4이웃 모두 TSDF 기하가 존재(`d > 0.05 m`)하고 `|z_cv - d0| <= 0.05 m` 이내로 일치할 때만 카메라 사진을 투영한다. 이로써 메시에 구멍이 뚫린 자리(`d0 == 0`)나 앞쪽 의자·책상에 가려진 자리(`z_cv - d0 > 0.05 m`)에 전경 가구 색이 잘못 박히는 현상을 원천 차단한다.
- **Oklab 메도이드 외톨이 제거 ($\sigma = 0.056$)**:
  - 후보 3대 이상: 쌍별 Oklab 색 거리 합이 최소인 메도이드(medoid) 카메라를 기준색으로 선택.
  - 후보 2대: 주변 관측 텍셀 가우스 중앙색(`neighbor_median_srgb`)을 **세 번째 표(3rd vote)** 로 넣어 두 후보 중 실제 평면 색과 일치하는 카메라를 메도이드로 선택.
  - 기준색과의 정규화 Oklab 거리 $d_n = \|\text{lab}_k - \text{lab}_{\text{ref}}\|_2 / \sigma$ 에 대해 $\exp(-d_n^2) \cdot (1 - \text{smoothstep}(\text{lo}, \text{hi}, d_n))^2$ 감쇠를 적용해 의자 다리·테이블 그림자 외톨이를 제거한다.

| 평면 | 텍스처 해상도 (`W×H`) | 면적 (`m²`) | 관측 비율 (`observed_projection`) | 미관측 비율 (`inpaint_mask`) | 관측 텍셀 수 | 3대+ 후보 | 2대 후보 | 1대 후보 | 감쇠된 외톨이 표본 수 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `floor` | `1640×2048` | `83.98` | **`53.20%`** | `46.80%` | `1,795,858` | `691,760` | `687,427` | `416,671` | `809,271` |
| `wall_0` | `1960×512` | `25.08` | **`75.50%`** | `24.50%` | `758,739` | `0` | `576,325` | `182,414` | `48,520` |
| `wall_1` | `1136×504` | `14.36` | **`73.29%`** | `26.71%` | `420,141` | `0` | `312,407` | `107,734` | `34,712` |
| `wall_2` | `1360×496` | `16.75` | **`49.30%`** | `50.70%` | `333,983` | `0` | `253,625` | `80,358` | `74,936` |
| **합계/평균** | — | **`140.16`** | **`58.78%`** (`82.39 m²`) | **`41.22%`** (`57.77 m²`) | `3,308,721` | `691,760` | `1,829,784` | `787,177` | `967,439` |

### 4.4 떠다니는 작은 조각 제거 (단계별 비교 표)
`1 mm` 정점 위상 용접(weld) 후 정점 공유 연결 성분을 분석한 결과, 전체 `46`개 성분(`149,997`면, `192.63 m²`) 중 실제 방·가구 구조물 11개 성분(`>= 321면, >= 0.462 m²`)과 공중에 떠 있는 작은 파편 35개 성분(`<= 203면, <= 0.231 m²`) 사이에 뚜렷한 간극이 존재한다.

| 기준 (`min_faces`, `min_area_m2`) | 전체 성분 수 | 제거 성분 수 | 유지 성분 수 | 제거 삼각형 수 (비율) | 제거 면적 `m²` (비율) | 비고 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `(0, 0.00 m²)` | 46 | 0 | 46 | `0` (`0.00%`) | `0.000` (`0.00%`) | 원본 (공중 부유 파편 존재) |
| `(150, 0.05 m²)` | 46 | 33 | 13 | `1,229` (`0.82%`) | `1.512` (`0.79%`) | 중형 부유 파편 2개 잔류 |
| **`(300, 0.25 m²)`** | **46** | **35** | **11** | **`1,583` (`1.06%`)** | **`1.885` (`0.98%`)** | **채택 (부유 파편 전량 제거, 가구·벽 손실 0)** |
| `(500, 0.35 m²)` | 46 | 37 | 9 | `2,378` (`1.59%`) | `2.972` (`1.54%`) | 실제 소형 의자/가구 조각 일부 손실 시작 |
| `(800, 0.50 m²)` | 46 | 43 | 3 | `6,117` (`4.08%`) | `7.407` (`3.85%`) | 독립 가구 덩어리 다수 손실 |
| `(1500, 1.00 m²)` | 46 | 44 | 2 | `7,191` (`4.79%`) | `8.636` (`4.48%`) | 주 방 껍질 외 가구 대부분 손실 |

### 4.5 메시 구멍 가장자리 소프트 블렌딩 (8 cm)
- `1 mm` 위상 용접 메시에서 공유 삼각형이 1개뿐인 진짜 기하 구멍 테두리(`open_edges = 7,828`개, `boundary_vertices = 7,675`개)를 검출한다.
- glTF 2.0 표준 재질에서 정점색(`COLOR_0`)은 텍스처(`baseColorTexture`)에 **곱셈(`tex * color_0`)** 으로만 작용하므로 구멍 테두리의 어두운 그림자 텍셀을 밝은 평면 톤으로 올릴 수 없다. 따라서:
  - 텍스처가 있는 `textured_0000..0008`: 열린 경계 `8 cm` 이내 삼각형들의 UV 아틀라스 텍셀을 래스터라이즈해 대응 3D 월드 좌표 아래의 받침 평면 색과 선형 보간(`blend_atlas_at_hole_boundary`, `63,135` 텍셀 보정).
  - 텍스처가 없는 `unobserved_neutral`: 열린 경계 `8 cm` 이내 정점들의 `COLOR_0` 선형 정점색을 하부 받침 평면 색과 보간(`blend_unobserved_vertex_colors`).

---

## 5. 미관측 채움 3가지 변형(`tone` / `lama` / `classic`) 비교 결과

| 변형 | 산출물 파일 | GLB 용량 | 공통 투영 시간 | 채움+조립 시간 | 총 굽기 시간 | 관측 면적 비율 (`observed_projection`) | 채움 면적 비율 (`filled_<variant>`) | 육안 자연스러움 평가 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **방안 a (`tone`)** | `scene_tone.glb` | **`30.02 MB`** | `37.47 s` | **`15.67 s`** | **`53.15 s`** | `58.78%` (`82.39 m²`) | `41.22%` (`57.77 m²`, `filled_tone`) | **가장 자연스럽고 안정적 (1순위 권장)**: 바닥 카펫·벽 하단 미관측 영역이 주변 조명 톤과 이음매 없이 이어지며 가짜 줄무늬나 색 번짐이 전혀 없음 |
| **방안 a-grid (`tone_grid`)** | `scene_tone_grid.glb` | `30.12 MB` | `37.47 s` | `16.28 s` | `53.75 s` | `58.78%` (`82.39 m²`) | `41.22%` (`57.77 m²`, `filled_tone`) | 미관측 영역임을 명시적으로 보여줘야 하는 엔지니어링/진단 뷰어에 적합 |
| **방안 b (`lama`)** | `scene_lama.glb` | `32.24 MB` | `37.47 s` | `131.03 s` (CPU) | `168.50 s` | `58.78%` (`82.39 m²`) | `41.22%` (`57.77 m²`, `filled_lama`) | **벽면(`wall_0`, `wall_1`) 구조 연장에 강점**: 나무 벽 패널 세로선과 창문 틀 직선을 자연스럽게 연장하나, 바닥(`floor`)의 큰 테이블 구멍 내부에 노란 테두리 색을 넓게 번지게 하거나 `wall_2` 좌측 큰 빈 공간에 어두운 수직 그림자 띠를 생성함 |
| **방안 c (`classic`)** | `scene_classic.glb` | `32.97 MB` | `37.47 s` | `40.14 s` | `77.62 s` | `58.78%` (`82.39 m²`) | `41.22%` (`57.77 m²`, `filled_classic`) | **작은 틈 메우기에는 빠르고 가중치 불필요**: 그러나 큰 구멍(바닥 테이블 자리, `wall_2` 좌측)에서 경계의 밝은 점·노란 점이 부채꼴 방사형 줄무늬(Telea streak)로 번지고 패치 합성 입자감이 눈에 띔 |

---

## 6. 한계 및 운영 코드 편입 위치 가이드

### 6.1 현재 한계
1. **실내 전경 가구(회의 탁자·의자·사물함) 옆구리/밑면 구멍**: 바닥(`floor`)과 3개 외곽 벽(`wall_0..2`)의 구멍은 받침 평면으로 완전히 메워져 검은 배경 노출이 사라졌으나, 사물함(`SELF CAFE`) 중앙의 큰 기하 구멍이나 의자 다리 사이의 3D 형상 결손은 평면이 아닌 복잡 형상이므로 하부 벽/바닥 받침 평면이 비쳐 보인다.
2. **큰 마스크에서의 생성형 인페인팅(LaMa) 색 번짐**: 바닥 회의 탁자 자리처럼 구멍 가장자리에 전경 물체 테두리(노란색 탁자 받침)가 맞닿아 있는 대형 구멍에서는 LaMa 가 주변 카펫 대신 노란색 탁자 테두리를 구멍 내부로 연장하는 경향이 있어, 바닥에는 방안 a(`tone`)가 훨씬 깨끗하다.

### 6.2 운영 파이프라인(`ai/src/simai_ai/scene/`) 편입 위치 제안
본 실험 코드는 운영 코드를 수정하지 않고 `ai/experiments/hole_fill_v2/` 에 격리되어 있으며, 추후 운영 파이프라인에 편입할 때의 정확한 결합 지점은 다음과 같다:

1. **`simai_ai/scene/glb_fill.py`**:
   - `fill_holes()` 앞단에 `core.filter_small_connected_components(V, F, min_faces=300, min_area_m2=0.25)` 를 추가해 공중 부유 파편 35개(`1.05%` 면)를 먼저 제거한다.
   - `bake_plane()` 의 단순 단일 카메라 최근접 깊이 비교(`z <= depth + tol`)를 `bake_glb.bake_plane_observed_texture()` 의 **5-tap 깊이 일치 게이트(`|z_cv - d0| <= 0.05 m`) + `core.select_camera_colors_with_outlier_removal()` (Oklab 메도이드 + 2대 후보 이웃 중앙색 3번째 표)** 로 교체한다.
   - 평면 기본 해상도를 `texel_m = 0.008`(최대 `1024 px`)에서 `texel_m = 0.005`(최대 `2048 px`)로 상향하고, 재질에 `"doubleSided": False`(방 안쪽 법선 지향) 및 기하 격자 오프셋(`build_floor_backing_spec`, `build_wall_backing_spec`)을 적용한다.
2. **`simai_ai/scene/glb_inpaint.py`**:
   - `InpaintConfig.mode` 선택지에 `"tone"` (`core.inpaint_tone_plane`) 및 `"hybrid_tone_lama"` (바닥은 `tone`, 벽면은 `lama`)를 추가하고, 관측 ↔ 채움 경계에 `core.feather_observed_filled_boundary(feather_m=0.04)` 를 공통 후처리로 결합한다.
3. **`simai_ai/scene/glb_enhance.py`**:
   - `enhance_glb()` 마무리 단계에서 `core.compute_open_boundary_distances()` 로 열린 구멍 경계 거리를 구한 뒤, `core.blend_atlas_at_hole_boundary()` 와 `core.blend_unobserved_vertex_colors()` 로 구멍 테두리 `8 cm` 구간을 받침 평면 색으로 소프트 블렌딩한다.
