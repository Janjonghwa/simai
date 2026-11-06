# 시점 의존 텍스처 (View-Dependent Texture, S15P31A101-527) PoC

고정 CCTV 카메라 4대(`cam_A`~`cam_D`)의 실제 배경 사진(왜곡 폄)을 12차 맵(`current-scene-textured.glb`) 형상 위에 **관제 뷰어가 보는 시점 각도에 따라** 실시간 투영·혼합(Unstructured Lumigraph 방식)하는 PoC 다.

---

## 1. 파일 구성 및 산출물 위치

- 실험 코드 (`ai/experiments/view_dependent_texture/`):
  - `prepare.py`: 보정(`calibration.json`) 및 사람 없는 배경 사진(`cam_{A..D}.jpg`) 왜곡 펴기(새 `K`), 긴 변 2048(`2048x1152`) JPEG 저장, GLB 좌표계(`GLTF_Y_UP`) 기준 외부 파라미터·Three.js 행렬·다중 시점 겹침 기반 색 보정 계수(`color_gain_srgb`)를 `cameras.json` 에 기록, GLB 및 뷰어 파일 복사.
  - `index.html`, `vdt.js`: Three.js(`three@0.170.0` CDN importmap) 기반 좌/우 동기화 비교 뷰어 (`[원래 구운 텍스처 | 시점 의존 텍스처]`).
  - `capture_shots.py`: 로컬 HTTP 서버 + 헤드리스 브라우저(WebGL)로 8개 시점(`cam_A`~`cam_D`, `top`, `obl1`~`obl3`) 및 가중치 시각화 비교 이미지를 자동 캡처하고 그림자 맵 해상도별 FPS 를 측정.
- 산출물 (`C:/Users/SSAFY/simai-local/vdt/`, 저장소 밖):
  - `cameras.json`: GLB 좌표계 기준 카메라 4대 내부·외부 파라미터 및 색 보정 계수
  - `images/cam_{A..D}.jpg`: 왜곡을 편 `2048x1152` JPEG 배경 사진
  - `current-scene-textured.glb`: 12차 맵 GLB 사본
  - `shots/compare_{cam_A..D,top,obl1..3}.jpg`, `shots/compare_*_weights.jpg`, `shots/metrics.json`

---

## 2. 실행 및 로컬 서버로 띄우는 방법

### (1) 입력 준비 (`prepare.py`)
```powershell
uv run --project ai --with opencv-python-headless python ai/experiments/view_dependent_texture/prepare.py
```

### (2) 로컬 웹 서버로 시험 페이지 띄우기 (`python -m http.server`)
`prepare.py` 가 `C:/Users/SSAFY/simai-local/vdt/` 안에 `index.html`, `vdt.js`, `cameras.json`, `current-scene-textured.glb`, `images/` 를 모두 준비하므로 아래 명령 한 줄로 띄운다:

```powershell
python -m http.server 8765 --directory C:/Users/SSAFY/simai-local/vdt
```
브라우저에서 `http://localhost:8765/index.html` 을 열면 좌/우 비교 화면이 뜬다.
- 화면 하단 버튼으로 `cam_A`~`cam_D`, `위에서 보기(Top)`, `비스듬히 1~3(Obl1~3)` 시점 이동, `가중치 시각화`, `색 보정(노출 맞춤)`, `상위 2/3대 섞기`, `그림자 맵 해상도(1024/1536/2048)`, `캡처 저장`을 바로 조작할 수 있다.

### (3) 확인 이미지 일괄 캡처 및 성능 측정 (`capture_shots.py`)
```powershell
uv run --project ai python ai/experiments/view_dependent_texture/capture_shots.py
```

---

## 3. 방법 (알고리즘 및 좌표 변환)

1. **좌표 규약 일치 (`GLTF_Y_UP`)**:
   - `ai/src/simai_ai/scene/texture_mesh.py` 의 `GLTF_Y_UP = [[1,0,0],[0,0,1],[0,-1,0]]` ($G$) 규약에 따라 World($+Z$ 상방) 정점 $p_{\text{world}}$ 는 GLB($+Y$ 상방) 정점 $p_{\text{glb}} = G \, p_{\text{world}} = (x, z, -y)^T$ 로 저장된다.
   - 따라서 $p_{\text{world}} = G^T p_{\text{glb}}$ 이고, OpenCV 월드→카메라 변환 $p_{\text{cv}} = R_{\text{w2c}} p_{\text{world}} + t_{\text{w2c}}$ 는 GLB 정점 기준 $p_{\text{cv}} = (R_{\text{w2c}} G^T) p_{\text{glb}} + t_{\text{w2c}}$ ($R = R_{\text{w2c}} G^T, t = t_{\text{w2c}}$)가 된다.
2. **카메라별 깊이 그림자 맵 (Depth Shadow Map)**:
   - GLB 로드 시 4대 카메라 각각의 핀홀 내부 행렬 `K` 와 외부 행렬로 구성한 Three.js 카메라에서 선형 깊이($z_{\text{cv}} / Z_{\text{far}}$)를 `packDepthToRGBA` 32비트로 `WebGLRenderTarget` 에 1회 굽는다(카메라와 배경 메시가 고정이므로 매 프레임 다시 그리지 않아 60+ fps 유지).
3. **조각(Fragment)별 후보 판정 및 가중치 (Unstructured Lumigraph)**:
   - 각 조각의 GLB 월드 위치 $P$ 를 4대 카메라에 투영해 화면 안($u, v \in (0.005, 0.995)$), 정면 법선($\cos \theta_{\text{norm}} = N \cdot \vec{v}_{\text{cam}} > \cos_{\min}$), 깊이 그림자 일치(가림 없음 및 전경 실루엣 단차 차단)를 모두 통과한 카메라만 후보로 남긴다.
   - 가중치:
     $$W_i = w_{\text{depth}, i} \times w_{\text{norm}, i} \times w_{\text{border}, i} \times w_{\text{view}, i} \times w_{\text{dist}, i}$$
     여기서 $w_{\text{view}, i} = \frac{\exp(8.5 (\cos \alpha_i - 1))}{\max(1 - \cos \alpha_i, 0.001)}$ ($\cos \alpha_i = \vec{v}_{\text{eye}} \cdot \vec{v}_{\text{cam}, i}$)로, 실제 카메라 시점 근처에서는 해당 카메라 가중치가 99.9% 이상 지배적(epipolar consistency)이 되어 원본 CCTV 사진 수준의 선명도를 내고, 카메라 사이 자유 시점에서는 상위 2~3대 카메라를 $(K+1)$번째 가중치 문턱값 차감($C^0$ 연속 블렌딩)으로 부드럽게 섞는다.
   - **폴백(지어내지 않음, 규칙 2·7)**: 어느 카메라에서도 가림·법선 조건을 통과하지 못하는 영역과 추정 바닥 평면(`estimated_floor_plane`)은 원래 구운 텍스처(또는 원래 정점색)를 그대로 유지한다.
4. **카메라 간 노출·색 보정**:
   - 2대 이상 카메라에 동시에 가림 없이 관측되는 메시 삼각형(약 5.5만 개) 중심의 평균색이 일치하도록 카메라별 RGB 이득(`color_gain_srgb`)을 계산해 투영 시 곱한다.

---

## 4. 눈으로 본 판단 및 한계

- **개선된 점**:
  - `cam_A`~`cam_D` 근처 시점에서 보면 기존 12차 texrecon 아틀라스에서 뭉개지거나 흐려졌던 벽면 액자, 창틀·블라인드, `SSAFY`·`SELF CAFE`·`LOCKER` 글자, 바닥 콘센트, 라바콘, 책상 위 홍보물·노트북·의자 윤곽이 원본 CCTV 사진에 가깝게 선명해진다.
  - 카메라 사이 비스듬한 시점(`obl1`~`obl3`)과 위에서 본 시점(`top`)에서도 벽면과 바닥 카펫 결이 훨씬 또렷하며, 가중치 문턱값 차감과 노출 맞춤 덕분에 카메라 전환 경계의 급격한 색 단차가 줄어든다.
- **한계 (형상 정밀도 의존성)**:
  - **형상 오차가 큰 곳에서의 투영 미끄러짐(고스팅)**: 원형 테이블 상판 가장자리, 얇은 의자 다리·등받이, 창가 책상처럼 TSDF 메시 형상 자체가 실제 물체보다 부풀거나 깎인 곳(수 cm~십수 cm 오차)에서는 해당 카메라 시점에서는 맞더라도 다른 각도에서 볼 때 전경 색이 살짝 어긋나거나 두 카메라의 투영이 겹쳐 보이는 한계가 있다. 즉 시점 의존 텍스처는 **메시 형상 정밀도 개선과 반드시 함께 봐야 한다**.
  - **깊이 잔물결 및 사선 시선**: 카메라에서 7~10 m 떨어진 바닥·모서리는 시선이 15° 이하의 사선(grazing angle)이라 TSDF 의 2~4 cm 잔물결에도 법선·깊이가 흔들리기 쉽다. 바닥 법선 안정화(`+Y` 혼합)와 5탭 깊이 허용폭으로 대폭 완화했으나, 근본적으로는 바닥·벽 평면 정합(`--plane-cleanup` 등)이 선행될수록 품질이 올라간다.

---

## 5. 운영 뷰어(`front/apps/admin/.../viewer3d`)에 붙일 때 할 일

1. **카메라 파라미터 공급 (`-522` API 재사용)**:
   - 운영 서버·뷰어의 카메라 위치·자세·내부 파라미터 API(`S15P31A101-522`)가 내려주는 현재 활성 보정(`calibration_version`)의 `K`, `R`, `t`, `distortion` 을 그대로 받아 뷰어 어댑터(`worldCoordinates.ts`)의 `WORLD_TO_THREE_MATRIX` 규약으로 변환해 셰이더 유니폼에 주입한다.
2. **투영 소스 사진 (세팅 배경 프레임)**:
   - 세팅 완료 시 만들어진 사람 없는 배경 번들(`clean_backgrounds`: `cam_{A..D}.jpg` 왜곡 폄본)을 Scene 자산과 함께 서명 URL(또는 정적 자산)로 내려받아 4장의 텍스처로 바인딩한다.
3. **SceneBackground 재질 확장**:
   - `SceneBackground.tsx` 의 `prepareSceneRoot` 에서 GLB 로드 직후 1회 4대 카메라 깊이 그림자 `WebGLRenderTarget`(`1536x864`)을 굽고, `attachPhotoShader` 에 시점 의존 투영 분기(VDT 켜기/끄기 토글 포함)를 통합한다. 그림자 맵은 정적 배경이므로 최초 1회만 렌더되어 실시간 관제(30 FPS 이상)에 프레임 부하를 거의 주지 않는다(측정 평균 프레임 렌더 0.27 ms).
