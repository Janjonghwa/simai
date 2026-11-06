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
  - `compare_{view}_diag.jpg`: **1. 단색 진단 보기** 비교 (`원래 맵 | #b8c4d0 단색 + 밝은 배경`)
  - `compare_{view}_vdt_235.jpg`: `[기본 VDT | 수정 후 VDT + 2(배경톤) + 3(바닥층) + 5(경계섞기)]` 비교
  - `compare_{view}_vdt_2345.jpg`: `[수정 전 ②③④⑤ (단순 가림 → 구멍에 의자·탁자 비침) | 수정 후 ②③④⑤ (깊이 5cm 일치만 투영, 빈 곳은 톤+격자)]` 비교
  - `compare_{view}.jpg`: `[원래 맵 | 수정 후 VDT+2+3+4+5 (전체 적용)]` 비교
  - `compare_{view}_stages.jpg`: 한 시점의 **4단계(`① 원래 맵 / ② 시점 의존 / ③ 수정 전 ②③④⑤ / ④ 수정 후 ②③④⑤`) 2×2 한눈에 보기**
  - `compare_{view}_weights.jpg`: 카메라별 가중치 시각화 (`A:빨강, B:초록, C:파랑, D:노랑`)
  - `metrics.json`: 전체 토글 활성화 상태의 그림자 맵 해상도별 FPS 및 렌더 시간 측정치

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

## 5. 브라우저 렌더 성능 재측정 결과 (기본 토글 ②③④⑤ 켬, ① 끔)

- **측정 환경**: Windows 헤드리스 Chrome (`1894×980` 듀얼 뷰포트), GPU `ANGLE (Intel(R) Arc(TM) Graphics Direct3D11)`
- **결과 요약** (`C:/Users/SSAFY/simai-local/vdt/shots/metrics.json`):

| 그림자 맵 해상도 (4대 각각) | 초기 그림자 맵 굽기 (1회) | 프레임당 GPU 렌더 시간 (평균 / P95) | 브라우저 rAF FPS | 순수 렌더 처리량 |
|---|---:|---:|---:|---:|
| `1024 × 576` | `9.1 ms` | `0.33 ms` / `0.60 ms` | **`66.6 fps`** | `3,030 fps` |
| `1536 × 864` (기본) | `45.4 ms` | `0.32 ms` / `0.60 ms` | **`62.8 fps`** | `3,125 fps` |
| `2048 × 1152` | `31.2 ms` | `0.30 ms` / `0.60 ms` | **`60.0 fps`** | `3,333 fps` |

- 바닥 표시 층(삼각형 2개), 벽 표시 층 3면(삼각형 6개), 배경 그라데이션 쿼드(삼각형 2개), 정점별 경계 거리 보간(`vBoundaryDist`)은 모두 가벼운 셰이더 연산으로 처리되어 **60 fps 이상 실시간 렌더링**을 유지한다.

---

## 6. 시점별 비교 평가 및 남은 한계 (`원래 / 시점의존 / 수정 전 ②③④⑤ / 수정 후 ②③④⑤`)

- **시각 비교 요약 (`shots/compare_*_stages.jpg`, `shots/compare_*_vdt_2345.jpg`)**:
  - `obl2` (비스듬히 2): 수정 전(`③`)에는 원형 테이블과 긴 갈색 테이블 사이·기둥 앞 바닥 구멍 자리에 의자 등받이와 파란 다리 사진이 납작하게 달라붙어 섞여 들어간 느낌이 있었으나, 수정 후(`④`)에는 깊이 맵이 비어 있는 구멍 자리에 사진 투영이 차단되어 **차분한 바닥 톤 + 1 m 격자**로 깔끔하게 정리됨.
  - `obl3` (비스듬히 3): 수정 전(`③`)에는 우측 노란 벽(`wall_0`) 하단 구멍 자리에 흰 원형 테이블 상판과 의자 사진이 벽 평면에 납작하게 비쳤으나, 수정 후(`④`)에는 벽 평균 톤 단색 + 1 m 격자로 정직하게 표시되며 창문·액자 등 관측 TSDF 면은 선명하게 유지됨.
  - `top` (위에서 보기): 원형 테이블과 갈색 벤치 주변의 구멍 자리에 납작하게 늘어나 붙던 의자·테이블 가장자리 잔상이 사라지고, 관측된 카펫과 미관측 격자 층이 11 cm 페더링 경계를 통해 자연스럽게 이어짐.
  - `cam_A` ~ `cam_D`, `obl1`: 모든 카메라·대각선 시점에서 원래 맵 메시(관측 면)의 선명한 시점 의존 텍스처는 그대로 유지되면서, 형상이 없는 구멍 자리만 차분한 바닥·벽 톤 + 1 m 격자로 표현됨.
- **남은 한계**:
  - 방 내부 독립 기둥(`obl2` 중앙 기둥 뒷면)이나 정수기/파티션(`obl1` `SELF CAFE` 뒷면)처럼 방 경계 벽 평면(3면)이나 바닥 평면에 속하지 않는 **실내 구조물 자체의 뒷면 형상 결손**은 평면 표시 층의 대상이 아니므로 슬레이트 배경 톤과 경계 페더링으로만 완화된다.
