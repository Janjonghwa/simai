# S15P31A101-590 카메라 시점 보기와 카메라 시점 VDT

관련 명세: 13장(관제 3D 뷰어), 3.2(Visual 층은 위치·판정에 들어가지 않음), 4장(좌표 규약). VDT 자체는 `docs/design/viewer-view-dependent-texture.md`(-534), `docs/design/viewer-map-cameras.md`(-541).

## 한 일

- 관제 화면에서 카메라 마커를 누르면 CCTV 창을 여는 것에 더해 3D 시점을 그 카메라 자리·방향·세로 시야각으로 옮긴다(`SpatialViewportHost` → `Viewer3DHandle.viewFromCamera`).
- 시점이 그 카메라에 맞아 있는 동안만 VDT(시점별 사진 투영)를 켜고, 시점이 바뀌면 서서히 끈다. 관제 화면은 여전히 `vdt` prop 을 넘기지 않는다(늘 켜기 아님).
- CCTV 창 닫기(창의 「닫기」. 도구 모음의 「CCTV 창」 버튼은 -604 에서 없앴고 창은 카메라 마커로만 열린다)와 CCTV 창 머리줄의 「전체 보기」(카메라 시점일 때만 보임)는 클릭 전 시점으로 돌아간다(`exitCameraView`). 「시점 초기화」는 카메라 시점 보기를 끝낸다.
- CCTV 창 기본 자리는 왼쪽 위(top 128 px · left 96 px, 480×360)라 1920×1080 화면 가운데(장면 핵심)를 가리지 않는다. 그대로 둔다(사용자가 옮긴 자리는 지금처럼 기억).

## 코드·시험 위치

| 내용 | 위치 |
|---|---|
| 시점 계산·이음·맞음 판정·FPS 감시(순수) | `front/apps/admin/src/features/viewer3d/cameras/cameraView.ts` |
| 시점 이동·맞음·바깥 변화 감지 | `viewer3d/CameraRig.tsx` |
| 요청·상태(`cameraViewRequest`·`cameraViewExits`·`cameraViewId`·`alignedCamera`·`cameraVdtBlocked`) | `viewer3d/viewer3d.store.ts` |
| 카메라 시점 VDT 장착·페이드·FPS 끄기 | `viewer3d/scene/SceneBackground.tsx`, 셰이더 `uVdtStrength`(`scene/vdtShader.ts`) |
| 사진 주소 자리 | `ViewerVdtOptions.cameraImageUrls`(`scene/vdtTypes.ts`), `withVdtCameraImages`(`scene/vdtMath.ts`) |
| 마커 숨김 | `viewer3d/cameras/CameraLayer.tsx` |
| 관제 연결·전체 보기 | `features/layout/SpatialViewportHost.tsx`, `features/cctv/CctvFloat.tsx` |
| 시험 | `__tests__/features/viewer3d/cameraView.test.ts`, `cameraViewVdt.test.tsx`, `cameraRig.test.tsx`(카메라 시점 보기 묶음), `__tests__/features/layout/SpatialViewportHostCameraMarkers.test.tsx` |

좌표: 카메라 자리·회전 중심은 World 에서 계산하고 `worldToThree` 로만 옮긴다(새 축 변환 없음, 규칙 3).

## 명세에 없어 정한 수치

| 수치 | 값 | 근거 |
|---|---|---|
| 시점 이동 시간 | 1000 ms, ease-in-out cubic | 티켓 지시(약 1 초). 움직임 줄이기 설정이면 0 ms |
| VDT 켜고 끄는 시간 | 400 ms | 티켓 범위(0.3~0.5 s)의 가운데. 셰이더 `mix(원래 구운 색, VDT 색, 비율)` 로 섞는다 |
| 세로 시야각 | `2·atan(tan(hfov/2) / (16/9))` | 보정 문서는 수평 시야각만 준다. 현장 카메라 영상이 16:9(2304×1296)이고 마커 절두체도 같은 비율(`CAMERA_FRUSTUM_ASPECT`) |
| 회전 중심 | 정면 광선이 바닥(Z=0)에 닿는 점, 1~15 m 로 자름. 바닥을 향하지 않으면 4 m 앞 | 카메라에서 돌리기 시작할 때 보던 바닥 지점을 중심으로 돈다. 교실 대각선(약 12 m)보다 조금 넓게 자름 |
| "맞아 있음" 허용 | 자리 1 cm, 방향 0.25°, 세로 시야각 0.1°. 회전 중심 거리는 보지 않음 | OrbitControls 의 구면 좌표 왕복 오차(1e-9 수준)는 넉넉히 넘고, 사람이 돌리기 시작한 첫 프레임은 잡는다 |
| FPS 기준 | 켠 뒤 1 s 준비, 2 s 창 평균 24 FPS 미만이면 그 카메라 시점에서 끔. 500 ms 넘는 한 프레임은 탭 멈춤으로 보고 창을 비움 | 준비 시간은 사진 올리기·그림자 맵 굽기 끊김을 뺀다. 24 FPS 는 조작이 끊겨 보이기 시작하는 흔한 기준. 다시 같은 카메라를 누르면 다시 시도 |

## 동작 규칙

- 바깥 변화 감지: 시점 틀은 OrbitControls update(우선순위 -1) 뒤 프레임에서 지금 시점을 마지막으로 놓은 시점과 비교한다. 다르면 사용자 조작·초점·위에서 보기 등으로 보고 이동을 멈추거나 맞음을 푼다. 클릭만 하고 돌리지 않으면 맞음이 풀리지 않는다(OrbitControls `start` 이벤트로 판단하지 않은 이유).
- 이동을 시작할 때 OrbitControls 의 남은 관성(damping)을 한 번에 풀어 이동과 다투지 않게 한다.
- 클릭 전 시점은 첫 요청 직전 것 하나다. 다른 카메라로 옮겨 다녀도 나가기는 그 시점으로 간다. 사용자가 돌려 맞음이 풀려도 기억하고, 시점 초기화만 잊게 한다.
- 시점 초기화·위에서 보기·스캔 시점은 시야각도 처음 값(50°)으로 돌린다(카메라 시점이 시야각을 바꾸므로).
- VDT 장착은 맞은 뒤에 한다(사진 받기·그림자 맵 4장 굽기). 끌 때는 비율을 0 으로 내린 뒤 원래 재질로 되돌린다. 다시 맞추면 다시 장착한다.
- `vdt.enabled`(개발 화면 늘 켜기)가 켜져 있으면 비율은 늘 1 이고 카메라 시점과 상관없다.

## 사람 지운 배경 사진 경로

- 찾은 것: Tier 0 가 카메라별 사람 지운 배경을 Pod `state_dir/clean-background/<calibration_version>/bundle-<uuid>/` 에 쓰고(-358·-363), 분석 워커가 LIVE 가 아닐 때 `static-inputs/<bundle_id>/<카메라 ID>.jpg` 로 S3 에 올린 뒤 `POST /api/worker/scene-jobs/static-reconstruct` 로 맵 재생성 입력에만 쓴다(`StaticReconstructSubmitService`).
- 못 찾은 것: 이 사진을 화면에 내려 주는 서버 API·서명 주소는 없다. `GET /api/scenes/current`(`SceneService`)는 `gltf_url`·`surface_source_map_urls`·`map_cameras` 만 주고 `vdt` 블록(카메라 사진 주소)은 주지 않는다. 프론트에도 그 주소를 받는 곳이 없었다.
- 그래서 새 서버 API 를 만들지 않고 뷰어에 자리(`vdt.cameraImageUrls`)만 두었다. 관제 화면은 아직 넘길 주소가 없어 넘기지 않으며, 장면에 `vdt` 도 없으므로 지금 운영에서는 카메라 시점 이동만 되고 VDT 는 켜지지 않는다. 주소를 내려 주는 API 가 생기면 그 훅 결과를 `SpatialViewportHost` 의 Viewer3D 에 `vdt={{ cameraImageUrls }}` 로 넘기면 된다(`map_cameras` 가 있는 Scene 이면 표시 층 없이 동작).

## 버린 대안

- OrbitControls `start` 이벤트로 맞음 풀기: 클릭(사람 고르기)만으로도 시작돼 VDT 가 꺼진다. 시점 비교가 초점·위에서 보기 같은 다른 명령도 함께 잡는다.
- VDT 를 늘 장착해 두고 비율만 0/1: 무거운 셰이더가 늘 돌아 노트북 프레임을 깎는다. 맞을 때만 장착한다.
- 카메라 자리에서 조금 뒤로 물러서 마커를 피하기: 사진과 시점이 어긋난다. 그 카메라 마커만 숨긴다.
- 정밀 자세를 `map_cameras`(K·R·t)에서 가져오기: 관제 마커·CCTV 와 같은 정본 보정 자세(`cameraPoses`)를 쓴다. 차이가 나도 VDT 는 어느 시점에서나 사진을 투영하므로 동작한다.
- 새 서버 API: 지시에 따라 만들지 않았다.

## 남은 위험

- 운영 데이터에 VDT 사진이 없어 실제 장면에서 페이드·FPS 끄기를 눈으로 보지 못했다(시험은 가짜 렌더러). 사진 API 가 생긴 뒤 GPU 노트북에서 확인 필요.
- 카메라 롤(기울기)은 보정 자세에 없어 시점은 수평으로 맞춘다. 사진과 화면 테두리가 약간 기울어 보일 수 있다.
- 바닥·벽 표시 층·배경 톤 판은 비율과 상관없이 장착 동안 보이고 끝날 때 한 번에 사라진다(카메라 자리에서는 구멍이 거의 안 보여 작다고 봄).
- VDT 를 다시 켤 때마다 사진을 다시 읽고 그림자 맵을 다시 굽는다(브라우저 캐시에 기댐).
- 구역 편집에 들어가면 CCTV 창이 숨어 「전체 보기」를 누를 수 없다(사용자가 직접 돌리면 VDT 는 꺼진다).

## 넘긴 일

- 사람 지운 배경 사진을 화면에 내려 주는 서버 API(서명 주소)와 그 훅. 계약 변경 여부는 그 티켓에서 정한다.
