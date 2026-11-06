# 시점 의존 텍스처(View-Dependent Texture) 및 미관측 구멍 자연화 PoC (`S15P31A101-527`)

고정 카메라 4대(`cam_A`~`cam_D`)의 왜곡 보정 배경 사진과 12차 보정 파라미터(`calibration.json`)를 사용해, 기존 12차 맵 GLB(`current-scene-textured.glb`)의 고정 UV 아틀라스 텍스처 대신 **보는 시점(뷰어 카메라)에 따라 최적 카메라 사진을 실시간 투영·블렌딩**하고(1차 지시), 나아가 **TSDF 형상 구멍(검은 영역)을 지어내지 않고 자연스럽게 메우는 5종 토글**(2차 지시)을 구현·검증한 실험 패키지다.

---

## 1. 파일 구성 및 산출물 위치

### 코드 (`ai/experiments/view_dependent_texture/`)
- [`prepare.py`](file:///C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-527-view-dependent-texture/ai/experiments/view_dependent_texture/prepare.py):
  1. 최신 보정(`cal-lobby-init-da3-exrinsic-fix_20260416_120329`)과 사람 없는 배경 사진(`cam_{A..D}.jpg`, `2304×1296`) 로드
  2. 렌즈 왜곡(`OPENCV_PINHOLE`, $k_1 \approx -0.29$) 보정 및 긴 변 `2048 px`(`2048×1152`) JPEG 저장
  3. `GLTF_Y_UP` 좌표계 기준 카메라 행렬(`K, R, t`, Three.js `viewMatrix`/`projectionMatrix`) 및 다중 시점 노출 보정 계수(`color_gain_srgb`)를 계산해 `cameras.json` 저장
  4. `ai/src/simai_ai/scene/glb_fill.py` 의 `floor_level` 및 `find_wall_planes` (RANSAC 벽 평면 검출)를 호출해 바닥 표시 층(`World Z = -0.015 m`)과 수용된 경계 벽 평면 3면을 `walls.json` 으로 저장
- [`index.html`](file:///C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-527-view-dependent-texture/ai/experiments/view_dependent_texture/index.html) / [`vdt.js`](file:///C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-527-view-dependent-texture/ai/experiments/view_dependent_texture/vdt.js):
  - Three.js(`three@0.170.0`) 기반 좌우 분할 비교 뷰어 (`[원래 맵 | 시점 의존 텍스처 + 2차 개선 토글 1~5]`)
- [`capture_shots.py`](file:///C:/Users/SSAFY/orca/workspaces/S15P31A101/S15P31A101-527-view-dependent-texture/ai/experiments/view_dependent_texture/capture_shots.py):
  - 로컬 HTTP 서버 및 헤드리스 Chrome(WebGL 하드웨어 가속)으로 8개 시점(`cam_A`~`cam_D`, `top`, `obl1`~`obl3`)의 단계별 비교 이미지 42장과 해상도별 FPS 벤치마크(`shots/metrics.json`) 자동 생성

### 실행 산출물 (`C:/Users/SSAFY/simai-local/vdt/`)
- `current-scene-textured.glb`: 12차 맵 GLB 사본 (`10.40 MB`, 삼각형 `150,247`개, 열린 경계 모서리 `8,659`개)
- `cameras.json`: GLB 좌표계 기준 4대 카메라 내부·외부 파라미터, 노출 보정 계수, 자유 시점 프리셋
- `walls.json`: 바닥 미관측 표시 층(`World Z = -0.015 m`) 및 `find_wall_planes` 수용 벽 평면 3면(`wall_0`, `wall_1`, `wall_2`)의 좌표·법선·관측 대표 톤
- `images/cam_{A..D}.jpg`: 왜곡 보정 및 `2048×1152` 리사이즈된 배경 사진 4장
- `shots/`:
  - `compare_{view}_r5.jpg`: **5차 지시** `[3차 (외톨이제거 전) | 4차 (강한 외톨이제거) | 5차 (완화된 σ·소프트 감쇠·공간 매끄러움)]` 3칸 비교 (`obl3`, `obl2`, `obl1`, `top`, `cam_A`~`cam_D`)
  - `compare_{view}_r4.jpg`: **4차 지시** `[3차 결과 (미관측투영 켬 · 외톨이제거 없음) | 4차 결과 (미관측투영 끔 · 카메라간 일치 외톨이제거)]` 좌우 비교 (`obl2`, `obl3`, `top`, `obl1`, `cam_A`~`cam_D`)
  - `compare_obl2_r4_unobs_on.jpg`, `compare_obl3_r4_unobs_on.jpg`: `미관측 TSDF 면도 투영`을 켰을 때도 5 cm 깊이 일치 게이트 + 외톨이 제거가 적용된 비교
  - `compare_{view}_diag.jpg`: **1. 단색 진단 보기** 비교 (`원래 맵 | #b8c4d0 단색 + 밝은 배경`)
  - `compare_{view}_vdt_235.jpg`: `[기본 VDT | 수정 후 VDT + 2(배경톤) + 3(바닥층) + 5(경계섞기)]` 비교
  - `compare_{view}_vdt_2345.jpg`: `[수정 전 ②③④⑤ (단순 가림 → 구멍에 의자·탁자 비침) | 4차 수정 후 ②③④⑤ (깊이 5cm 일치 + 외톨이 제거)]` 비교
  - `compare_{view}.jpg`: `[원래 맵 | 4차 수정 후 VDT+2+3+4+5 (전체 적용)]` 비교
  - `compare_{view}_stages.jpg`: 한 시점의 **4단계(`① 원래 맵 / ② 시점 의존 / ③ 수정 전 ②③④⑤ / ④ 수정 후 ②③④⑤`) 2×2 한눈에 보기**
  - `compare_{view}_weights.jpg`: 카메라별 가중치 시각화 (`A:빨강, B:초록, C:파랑, D:노랑`)
  - `metrics.json`: 전체 토글 활성화 상태(후보 4대 모두 샘플링 포함)의 그림자 맵 해상도별 FPS 및 렌더 시간 측정치

---

## 2. 로컬 실행 방법

```powershell
# 1. 입력 데이터 준비 (왜곡 보정 이미지 + cameras.json + walls.json 생성)
uv run --project ai --with opencv-python-headless python ai/experiments/view_dependent_texture/prepare.py

# 2. 브라우저 확인용 로컬 정적 서버 실행 (C:/Users/SSAFY/simai-local/vdt)
python -m http.server 8080 --directory C:/Users/SSAFY/simai-local/vdt
# 브라우저에서 http://127.0.0.1:8080/index.html 접속

# 3. 비교 스크린샷 일괄 캡처 및 FPS 재측정
uv run --project ai python ai/experiments/view_dependent_texture/capture_shots.py
```

---

## 3. 1차 지시 요약 — 좌표계 변환 및 실시간 시점 의존 투영

- **GLB ↔ OpenCV 카메라 좌표 변환**:
  - `texture_mesh.py` 의 `GLTF_Y_UP` 회전 행렬 $G = \begin{bmatrix} 1 & 0 & 0 \\ 0 & 0 & 1 \\ 0 & -1 & 0 \end{bmatrix}$ ($\det(G) = +1$)에 의해 $\mathbf{p}_{\text{world}} = G^T \mathbf{p}_{\text{glb}}$ 이므로,
    $$\mathbf{p}_{\text{cam,cv}} = (R_{\text{w2c}} G^T)\,\mathbf{p}_{\text{glb}} + \mathbf{t}_{\text{w2c}} = R_{\text{glb2cv}}\,\mathbf{p}_{\text{glb}} + \mathbf{t}_{\text{glb2cv}}$$
- **실시간 가림 검사 및 각도 가중 블렌딩**:
  - 4대 카메라 시점의 선형 깊이 그림자 맵(`uCamDepth0..3`, 기본 `1536×864`)으로 가구·벽에 가려진 카메라를 제외하고, Unstructured Lumigraph 각도 가중치 $w_{\text{view}} = \frac{\exp(8.5(\cos\alpha - 1))}{\max(1 - \cos\alpha, 0.001)}$ 와 $(K+1)$번째 문턱값 차감으로 상위 2~3대 카메라를 $C^0$ 연속 블렌딩한다.

---

## 4. 2차 지시 구현 상세 — 검은 구멍을 자연스럽게, 지어내지 않고

### ① 단색 진단 보기 (`1. 단색 진단 보기` 토글)
- 모든 TSDF 삼각형을 단색(`#b8c4d0`, `DoubleSide`, 완만한 양면 법선 음영)으로 칠하고 배경을 밝은 슬레이트 백색(`#e2e8f0`)으로 렌더링한다 (`shots/compare_*_diag.jpg`).
- **진단 결과**: 12차 GLB 에서 검게 보이던 바닥 구멍(원형 테이블 주변, 기둥 뒤쪽)과 벽 하단 구멍(노란 벽 테이블 뒤, 창문 아래)에서 밝은 `#e2e8f0` 배경이 그대로 투과되어 보인다. 즉 해당 검은 영역은 **텍스처 누락이 아니라 TSDF 형상 자체에 삼각형이 없는 열린 구멍(missing geometry)** 임을 눈으로 명확히 확인했다.

### ② 배경 톤 (`2. 배경 톤+안개` 토글)
- 완전 검정(`#0b0f17`) 대신 교실 분위기·카펫 톤과 어울리는 어두운 슬레이트-블루 수직 그라데이션(`#18202c` 상단 → `#283445` 중앙 → `#1e2735` 하단)을 풀스크린 쿼드로 깔고, `8 m ~ 25 m` 구간에 완만한 거리 안개(`#253041`, 최대 `38%`)를 적용해 외곽 절단면이 검게 튀지 않게 했다.

### ③ 바닥 미관측 표시 층 (`3. 바닥 미관측 표시 층` 토글)
- **위치**: `World Z = 0` 기준 `1.5 cm` 아래인 `z_world = -0.015 m` (`GLB Y = -0.015 m`)에 방 바닥 범위(수용된 3개 벽 평면 경계 + 열린 `+Y` 방향 여유 `0.45 m`) 사각형 평면을 배치했다. 맵 TSDF 바닥면 중앙값(`+0.0461 m`)보다 `6.1 cm` 아래에 있어 z-fighting 이 전혀 발생하지 않는다.
- **스타일 (미관측 사각지대)**: 가짜 카펫 텍스처를 지어내지 않고, 관측된 바닥 삼각형 중앙값 색(`sRGB [0.412, 0.437, 0.488]`)에 맞춘 차분한 단색(`sRGB [0.348, 0.369, 0.412]`) 위에 화면 미분(`fwidth`) 기반 모아레 없는 **1 m 주 격자선 + 0.5 m 보조 격자선**을 그려 미관측 영역임을 정직하게 표시한다.
- **실제 사진 투영 조건 (3차 지시 반영: 깊이 5 cm 일치 + 형상 존재 검사)**:
  - 2차 지시(수정 전)에서는 단순히 전경 가림(`zCv - d > tolOccl`)만 검사했기 때문에, 원래 의자·탁자가 있던 자리인데 TSDF 맵에 의자·탁자 형상이 빠져 깊이 그림자 맵이 비어 있는 곳(`d = uFar = 25 m`)에서도 가림 검사를 통과해 의자·탁자 사진이 바닥·벽 표시 층에 납작하게 달라붙는 문제("섞여 들어간 느낌")가 발생했다.
  - **3차 지시 해결**:
    1. **깊이 그림자 맵에 전체 TSDF 형상 기록**: 바닥·벽·가구의 모든 TSDF 삼각형을 카메라별 깊이 그림자 맵(`uCamDepth0..3`)에 기록하고, 형상이 없는 구멍 픽셀만 클리어 값(`d = uFar = 25.0 m`)으로 남긴다.
    2. **표시 층 깊이 5 cm 일치 게이트 (`INDICATOR_DEPTH_MATCH_TOL = 0.05 m`)**: 바닥·벽 표시 층(`uIsIndicatorPlane == 1`)에 카메라 사진을 비출 때는 5-tap 깊이 그림자 맵에 **맵 형상이 실제로 존재(`d < uFar - 0.5`)**하고, 그 깊이가 표시 층 표면 위치(`projPos`)의 카메라 깊이와 **`5 cm`(`0.05 m`) 이내로 일치(`max |zCv - d_i| <= 0.05 m`)**할 때만 사진을 투영한다.
    3. **5 cm 근거**: TSDF 바닥면 높이 사분위 편차(`±2.5 cm`)와 `find_wall_planes` 의 RANSAC 평면 inlier 허용치(`4 cm`)를 포괄하는 최소 물리 정합 임계값이 `5 cm` 다. 깊이 맵이 비어 있거나(형상 없음 = 카메라가 무엇을 봤는지 모름) 깊이 차가 `5 cm` 를 넘으면 사진을 쓰지 않고 **차분한 단색 톤 + 1 m 격자**만 표시한다. 원래 맵 메시(관측 면, `uIsIndicatorPlane == 0`)의 시점 의존 텍스처는 그대로 유지한다.
    4. **표시 층 전용 `clearDepth` 패스 분리**: 바닥·벽 표시 층(`indicatorScene`)을 먼저 그린 뒤 깊이 버퍼만 비우고(`renderer.clearDepth()`) TSDF 본 메시(`scene`)를 그린다. 이를 통해 벽면보다 바깥으로 오목하게 들어간 창문(`obl2`, `obl3`)이나 곡률이 있는 TSDF 벽·바닥을 표시 층 평면이 절대 가리지 않으며, 오직 TSDF 기하가 비어 있는 구멍 픽셀에만 표시 층이 노출된다.

### ④ 벽 미관측 표시 층 (`4. 벽 미관측 표시 층(3면)` 토글)
- `prepare.py` 에서 `simai_ai.scene.glb_fill.find_wall_planes` 를 호출해 수용된 **방 경계 벽 평면 3면**을 `C:/Users/SSAFY/simai-local/vdt/walls.json` 으로 내보냈다:
  - `wall_0` (좌측 노란 벽, $x \approx 0.07\text{ m}$, 길이 $9.67\text{ m}$, 높이 $2.55\text{ m}$, 관측 중앙 톤 `[0.683, 0.611, 0.508]`)
  - `wall_1` (우측 창가 벽, $x \approx 8.24\text{ m}$, 길이 $5.62\text{ m}$, 높이 $2.51\text{ m}$, 관측 중앙 톤 `[0.725, 0.654, 0.554]`)
  - `wall_2` (후면 사물함/SSAFY 벽, $y \approx -0.16\text{ m}$, 길이 $6.71\text{ m}$, 높이 $2.45\text{ m}$, 관측 중앙 톤 `[0.576, 0.642, 0.707]`)
  - 열린 `+Y` 방향과 방 내부 파티션 후보 3개(`NOT_ROOM_BOUNDARY`, `DUPLICATE`)는 기각되어 **수용 평면이 없는 방향에는 평면을 만들지 않는다**.
- 방 안쪽을 향하는 단면(`dot(N, viewDir) > 0.02`)으로만 렌더링하여 벽 근처에 설치된 `cam_C`·`cam_D` 의 시야를 뒷면이 가리지 않게 했다.
- 바닥과 동일하게 깊이 그림자 맵에 형상이 있고 깊이가 `5 cm` 안으로 일치할 때만 벽 사진을 투영하며, 형상이 없는 벽 구멍은 해당 벽의 평균 톤 단색 + 1 m 격자로 표시한다.

### ⑤ 구멍 경계 부드럽게 섞기 (`5. 구멍 경계 부드럽게 섞기` 토글) — 방법 및 선택 근거
- **선택한 방법 (위상 정점 용접 기반 열린 경계 거리장 `aBoundaryDist` + 셰이더 11 cm 연속 색상 페더링)**:
  1. GLB 로드 시 UV 아틀라스 경계로 분리된 정점들을 `1 mm` 공간 해시 격자로 용접(weld)한 뒤, 공유 삼각형이 **정확히 1개뿐인 무방향 간선**(`8,659`개)을 찾아 진짜 TSDF 형상 구멍 테두리 정점(`8,477`개)과 간선 중점을 추출한다.
  2. `8 cm` 3차원 공간 해시 격자로 모든 정점에서 가장 가까운 열린 구멍 경계까지의 유클리드 거리 `aBoundaryDist` (최대 `0.12 m` 캡)를 계산해 정점 속성(`BufferAttribute`)으로 바인딩한다(초기화 소요 시간 `< 15 ms`).
  3. 프래그먼트 셰이더에서 열린 구멍 경계로부터 `11 cm` 구간(`vBoundaryDist < 0.11 m`)에 대해 `t = smoothstep(0.0, 0.11, vBoundaryDist)` 가중치로 하부 표시 층(바닥/벽 단색+격자) 및 슬레이트 배경 톤과 연속 블렌딩하고, 바닥의 미관측 삼각형(`unobserved_neutral`) 폴백 톤도 바닥 표시 층 톤에 일치시킨다.
- **선택 근거 (기각한 대안과의 비교)**:
  - *기각한 대안*: 경계 정점의 알파를 `0` 으로 두는 하드웨어 알파 블렌딩 / `discard` 방식은 폭이 좁은 삼각형 브리지(3개 정점이 모두 구멍 경계에 접한 삼각형) 전체의 알파가 `0` 이 되어 멀쩡한 바닥 삼각형이 뚫리는 부작용이 발생했다.
  - *채택 근거*: 정점별 열린 경계 거리(`vBoundaryDist`)를 따라 하부 표시 층의 색상(`floorGridAtPos` / 배경 톤)으로 색상을 $C^0$ 연속 수렴시키면, 좁은 삼각형 형상을 조금도 손실하지 않으면서 구멍 테두리의 톱니 모양 단차와 밝은 회색(`unobserved_neutral`) 띠가 매끄럽게 사라진다.

---

## 5. 4차 지시 구현 상세 — 남은 두 종류의 "섞여 들어간" 잔상 제거

### ① `obl2` 회색 바닥 층 위 반투명 탁자·의자 잔상 제거 (`미관측 TSDF 면도 투영` 기본 끔 + 5 cm 게이트)
- **원인**: 12차 GLB 의 `unobserved_neutral` 프리미티브(`uHasBaseMap == 0`, 구운 텍스처가 없는 미관측 TSDF 삼각형)에 카메라 사진을 투영(`uProjectUnobserved == 1`)하면, 탁자·의자 주변의 불완전한 미관측 삼각형 조각 위에 카메라의 탁자·의자 사진이 비치면서 하부 회색 바닥 층(`obl2`) 위에 반투명하게 떠 보이는 잔상이 발생했다.
- **해결**:
  1. `미관측 TSDF 면도 투영` 토글(`#chk-project-unobs`)의 기본값을 **끔(`checked` 제거, `sharedUniforms.uProjectUnobserved.value = 0`)** 으로 변경했다.
  2. 사용자가 해당 토글을 켰을 때(`uProjectUnobserved == 1`)도 표시 층과 동일하게 **깊이 그림자 맵에 맵 형상이 존재하고 중심 깊이가 `5 cm`(`0.05 m`) 이내로 일치할 때만** 투영하도록 게이트(`uUnobsDepthMatch = 1`)를 적용했다.
  3. 미관측 투영을 끈 상태에서 `unobserved_neutral` 바닥 삼각형 정점에 구워진 짙은 그림자(`luma < 0.37`)가 검은 조각으로 튀지 않도록 관측 바닥 대표 톤(`uIndicatorObsColor * 0.88`)으로 완만하게 보정했다.

### ② `obl3` 실제 바닥(관측 면) 위 반투명 탁자·스툴 다리 줄 제거 (`카메라 간 일치(외톨이 제거)`)
- **원인**: 스툴·탁자의 얇은 금속 다리(직경 `2~3 cm`)는 TSDF 메시 형상에 복원되지 않아 깊이 그림자 맵 가림 검사를 통과한다. 그 결과 한 카메라 사진에 찍힌 다리가 바닥 관측 면(`uHasBaseMap == 1`)에 투영된 뒤 다른 카메라의 카펫 사진과 섞여 반투명한 검은 다리 줄(`obl3`, `top`, `cam_B`)로 남았다.
- **해결 (`uOutlierRemoval = 1`, 기본 켬)**:
  1. **후보 카메라 전수 샘플링 및 Oklab 색 공간 변환**: 프래그먼트마다 가림·법선·화면 경계를 통과한 모든 후보 카메라(`geomW_i > 0.005`, 최대 4대)의 색을 먼저 샘플링하고 지각적 균일 색 공간인 **Oklab** 으로 변환한다.
  2. **후보 수별 기준색(`refLab`) 결정**:
     - **후보 3대 이상 (`nCand >= 3`)**: 다른 후보들과의 Oklab 색 거리 합이 가장 작은 **메도이드(medoid)** 후보를 기준(`refLab`)으로 정한다(2대2 동률 해소를 위해 구운 텍스처와의 색 거리를 보조 가중치로 반영).
     - **후보 2대 (`nCand == 2`)**: **구운 텍스처 색(`thirdVoteLab`)을 세 번째 표**로 써서 두 후보 중 색 거리 합이 작은 메도이드 후보를 기준(`refLab`)으로 정한다. 바닥에서 구운 텍스처 자체가 어두운 그림자로 오염된 경우에는 관측 바닥 중앙값(`floorObsLab`)으로 세 번째 표를 보정한다.
     - **후보 1대 (`nCand == 1`)**: 비교할 다른 카메라가 없으므로 기본적으로 유지하되, 바닥 관측 면에서 그 색이 구운 텍스처(노출 보정 게인 반영)와 같은 $\sigma$ 기준으로 크게 다르면 다리 잔상으로 판정해 감쇠하고 **구운 텍스처(`uBaseMap`)** 로 대체한다.
  3. **근거 있는 $\sigma$ 설정 및 외톨이 버리기**:
     - `prepare.py` 에서 색 보정(`color_gain_srgb`) 적용 후 2대 이상 중첩 가시 영역(`234,044`개 카메라 쌍)의 카메라 간 Oklab 색 거리 분포를 실측해 `cameras.json` 에 기록했다:
       - 전체 겹침 쌍 중앙값 `p50 = 0.0542`, 3대 이상 메도이드 인라이어 `p75 = 0.0506`, 전체 쌍 `p75 = 0.1389`.
     - 이에 근거해 바닥 면의 기준 $\sigma$ 를 `uColorSigma = 0.056`(인라이어 `p75 = 0.0506` 및 전체 중앙값 `0.0542` 기준)으로, 비바닥(벽·간판 등)의 $\sigma_{\text{eff}}$ 를 `2.25 * uColorSigma = 0.126`(전체 겹침 `p75 = 0.1389` 기준)으로 설정했다.
     - 각 후보의 색 거리 $d_i = \|\text{lab}_i - \text{refLab}\|$ 에 대해 가중치를 $w_i \leftarrow w_i \cdot \exp(-(d_i/\sigma)^2) \cdot g(d_i/\sigma)^2$ 로 감쇠하고, 기준과 크게 다른 외톨이 후보($d_i/\sigma > 1.55$)는 완전히 버린($g = 0$) 뒤 남은 인라이어 후보만 재정규화해 섞는다. 또한 바닥에서 시점 방향과 우연히 정렬된 외톨이 카메라의 각도 가중치($w_{\text{view}}$)가 인라이어 카메라를 압도하지 않도록 외톨이 감쇠율에 비례해 $w_{\text{view}}$ 도 함께 수렴시켰다.

---

## 6. 브라우저 렌더 성능 재측정 결과 (창 크기 `1900×1000`, 기본 토글 ②③④⑤ + 외톨이 제거 켬, 미관측 투영 끔)

- **측정 환경**: Windows 헤드리스 Chrome (창 크기 `1900×1000`, 실 뷰포트 `1874×900` 좌우 분할), GPU `ANGLE (Intel(R) Arc(TM) Graphics Direct3D11)`
- **결과 요약** (`C:/Users/SSAFY/simai-local/vdt/shots/metrics.json`, 후보 4대 모두 텍스처 샘플링 포함):

| 그림자 맵 해상도 (4대 각각) | 초기 그림자 맵 굽기 (1회) | 프레임당 GPU 렌더 시간 (평균 / P95) | 브라우저 rAF FPS | 순수 렌더 처리량 |
|---|---:|---:|---:|---:|
| `1024 × 576` | `3.7 ms` | `0.16 ms` / `0.30 ms` | **`67.9 fps`** | `6,250 fps` |
| `1536 × 864` (기본) | `84.2 ms` | `0.15 ms` / `0.30 ms` | **`58.5 fps`** | `6,667 fps` |
| `2048 × 1152` | `48.1 ms` | `0.19 ms` / `0.50 ms` | **`56.5 fps`** | `5,263 fps` |

- 후보 카메라 4대의 색을 모두 샘플링하고 Oklab 메도이드 외톨이 제거 연산을 추가했음에도 프레임당 GPU 렌더 시간은 평균 `0.15~0.19 ms` (P95 `0.30~0.50 ms`), 브라우저 rAF 기준 **`58.5~67.9 fps`** 로 실시간 렌더링 성능을 그대로 유지한다.

---

## 7. 시점별 비교 평가 및 남은 한계 (`[3차 결과 | 4차 결과]`, `shots/compare_*_r4.jpg`)

- **시각 비교 요약 (`shots/compare_*_r4.jpg`)**:
  - `obl2` (`compare_obl2_r4.jpg`): 3차 결과(왼쪽)에서 회색 바닥 표시 층 위에 반투명하게 떠 있던 탁자 상판·의자 잔상이 4차 결과(오른쪽, `미관측 TSDF 면도 투영` 기본 끔)에서 **완전히 사라져** 차분한 회색 바닥 층 + 1 m 격자만 깔끔하게 표시된다. `미관측 TSDF 면도 투영`을 켠 상태(`compare_obl2_r4_unobs_on.jpg`)에서도 5 cm 깊이 일치 게이트에 의해 회색 바닥 위 반투명 탁자·의자 잔상이 차단된다.
  - `obl3` (`compare_obl3_r4.jpg`): 3차 결과(왼쪽)에서 스툴·원형 탁자 아래 실제 관측 바닥(`uHasBaseMap == 1`) 카펫 위로 길게 뻗어 있던 4줄의 반투명 검은 스툴·탁자 다리 줄이 4차 결과(오른쪽, `카메라 간 일치(외톨이 제거)`)에서 **깨끗하게 제거**되었고, 바닥 중앙의 검은 그림자 얼룩도 카펫 톤으로 자연스럽게 정돈되었다.
  - `top` (`compare_top_r4.jpg`): 두 갈색 벤치 주변과 원형 탁자 주변 바닥에 방사형으로 뻗어 있던 검은 다리 잔상과 붉은 라바콘 번짐이 제거되고 벤치 상판·원형 탁자·바닥 카펫 경계가 한층 선명해졌다.
  - `cam_A` ~ `cam_D`, `obl1` (`compare_cam_A_r4.jpg`~`compare_cam_D_r4.jpg`, `compare_obl1_r4.jpg`): `cam_B` 우측 하단 바닥에 굵게 비치던 검은 의자 다리 막대 잔상과 `cam_A`·`cam_D` 의 스툴 다리 잔상이 제거되면서도, 벽면 액자(`obl1`, `cam_A`, `cam_B`)와 `SSAFY`·`SELF CAFE`·`LOCKER`·`SAMSUNG` 간판 글씨는 손실이나 이중 상 없이 선명하게 유지된다.
- **남은 한계**:
  - 방 내부 독립 기둥(`obl2` 중앙 기둥 뒷면)이나 정수기/파티션(`obl1` `SELF CAFE` 뒷면)처럼 방 경계 벽 평면(3면)이나 바닥 평면에 속하지 않는 **실내 구조물 자체의 뒷면 형상 결손**은 평면 표시 층의 대상이 아니므로 슬레이트 배경 톤과 경계 페더링으로만 완화된다.

---

## 8. 5차 지시 구현 상세 — 3차의 바닥·가구 매끄러움 복원 + 다리 잔상 제거 (`compare_{view}_r5.jpg`)

### ① 4차에서 바닥(카펫)·가구 표면이 거칠고 조각나 보였던(`줄무늬·얼룩`) 원인 분석
1. **메시 내부 `unobserved_neutral`(`uHasBaseMap == 0`) 삼각형 교차 배치로 인한 대각선 줄무늬·조각**:
   - `current-scene-textured.glb` 의 `unobserved_neutral` 프리미티브(`47,606`개 정점)는 `obl2` 바닥 구멍 가장자리뿐 아니라 `obl3` 좌하단 카펫 바닥(`5,707`개 정점, `2.7 cm` 간격으로 `textured_0000` 과 교차)과 스툴·탁자·창틀·벽면 테두리(`26,022`개 정점)에 걸쳐 분포한다.
   - 4차에서 `uHasBaseMap == 0` 전체의 투영을 일괄 차단(`earlyReturnUnobs`)하자, 카펫 바닥의 교차 삼각형 띠가 대각선 줄무늬(`줄무늬`)로 드러나고 가구·창틀 표면이 삼각형 단위로 조각나 보였다.
2. **과도하게 좁은 $\sigma = 0.056$, 가중치 `0` 절단, 픽셀별 이산 메도이드 전환으로 인한 얼룩**:
   - 4차에서는 각 픽셀마다 `if (sum1 < bestSum)` 이산 분기로 단일 기준 카메라(`refLab`)를 고르고, $d_n > 1.55$ 에서 가중치를 `0` 으로 끊었으며(`1.0 - smoothstep(1.05, 1.55, dn)`), 바닥에서 $w_{\text{view}}$ 를 `mix(0.25, wView + 0.25, a)` 로 비선형 왜곡했다.
   - 그 결과 카펫 직조 결이나 후보 카메라 수 변화(`3대 → 2대 → 1대`) 경계에서 픽셀마다 선택 카메라와 가중치가 급격히 바뀌며 얼룩(`얼룩`)이 발생했다.

### ② 5차 해결 방법 (`uOutlierRemoval == 1`)
1. **완화된 $\sigma$ 및 인라이어 불감대(deadzone) 도입**:
   - 바닥 $\sigma$ 를 `0.056` → **`0.095`** (약 `1.7`배 완화), 비바닥(가구·벽) $\sigma$ 를 `0.126` → **`0.220`** 으로 완화하고, 정상 카펫 결·조명 편차를 전혀 건드리지 않도록 인라이어 불감대(`deadzone`: 바닥 `0.042`, 비바닥 `0.065`)를 두었다.
2. **가중치를 `0` 으로 끊지 않는 소프트 가우시안 감쇠 및 $w_{\text{view}}$ 원형 유지**:
   - 하드 컷오프(`1.0 - smoothstep(...)`)를 제거하고 최소 가중치 바닥값(`minWeightFactor = 0.08`) 위로 완만하게 줄이는 $a_i = \text{mix}(0.08,\,1.0,\,\exp(-d_{n,i}^2))$ 를 적용했다.
   - $w_{\text{view}}$ 비선형 왜곡을 제거하고 순수하게 $w_i \leftarrow w_i \cdot a_i$ 만 곱해 후보 카메라가 1대로 줄어드는 경계에서도 가중치가 튀지 않는다.
3. **5-tap 공간 저역통과(`readCamPhotoLowPass`) + 연속 소프트 메도이드(`soft-medoid`) 합의**:
   - 렌더링 색상(`c0..c3`)은 원본 해상도(`readCamPhoto`)의 선명한 카펫 결을 그대로 유지하되, Oklab 색 거리 판정에만 반경 `3.0 texel` 5-tap 공간 저역통과 필터(`readCamPhotoLowPass`)를 사용해 픽셀 단위 직조 노이즈로 인한 떨림을 제거했다.
   - 이산 `if (sum1 < bestSum)` 분기 대신 연속 존재 가중치 $p_i = \text{smoothstep}(0.002, 0.025, \text{geomW}_i)$ 와 볼츠만 가중치 $m_i = p_i \exp(-s_i / T)$ 로 가중 평균한 연속 합의 기준색(`refLab`)을 사용해 픽셀마다 선택 카메라가 바뀌어 생기는 얼룩을 원천 제거했다.
   - 바닥(`y < 7.5 cm`, 상향 법선 $|N_y| > 0.55$)에서 강한 외톨이($a_i < 0.70$ 인 검은 다리 잔상 등)가 시점 각도 가중치($w_{\text{view}}$) 우세로 남을 때만 다른 인라이어 카메라들의 실제 카펫 텍스처($cw_k = m_k a_k^2$)로 부드럽게 수렴시켜, 단색 패치 없이 실제 카펫 결을 유지하면서 다리 잔상만 제거했다.
4. **바닥 `uHasBaseMap == 0/1` 처리 일원화 및 `obl2` 구멍 고립 조각 선택적 차단**:
   - 바닥(`isFloor`)에서는 `uHasBaseMap == 0` 과 `uHasBaseMap == 1` 이 3차와 완전히 동일한 깊이 가림 검사와 카펫 기준색을 공유하도록 일원화하여 `obl3`·`obl1` 바닥의 대각선 줄무늬를 완전히 제거했다.
   - 비바닥 `uHasBaseMap == 0` 은 중심 탭 수직 깊이 오차(`centerPerpDiff <= 5.5 cm`)와 연속 인라이어 기하 합의(`smoothstep(0.018, 0.075, sumInlierGeomW)`)로 `obl2` 구멍 위의 가려진 의자·탁자 잔상만 차단하고 가구·창틀·걸레받이 표면은 3차와 동일하게 매끄럽게 유지했다.

### ③ 5차 브라우저 렌더 성능 및 `[3차 | 4차 | 5차]` 비교 결과 (`shots/compare_*_r5.jpg`)
- **성능 (`C:/Users/SSAFY/simai-local/vdt/shots/metrics.json`)**:
  - `1024×576`: 평균 `0.18 ms` (P95 `0.40 ms`), 브라우저 rAF **`65.2 fps`**
  - `1536×864` (기본): 평균 `0.17 ms` (P95 `0.40 ms`), 브라우저 rAF **`62.3 fps`**
  - `2048×1152`: 평균 `0.24 ms` (P95 `0.50 ms`), 브라우저 rAF **`57.5 fps`**
- **시각 검증 (`shots/compare_{view}_r5.jpg`)**:
  - `compare_obl3_r5.jpg`: 3차(왼쪽)의 검은 스툴·탁자 다리 잔상과 4차(가운데)의 바닥 대각선 줄무늬·조각난 가구 표면이 5차(오른쪽)에서 **동시에 해결**되어, 바닥 카펫·걸레받이·창틀·스툴 표면이 3차만큼 매끄러우면서 다리 잔상만 깨끗하게 제거되었다.
  - `compare_obl2_r5.jpg`: 회색 바닥 표시 층 위의 반투명 탁자·의자 잔상이 5차에서도 완전히 제거되면서, 4차에서 기둥·창가 벽 테두리에 생겼던 회색 조각 테두리가 사라져 3차와 동일한 매끄러움을 유지한다.
  - `compare_obl1_r5.jpg`, `compare_top_r5.jpg`, `compare_cam_A_r5.jpg`~`compare_cam_D_r5.jpg`: 모든 시점에서 4차의 바닥·가구·벽 조각 현상이 사라지고 3차 이상의 표면 매끄러움을 달성했다.
