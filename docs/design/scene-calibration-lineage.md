# Scene–보정 계보 정합 (S15P31A101-381)

## 근거와 결정

명세 4.1·4.3·4.5, 13.1~13.2(14차 개정)를 따른다. World는 실행 중 보정에서도 기준 카메라로 계승하며 Scene은 시각화에만 쓴다. 판정·위치·구역·알림에는 이 결과를 넣지 않는다.

Tier 0의 Scene 참조는 **health와 같은 주기적 제어 경로**를 선택한다. EC2는 기존 인증된 I1 연결에 작은 `scene_reference`를 1초마다 보내고 Tier 0는 기동 때 캐시한 현장 ID를 확인한 뒤 판정 lock 안에서 다음 프레임의 참조만 바꾼다(수신 경로에서 디스크/네트워크 조회 없음). 재연결에도 다시 보낸다. 구성 묶음은 위험 규칙 적용과 Scene 선택의 수명을 불필요하게 묶고, REST 조회는 별도 인증·통신·재시도 경로가 필요해 버린다. 활성 Scene이 없으면 참조는 null이고 Tier 0는 마지막 참조(기동 전에는 설정값)를 유지한다. 설정 기본 Scene이 있으면 EC2의 기본 버전을 보낸다.

World 계보 ID는 `RuntimeCalibrationStore`의 bootstrap 보정 버전이다. 후보는 기존 World 정렬·카메라 집합·내부 파라미터 검증을 통과해야 같은 계보를 물려받는다. 되돌림·재시작도 저장된 bootstrap을 유지한다. 초기/교체/DRIFTED 보정 알림에 `world_frame_id`와 `camera_poses`(World→camera R,t)를 추가하고, 기존 append-only payload 저장을 사용한다. 초기 자세도 최초 INITIAL 알림으로 저장한다. 과거 문서·해시는 수정하지 않는다. 서버는 저장된 알림의 자세를 우선 사용하고, 없으면 업로드된 보정 문서의 자세를 사용한다. 문서의 자기 버전이 상대 알림의 bootstrap ID와 같을 때만 초기 계보를 연결하며, 그 밖의 과거 문서는 계보 미상(DIMMED)이다. 같은 현장이라는 이유만으로 World가 같다고 추정하지 않는다.

## 서버 판정

비교 기준은 **Scene을 생성한 보정**이며 직전 보정이 아니다. 카메라별 중심 `C=-Rᵀt`의 거리(m), 상대 회전 `acos(clamp((trace(Rnew Roldᵀ)-1)/2))`(내부 rad, 표시 deg)의 최댓값을 낸다. 작은 교체가 누적되면 문턱을 넘는다. 카메라 집합·유한수·회전행렬 검증 실패는 근거 없음이다.

- 같은 보정이거나, 같은 계보에서 최대 중심 이동 ≤ **0.10 m**, 최대 회전 ≤ **2°**이면 `VERIFIED`.
- 같은 계보에서 어느 문턱이든 초과하면 `DIMMED`, `regeneration_required=true`.
- 다른 계보이면 `MISMATCH`(숨김). 근거가 없으면 `DIMMED`와 재생성 필요로 보수적으로 표시한다.

0.10 m·2°는 보기용 배경의 초기 운영 문턱이며 G1/G4 합격 기준이 아니다. 명세 4.3의 World 계승, `extrinsics-from-recording.md`의 기준 카메라 정렬 잔차 약 1.44 cm와 1.305° 자유 World 기울어짐 사례, 운영 중 후보 안전장치에 근거해 cm급 미세 조정을 유지하고 명백한 자세 변경을 흐리게 한다. 명세 4.4의 누적 편차 경고(5 cm·1.5°)보다 넓게 두어 경고만으로 배경을 감추지 않으며, 초과해도 숨김 대신 흐림으로 재생성을 안내한다. 8차 253z→266z의 원시 자세 자료는 이 worktree에 없어 수치를 지어내거나 문턱 검증 자료로 쓰지 않았다. 실영상 정합을 보고 문턱을 재측정해야 한다.

`GET /api/scenes/current`와 `viewer_health.scene`은 같은 서버 비교 서비스를 사용한다. 결과에 비교한 `calibration_version`과 Scene 버전을 함께 결속한다. 뷰어는 현재 world 프레임과 결속이 일치하는 서버 판정만 사용하고, 전환 중/근거 없음은 흐리게 한다. 자산 Scene이 바뀌면 재조회한다. Scene 생성 보정의 `calibration_state=PROVISIONAL`은 ‘임시 보정 기준 배경’, 큰 변화/근거 부족은 ‘재생성 필요’로 표시한다.

## 계약 변경

- I1 `calibration_update` 2.1.0: 선택 `world_frame_id`, `camera_poses`만 추가. **Lead 승인 필요**.
- I1 아래로 `scene_reference` 1.0.0 신설: site_id, scene_version(null 가능).
- 공통 `scene_compatibility` 결과 정의 신설. I2 `viewer_health` 1.1.0의 scene에 선택 compatibility 추가. 기존 mismatch는 Scene 참조 문자열 불일치 의미를 보존한다.
- REST Scene 응답에 같은 선택 compatibility 추가. 예시·프론트 생성 타입/검증기를 함께 갱신한다.

## 검증·위험

관련 시험만 실행한다. 카메라 중심 계산, 회전 경계, 누적 변화, 다른/미상 계보, 누락·잘못된 자세, 보정 교체·재시작, Scene 활성화·되돌림·재연결, PROVISIONAL 문구와 재조회 동작을 검증한다. 실제 GPU·S3·브라우저 영상 정합은 별도 현장 검증이 필요하다. 오래된 알림에 자세/계보가 없으면 자동으로 같은 World라고 보지 않는다. DB에 보정 원문이 없고 초기 알림도 아직 수신되지 않았다면 배경은 흐리게 남는다.


## 구현 위치

- `contracts/messages/calibration_update.schema.json`, `scene_reference.schema.json`, `viewer_health.schema.json`, `contracts/common/scene_compatibility.schema.json`: 선택 메타데이터·참조·판정 계약. `world_state` 계약은 변경하지 않는다.
- `ai/src/simai_ai/calibration/runtime.py`: 초기 자세 스냅샷, bootstrap 계보와 버전별 자세. `ai/services/tier0/service.py`: 인증 I1 참조 수신·현장 확인·tick과 직렬화, 초기 알림 조립.
- `server/.../domain/scene/SceneCompatibility.java`: 서버 정합 판정. `SceneReferenceSender.java`: 주기 참조·재연결. `SceneService`·`ViewerHealthService`·`HealthState`: REST/I2의 동일 비교와 현재 보정 결속. DB migration은 없다(기존 append-only 보정 알림 payload 이용).
- `front/.../viewer3d/scene/sceneContract.ts`·`SceneBackground.tsx`: 서버 판정과 프레임 결속·흐림 유지. `useCurrentScene`·`SpatialViewportHost`: 활성 Scene 재조회와 한국어 상태 표시. 자동 재조회는 기존 10초 제한 간격을 따른다.
- 시험: AI `test_calibration_swap.py`·`test_i1_units.py`·`test_live_calibration_safety.py`; 서버 `SceneCompatibilityTest`·`SceneReferenceSenderTest`·`SceneServiceTest`·`CalibrationStatusIntegrationTest`; 프론트 `sceneContract`·`sceneBackground`·`viewerSceneApi`·`useCurrentScene`·`SpatialViewportHostScene`, 생성 검증기 시험. 실제 명령과 결과는 `.agy/notes.md`에 남긴다.
