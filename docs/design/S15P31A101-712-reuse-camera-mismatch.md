# S15P31A101-712 재사용 판정에 「카메라 시점이 이전 맵과 맞는가」를 더한다

관련 명세: 4.3(현장 세팅 자동 보정·1단계 카메라 헬퍼), 4.4(틀어짐 감시·재세팅 요구), 4.5(보정·Scene 계보), 12.2·12.6(기기·세팅 REST), 13.4(세팅 화면).
계약 `messages/coverage_guide` 1.1.0(묶음 v7.55.0). DB 변경 없음.
앞 문서: `S15P31A101-689-reuse-always-selectable.md`(재사용 판정·reuse-check, 버린 대안 "세팅 직전 프레임 비교"를 이 티켓이 한다), `-691`(1단계 화면·카메라 배지),
`-686`(새 배경), `-687`(카메라 확인), `camera-fall-drift-detection.md`(-540 배경 회전 감시), `world-reanchor.md`(-584 World 다시 맞추기), `coverage-guide-relay.md`(-447 헬퍼 중계).

## 문제(16차 GPU 세션)

REUSE 세팅이 가장 최근 완료 보정(15차, `cal-lobby-init-bc13ef6f-…efddde78`)에서 출발해 카메라 배치가 다른 날(14차) 영상으로 다듬다가 Stage A
`VALIDATION_REJECTED worsened_pairs=['cam_B-cam_C']` 로 끝났다. -689 재사용 판정의 카메라 근거는 둘뿐이었다.

- TIER0_DRIFT(-540): Tier 0 배경 회전 감시의 기준 배경은 **그 Tier 0 프로세스가 처음 만든 배경**(`BackgroundCleanBackground._reference`, 메모리)이다.
  Pod 를 새로 켜면 지금 화면이 기준이 되므로 "다른 날 옮긴 카메라"는 원리상 못 잡는다. GPU 가 꺼져 있으면 근거 자체가 없다.
- LAST_SETUP_REJECTED: 이미 한 번 실패한 뒤에만 생긴다.

그래서 사람은 배경 촬영·초기 복원·Stage A(수 분)를 기다린 뒤에야 실패를 봤다. 현장에서 카메라를 옮긴 날도 같다.

## 고른 방법: 1단계 헬퍼가 이전 맵 사진과 지금 키프레임을 같은 카메라끼리 비교

| 후보 | 언제 보나 | 비용 | 믿을 만한가 | 판단 |
|---|---|---|---|---|
| **(선택) 게이트웨이 각도 헬퍼(-446)가 현재 관제 맵을 구운 카메라별 배경 사진 ↔ 지금 키프레임을 SIFT + 호모그래피로 비교** | 1단계(배경 전), GPU 불필요 | 카메라당 SIFT 1회(분석 960 px) + 매칭, 라운드당 수백 ms(노트북 CPU). 사진은 맵이 바뀔 때만 4장(각 1 MB 안팎) 받음 | -540 과 같은 방법·한도(매칭 40, 인라이어 0.6, 특이값 비 0.98)를 같은 카메라끼리 쓰고, 기준이 **이전 맵을 만든 날의 사진**이라 다른 날의 이동을 잡는다 | 선택 |
| -540 기준 배경을 보정 때 사진으로 바꿔 Pod 시작 때 불러오기 | Tier 0 가 켜진 뒤, 60 s 주기 × 2회 확인 | Tier 0 변경, GPU 필요 | DRIFTED 확정이 융합 제외(관제 판정)까지 바꾼다. 세팅 판단 하나를 위해 관제 경로를 건드린다 | 버림 |
| 워커가 배경 촬영 직후·Stage A 전에 비교 | 2단계, 배경(~20 s) 뒤 | 작업 입력에 기준 사진 주소 추가, 워커 변경 | 믿을 만하나 사람이 배경 촬영까지 한 뒤 실패를 보고 새 맵으로 다시 시작해야 한다 | 1단계에서 볼 수 있으므로 하지 않음(넘긴 일) |
| 이전 보정으로 투영한 고정 구조(점구름·벽선)와 지금 영상 정합 | 1단계 | 맵·점구름 내려받기, 투영·정합 구현 | 정적 GLB 품질·가림에 좌우된다. 같은 카메라 사진끼리가 훨씬 단순하다 | 버림 |
| LightGlue(헬퍼에 이미 있는 ALIKED) 로 비교 | 1단계 | 노트북 CPU 로 한 번 16~24 s(-446 실측), 라운드가 두 배 가까이 길어짐 | 같은 카메라·거의 같은 시점은 쉬운 매칭이라 SIFT 로 충분하다 | 버림 |

기준 사진은 뷰어 VDT(-590·-593)가 이미 쓰는 그 Scene 의 굽기 입력 사진(`SceneCameraImageService`, Tier 0 사람 지운 배경 원본 JPEG, 카메라 해상도·렌즈 왜곡
그대로)이다. 새 저장·새 측정 경로가 없다. 렌즈는 그 Scene 보정의 원문 `calibration_document` 의 `cameras[]`(K·왜곡·image_size)다(외부 자세는 주지 않는다). 처음에는
`calibration_camera` 표에서 읽었으나 이 표를 채우는 경로가 없어 운영에서 기준이 늘 비었다(S15P31A101-771, `S15P31A101-771-reuse-lens-from-calibration-doc.md`).

### 흐름

```
관리자 1단계 화면 ── GET /api/cameras/coverage-guides(수요 켬) ──▶ EC2
헬퍼 ── GET·POST /api/devices/coverage-guides ──▶ EC2 응답 {active_demand, reuse_reference{scene_version, calibration_version,
                                                     expires_at_ms, cameras[{camera_id, url, image_size, K, distortion}]}}
헬퍼: 각도 안내(기존) + 카메라별 시점 비교 → coverage_guide 1.1.0 reuse_view_check ──▶ EC2 (각도 안내와 따로 최신 1건, 60 s)
화면 ── GET /api/setup-sessions/reuse-check(10 s 마다) ──▶ EC2 SetupReuseCheck: CHANGED 카메라 → CAMERA_CHANGED, camera_source VIEW_MATCH
POST /api/setup-sessions(mode=REUSE) 도 같은 판정 → 409 SETUP_REUSE_CAMERA_CHANGED
```

- `reuse_reference` 는 수요가 있을 때만 싣는다(헬퍼는 그때만 계산한다). 현재 관제 맵·사진·렌즈 중 하나라도 없으면 null(렌즈 없는 카메라는 뺀다).
- 판정 순서(`SetupReuseCheck`): TIER0_DRIFT → **VIEW_MATCH** → LAST_SETUP_REJECTED → (Tier 0 연결 중 re_setup 빈 배열 **또는 시점 확인 4대 모두 SAME**) NOT_DETECTED → UNKNOWN.
  시점 확인은 **그 맵 기준**(scene_version 일치)이고 받은 지 60 s 안인 결과만 쓴다. CHANGED 가 없고 일부가 UNKNOWN 이면 막지 않는다(모르면 막지 않는 -689 원칙).
- 조용히 NEW 로 바꾸지 않는다: 재사용 칸을 끄고 이유를 보인다. 선택은 늘 보인다(새 맵 만들기는 늘 켜짐).

### 언제 도는가와 근거

세팅 1단계(맵 방식 고르기) 화면이 열려 있는 동안이다. 1단계는 이미 헬퍼 수요를 켜고(각도 안내) 재사용 판정을 10 s 마다 다시 받으므로 새 폴링이 없다.
헬퍼 한 라운드(노트북 GPU 약 2 s, CPU 16~24 s)마다 비교하고, CHANGED 는 서로 다른 키프레임 2회 연속이어야 하므로 화면에는 보통 두 라운드 + 재사용
판정 주기(10 s) 안에 이유가 뜬다. 그 전에는 재사용을 고를 수 있으나(근거 없음 = UNKNOWN), 시작(POST)도 같은 판정이라 그 사이 확정되면 409 로 막힌다.
배경 직후 워커 비교는 1단계에서 볼 수 있으므로 하지 않았다(헬퍼가 꺼진 현장은 기존대로 워커 World 다시 맞추기·Stage A 가 마지막 확인).

## 판정 방법과 임계값(`gateway/camera_helper/src/simai_camera_helper/view_check.py`)

1. 기준 사진과 지금 키프레임을 **같은 분석 크기**(보정 image_size 를 긴 변 960 px 로)로 줄여 SIFT(최대 1600점), 점은 분석 해상도 K 로 왜곡을 지운다.
   가로세로 비가 보정과 1% 넘게 다르면 STREAM_MISMATCH(다른 스트림, 모름). 해상도만 다르면(서브 스트림) 같은 분석 크기로 맞춘다.
2. 상호 최근접 + Lowe 0.75(-540 `match_features` 와 같음).
3. 판정(CHANGED 는 1회 후보, 아래 4의 확인을 거친다):

| 조건 | 결과 | 값·근거 |
|---|---|---|
| 기준 사진 특징점 < 80 | UNKNOWN·NO_TEXTURE | -540 `extract_features` 의 보강 기준 max(80, 2 × 40). 무늬 없는 벽을 보는 카메라는 이 방법으로 판단할 수 없다 |
| 대응 < 40 이고, 8개 이상이 제자리(중앙값 ≤ 4.5 px) | UNKNOWN·OCCLUDED | -540 `fit_rotation` 의 가림 판정과 같다(사람·조명으로 수만 줄고 맞는 점은 그대로) |
| 대응 < 40 | CHANGED·VIEW_DIFFERENT | -540 `min_matches` 40. 30° 넘게 돌거나 다른 곳을 보면 같은 배경 특징점이 40 아래로 떨어진다 |
| 대응의 50% 이상이 제자리(≤ 3 px) | SAME(회전 0) | 카메라가 그대로면 거의 항등이다. 가구·사람이 바뀐 일부 점은 무시 |
| 호모그래피 RANSAC(3 px) 인라이어 < 0.6 | 제자리 점 ≥ 12 이고 ≥ 35% 면 UNKNOWN·OCCLUDED, 아니면 CHANGED·MOVED | -540 `min_inlier_ratio` 0.6. 한 호모그래피로 설명되지 않는 시차 = 위치 이동 |
| K⁻¹HK 특이값 비 < 0.98 또는 회전 재투영 p95 > 3 px | CHANGED·MOVED | -540 `min_singular_ratio` 0.98. 평면(바닥·벽) 위주 장면에서 옮긴 카메라는 호모그래피는 맞아도 회전으로 설명되지 않는다 |
| 순수 회전각 > 10° | CHANGED·ROTATED | -584 `ReanchorConfig.max_orientation_change_rad` 10°. 그 이상이면 워커 World 다시 맞추기가 어차피 거부한다 |
| 그 밖 | SAME(회전각 기록) | 10° 이하 순수 회전은 재사용 보정이 다시 맞춘다 |

- **잔차 3 px 의 단위**: -540 은 원본 해상도 px 로 재지만(누적 배경 둘을 비교), 이 비교는 키프레임 한 장(압축·잡음)이라 **분석 해상도** px 로 둔다
  (1296p 원본의 약 7 px). 15 cm 이동(-584 배치 잔차 한도)의 시차는 깊이 3~8 m 방, 초점 거리 ≈ 0.42 × 폭(로비 광각)에서
  `f·b·(1/3 − 1/8)` ≈ 403 × 0.15 × 0.21 ≈ 12.6 px(분석 해상도)로 한도의 4배라 여전히 걸린다. 5 cm 이하 이동은 SAME 으로 지나갈 수 있다(그 정도는 -584 가 받아 준다).
4. **2회 연속 확인**(`ViewConfirm`): 그 카메라의 서로 다른 키프레임에서 연속 두 번 CHANGED 일 때만 CHANGED, 첫 회는 UNKNOWN·CONFIRMING. 같은 키프레임은 다시 세지 않고,
   영상 없음·오래됨·가림 등 근거가 끊기면 처음부터 센다. -540 `CandidateConfirmation`(2회)과 같은 안전 기준으로 순간 가림·블러 한 장의 오탐을 막는다.
5. 기준 사진은 맵(scene·보정)이 바뀔 때만 다시 받는다. 못 받으면 NO_REFERENCE 로 두고 30 s 뒤 다시 받는다. 비교가 예외로 실패해도 각도 안내는 그대로 보낸다.

## 정한 수치

| 값 | 위치 | 근거 |
|---|---|---|
| 시점 확인 근거 유효 60 s | `simai.coverage-guide.view-check-ttl`(`SIMAI_COVERAGE_GUIDE_VIEW_CHECK_TTL`) | 카메라 위치는 초 단위로 바뀌지 않는다. 노트북 CPU 헬퍼 한 라운드 16~24 s 를 두 번 넘게 기다려도 근거가 끊기지 않아야 화면 선택이 깜박이지 않는다. 각도 안내 STALE(10 s)과 따로 둔다 |
| 기준 사진 다시 받기 30 s | `REFERENCE_RETRY_S` | 서명 주소 만료·망 오류 때 2초 수요 주기마다 받지 않게 |
| 기준 사진 상한 16 MiB | `MAX_REFERENCE_BYTES` | 굽기 입력 사진 상한 8 MiB(`BAKE_IMAGE_MAX_BYTES`)의 두 배 |
| 분석 960 px·특징점 1600·대응 40·인라이어 0.6·특이값 비 0.98·잔차 3 px | `ViewCheckConfig` | -540 `RotationConfig` 와 같은 값(잔차 단위만 위 3 참고) |
| 회전 한도 10° | `ViewCheckConfig.max_rotation_rad` | -584 방향 변화 한도 |
| 제자리 비율 0.5 | `ViewCheckConfig.stationary_ratio` | 절반 넘는 배경 점이 그대로면 카메라는 그대로다(배치가 바뀐 물체·사람은 소수) |

## 화면(-689·-691 방식)

- 1단계 「맵」 카드의 재사용 칸: `camera_source: VIEW_MATCH` 면 "카메라가 바뀐 것 같아요(카메라 2·카메라 3)" / "이전 맵을 만들 때와 화면이 달라요 · 새 맵 만들기를 골라 주세요"
  두 줄, 재사용 칸은 꺼지고 선택은 새 맵으로 보인다(`reuseChoiceOf`). 다른 근거(TIER0_DRIFT·LAST_SETUP_REJECTED)는 기존 문구 그대로.
- 카메라 배지 「직전 보정과 달라짐」·「지금 할 일」은 이미 `changed_cameras` 를 쓰므로 그대로 따라온다(코드 변경 없음).

## 계약·REST

- `coverage_guide` 1.1.0: 선택 `reuse_view_check`, 실으면 schema_version 1.1.0(if/then), 상태별 사유 규칙. 1.0.0 은 그대로 받는다(서버는 `1.` 접두만 본다).
  EC2 는 시점 확인 카메라 집합이 각도 안내 카메라 4대와 같은지 의미 검사를 더했다.
- 계약 밖 REST: 기기 수요 응답 `reuse_reference`(기존 `active_demand` 와 같은 응답, 계약 밖), reuse-check `camera_source: VIEW_MATCH`(-689 와 같이 계약 밖).
- 배포 순서: EC2·화면 먼저, 헬퍼 나중. 옛 EC2 는 1.1.0 메시지를 계약 검사로 400 거부하지만 옛 EC2 는 `reuse_reference` 를 주지 않으므로 새 헬퍼도 1.0.0 만 보낸다.
  옛 헬퍼 + 새 EC2 는 `reuse_reference` 를 무시하고 1.0.0 을 보내 지금처럼 UNKNOWN 이다.

## 버린 대안(위 표 밖)

- **시점 확인 SAME 이면 LAST_SETUP_REJECTED 를 풀기**: 카메라를 되돌린 경우 유용하지만 거부 사유(축척 등)와 시점 확인의 범위가 달라 이 티켓에서 바꾸지 않았다.
- **카메라가 바뀌면 자동으로 새 맵 선택**: 사용자 결정(재사용 기본, 선택은 늘 보이고 이유를 보인다)으로 금지. 꺼진 칸과 이유만 보인다.
- **시점 확인 결과를 DB 에 쌓기**: 각도 안내처럼 메모리 최신 1건이면 충분하다(세팅 판단 근거, 이력 아님).

## 잔여 위험

- **녹화 재생 시험**: 헬퍼는 노트북의 실제 카메라 영상(relay 미리보기)을 본다. 16차처럼 Tier 0 에 다른 날 녹화를 재생하면 헬퍼는 오늘 카메라를 보고 판단하므로
  그 경우는 못 잡는다(현장 실사용에서는 같은 영상이다).
- **기준 사진이 이전 NEW 세팅 때 것**: 재사용 세팅은 맵을 굽지 않아 사진이 바뀌지 않는다. 재사용을 거듭하며 한도 안의 작은 회전이 쌓이면(예: 6° + 6°) 사진 대비 10° 를 넘어
  재사용이 이전 활성 보정 기준으로는 될 수 있는데도 막힐 수 있다. 새 맵을 만들면 풀린다.
- **조명·혼잡**: 밤/낮 조명 차이나 많은 사람으로 대응이 40 아래가 되고 남은 점도 흔들리면 VIEW_DIFFERENT 오탐이 날 수 있다(2회 연속 확인으로 줄였지만 없앨 수는 없다).
  「새 맵 만들기」는 늘 고를 수 있고, 판정은 10 s 마다 다시 받는다. 무늬 없는 벽만 보는 카메라는 NO_TEXTURE(모름).
- **5 cm 이하 이동·순수 회전 10° 이하**는 SAME 이다(재사용 보정이 받아 준다는 가정, -584 한도 기준).
- 헬퍼가 꺼져 있거나 가중치가 없어 라운드가 돌지 않으면 시점 확인도 없다(UNKNOWN, 기존과 같다). 헬퍼 라운드는 4대 모두 키프레임이 있어야 돈다.
- 카메라 ID 를 통째로 바꿔 꽂으면(배선 순환) 같은 카메라 비교가 아니게 되어 VIEW_DIFFERENT 로 막힌다(오히려 안전한 쪽).
- REUSE 에서 Stage A 가 `VALIDATION_REJECTED` 로 끝나도 LAST_SETUP_REJECTED 로 기록되지 않는다(-689 는 `REUSE_WORLD_REJECTED` 만 본다). 시점 확인이 대신 잡지만
  헬퍼가 없으면 같은 실패가 반복될 수 있다.

## 넘긴 일

- 17차 GPU 세션·현장: 카메라 한 대를 돌리거나 삼각대를 옮긴 뒤 1단계에서 「카메라가 바뀐 것 같아요(카메라 n)」가 두 라운드 안에 뜨는지, 그대로 둔 날 4대
  SAME(NOT_DETECTED)인지, 사람이 몇 명 걸어 다닐 때 오탐이 없는지, 헬퍼 로그 `reuse view` 줄과 라운드 시간 증가.
- 배경 직후·Stage A 전 워커 비교(헬퍼가 없는 현장의 예비)와 REUSE Stage A 거부를 LAST_SETUP_REJECTED 로 보는 일은 후속 후보.
- 명세 정리 MR: 4.3·13.4(1단계 재사용 시점 확인), 12.6(reuse-check `VIEW_MATCH`, 기기 수요 응답 `reuse_reference`).

## 코드·시험 위치

- 계약: `contracts/messages/coverage_guide.schema.json`(1.1.0), 예시 `contracts/examples/coverage_guide.reuse_view_check.json`, 반례
  `contracts/examples/invalid/coverage_guide.{schema_1_0_0_with_view_check,view_check_changed_confirming,view_check_same_with_reason,view_check_keypoints}.json`,
  `ai/src/simai_ai/contracts/models.py`(생성).
- 헬퍼: `gateway/camera_helper/src/simai_camera_helper/view_check.py`(신규), `message.py`(1.1.0), `service.py`(`Client.note_response`·`reuse_reference`,
  `reuse_view_check`, 라운드 연결). 시험 `gateway/camera_helper/tests/test_view_check.py`(합성 장면: 잡음 SAME, 4° SAME, 14° 2회 확인 ROTATED, 시차 MOVED,
  다른 장면 VIEW_DIFFERENT, 근거 끊김 재확인, 기준 실패·30 s 재시도, 비율 불일치, 서브 스트림·왜곡 SAME, 계약 검증, 클라이언트 기준 보관).
- 서버: `CoverageReuseReference`(신규), `CoverageGuideService`(`reuse_reference`·`reuseViewCheck`·시점 확인 보관·의미 검사), `CoverageGuideProperties.viewCheckTtl`,
  `SetupReuseCheck`(VIEW_MATCH), `application.yaml`. 시험 `CoverageGuideIntegrationTest`(보관·60 s·맵 불일치·카메라 검사·4대 SAME),
  `CoverageReuseReferenceIntegrationTest`(신규), `SetupReuseModeIntegrationTest`(VIEW_MATCH 차단·시작 409·Tier 0 우선·4대 SAME NOT_DETECTED·일부 모름).
- 화면: `front/apps/admin/src/features/setup/useReuseScene.ts`(`reuseChoiceOf` VIEW_MATCH 문구), `types.ts`(설명). 시험 `reuseChoice.test.ts`.
