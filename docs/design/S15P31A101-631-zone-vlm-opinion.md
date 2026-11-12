# 변경 의심 구역 VLM 의견과 전후 카메라 사진 (S15P31A101-631)

`-630` 높이 비교(`docs/design/S15P31A101-630-zone-height-change.md`) 후속이다. 사전 조사는 `docs/design/S15P31A101-631-research.md`.

관련 명세:
- **5.4** VLM 용도·권한 (`docs/spec/05-processing.md`): 용도 4(`vlm_zone_draft`), "설명과 제안만, 하향·해소 불가"
- **8.2** F-06 입체 Geo-fencing (`docs/spec/08-features.md`)
- **12.5·12.6** I5 작업 계약·세팅 세션 REST (`docs/spec/12-interfaces.md`)
- AGENTS 규칙 1·2·4·6·7·8

---

## 1. 무엇을 하나

세팅 구역 확인 때 `-630` 이 `CHANGE_SUSPECTED` 로 판정한 이어 쓴 구역마다, 이전 세팅과 이번 세팅의 **같은 고정 카메라** 사진에서 그 구역 부분을 잘라 같은 Pod 의 VLM(Cosmos3-Nano, 루프백 vLLM)에 두 장을 보이고 "무엇이 바뀌었나 + 유지(KEEP)·수정(MODIFY)·삭제(REMOVE) 권고" 한 줄을 받는다. 세팅 구역 확인 카드의 변경 의심 행 아래에 배지(유지 권고·수정 권고·삭제 권고·AI 의견 없음·AI 의견 준비 중)와 한 줄, 전후 썸네일 두 장(누르면 크게 보기)을 보인다.

- **AI 는 제안만(규칙 4)**: 의견은 표시용이다. 구역 목록·`needs_review`·관제 구역을 서버도 화면도 자동으로 바꾸지 않는다. 확인·수정·삭제는 관리자가 누른다.
- **모르면 비운다(규칙 2)**: 이전 사진·보정을 못 찾거나, 두 사진에 함께 보이는 카메라가 없거나, VLM 실패·시간 초과·형식 오류면 `NO_OPINION` 과 사유만 적는다. 권고·이유를 지어내지 않는다.
- **얼굴(규칙 8)**: VLM 에는 `gate_input_frames` 로 얼굴을 처리한 프레임의 크롭만 넣는다. 사진은 Pod 밖으로 나가지 않는다(이 기능은 외부 LLM 을 쓰지 않는다). 화면 사진은 기존 세팅 대표 프레임(`CAMERA_FRAME`, 사람 지운 배경)의 서명 GET 주소다.

## 2. 흐름

```
site_setup 미리보기 스냅샷(zone_height_changes 포함) ── EC2 SetupService.enqueueZoneDraftIfReady
   └ SetupZoneOpinions.addOpinionInput: World 이어짐 + CHANGE_SUSPECTED 구역 있으면
        suspected_zones(높이 차이 큰 순 8개, 지금 관제 구역 다각형·높이 + -630 수치)
        previous_setup_session_id · previous_calibration_{version,ref,hash} · previous_frames(이전 세션 CAMERA_FRAME)
        → vlm_zone_draft 작업 시간 상한 60 s → 150 s
워커 VlmZoneDraftHandler: 초안(기존 그대로) → 의견(simai_ai.vlm.zone_opinion)
   카메라 고르기(보정만) → 고른 카메라의 이전 프레임만 얼굴 처리 → 전·후 크롭 2장 + 수치 → VLM JSON → zone_opinions
EC2 콜백: setup_zone_opinion(V66)에 추가 → GET·PUT /zones, POST /zones/seed 항목에 ai_opinion(+전후 서명 주소·크롭 박스)
화면: SetupZoneReviewCard → ZoneOpinionView(CroppedPhoto 로 크롭 표시, ModalDialog 크게 보기)
```

### 2.1 "전" 사진을 찾는 법 (Lead 결정 2)
이전 관제 Scene 버전은 세션의 `site_setup` 작업 입력 `previous_scene.scene_version`(없으면 `zone_height_changes.previous_scene_version`)이다. 그 Scene 을 `SCENE`·`PREVIEW_SCENE` 으로 등록한 세팅 세션(보강 다시 굽기로 만든 Scene 이면 `setup_supplement_rebake.result_scene_version` 의 세션)을 찾고, 그 세션의 `calibration_version`(보정 참조·해시)과 `CAMERA_FRAME`(미리보기를 등록한 대여의 것, 없으면 최근 등록분, 카메라당 하나)을 싣는다. 못 찾으면 구역만 싣고 워커가 `NO_PREVIOUS_FRAMES` 로 답한다. 이전 모델 재사용(REUSE) 세션 사진 보존(조사 위험 1)은 이번 범위 밖이라, REUSE 로 끝난 세팅이 이전 Scene 의 주인이면 "전" 사진이 없다(그 Scene 자체는 이전 NEW 세션의 것이라 대개는 찾는다).

### 2.2 카메라 고르기·크롭 (Lead 결정 4, `ai/src/simai_ai/vlm/zone_opinion.py`)
- 구역 3D 상자 = `polygon_xy` × `[z_lo, z_hi]`. `z_lo = z_min_m`(없으면 0), `z_hi = z_max_m`(없으면 `z_lo + 1.0`)을 바닥 위 `[0.3, 2.0]` m 로 자른다.
- 이전·이번 보정 각각에 핀홀 투영(초안과 같은 `project_world_points`, 왜곡 생략). 카메라 앞 꼭짓점이 절반 이상, 투영 상자 중 화면 안 면적 비율(`visible_ratio`) 0.4 이상, 화면 안 상자의 변 16 px 이상일 때만 "보인다".
- 점수 = `visible_ratio × 화면 면적 비율`. 전·후 **같은 카메라**의 점수 작은 쪽이 가장 큰 카메라 하나. 화면이 두 장을 다 보일 수 있게 입력에 이전·이번 프레임이 둘 다 있는 카메라만 본다.
- 크롭 = 화면 안 투영 상자에 양쪽 25% 여백, 최소 변 96 px(1280 px 프레임 기준). 정규화 좌표 `[u1, v1, u2, v2]` 라 해상도와 무관하다. 그래서 카메라 고르기는 보정만으로 하고(VLM 이 없어도 사진 박스는 남는다), VLM 에 넣을 때 실제 프레임 크기로 픽셀 영역을 다시 잡는다.

### 2.3 프롬프트·응답
- 입력: 이전 크롭 → 이번 크롭 순서 2장 + 구역 이름·종류·바닥 크기·높이 범위·`max_height_diff_m`·`changed_area_ratio`·카메라. 사람 정보 없음.
- 응답 `json_schema`: `recommended_action`(KEEP·MODIFY·REMOVE), `rationale`(한국어), `confidence`(0~1 또는 null). 첫 줄만 쓰고 200자를 넘으면 자른다. 형식이 어긋나면 `VLM_FAILED`.
- `prompt_version = vlm-zone-opinion-1.0`.

## 3. 정한 수치와 근거

| 수치 | 값 | 근거 |
|---|---|---|
| 구역 수 상한 | 8 | 한 세팅의 이어 쓴 구역은 보통 수 개. VLM 1회 약 2~5 s(10차 세션 VLM 5.41 s 는 S3 업로드 포함)라 8개 ≈ 40 s 로 초안 시간 예산과 합쳐 150 s 안에 든다. 서버·계약(`maxItems 8`)·워커가 같은 값 |
| 작업 시간 상한 | 의견 있으면 150 s(없으면 기존 60 s) | 초안 60 s + 8 × 약 10 s(이전 프레임 얼굴 처리 + VLM 1회). 넘칠 것 같으면 워커가 남은 구역을 `TIME_BUDGET` 으로 낸다 |
| 호출 전 남은 시간 하한 | 8 s, 호출 상한 = 남은 시간 − 2 s | 기본 VLM 시간 상한(30 s)보다 짧은 남은 시간에서 시작해 작업 상한을 넘기지 않게 |
| 크롭 여백 | 양쪽 25%, 최소 변 96 px | 초안 크롭(`select_candidate_crops` 25%)과 같다. 작은 구역도 주변 맥락(벽·책상)이 보이게 최소 변을 초안(64 px)보다 크게 |
| 상자 높이 | 바닥 위 0.3~2.0 m, 모르면 1.0 m | 바닥 구역도 위에 놓인 물체가 잡히게 최소 0.3 m, VOLUME_3D 상한(2.5 m 등)이 크롭을 천장까지 키우지 않게 최대 2.0 m |
| 보이는 조건 | 카메라 앞 꼭짓점 ≥ 50%, 화면 안 비율 ≥ 0.4, 변 ≥ 16 px | 절반 넘게 잘린 구역은 전후 비교가 어렵다. 16 px 는 초안 크롭 최소와 같다 |
| 목록 다시 받기 | 의견 대기 중 5 s, 그 밖은 서명 주소 만료 30 s 전(최소 15 s) | 정밀 굽기 뒤 대여되는 의견을 기다리는 동안만 짧게, 서명 GET(5분)이 끝나기 전에 새 주소로 |

## 4. 계약·DB·REST

- 계약 묶음 **v7.44.0**: `vlm_zone_draft` **1.1.0**(입력 선택 `suspected_zones`·`previous_*`, 결과 선택 `zone_opinions`), `job_request` **3.12.0**, `job_callback` **2.23.0**. 불변식은 스키마: OPINION 은 사유 null·권고·이유·카메라·크롭 둘·모델 필수, NO_OPINION 은 사유 필수·권고·이유·신뢰도·모델 null, 카메라 null 이면 크롭 둘 다 null. `previous_frames` 는 `suspected_zones`·`previous_calibration_ref` 를, `previous_calibration_ref` 는 `previous_calibration_version` 을 함께 요구한다. 결과의 `status`·`model_version` 은 기존대로 초안 기준이다.
- **배포 순서: 워커 먼저, EC2 나중.** 1.0.0 워커는 `suspected_zones` 입력을 닫힌 객체로 거부한다. 새 워커는 그 입력이 있을 때만 `zone_opinions` 를 내므로 이전 EC2 와도 문제없다.
- DB **V66** `setup_zone_opinion`: 초안 작업(draft_run)마다 새 행(추가 전용, 규칙 6). 전·후 `CAMERA_FRAME` 주소(작업 입력에서 카메라로 찾음)·크롭 박스·이전 세션 ID·모델·프롬프트 판. 상태별 모양은 표 제약으로도 막는다. site 를 직접 참조하지 않고 `setup_session`·`setup_zone_draft_run`·`analysis_job` 을 지우면 함께 지워진다.
- REST: 구역 계획 응답 항목의 `ai_opinion` = `{status, reason, recommended_action, rationale, confidence, camera_id, model_version, prompt_version, before: {image_url, crop_box_norm}, after: {...}, image_expires_at_ms, created_at}`. 세션의 **지금 미리보기**로 낸 초안 작업의 의견만 보인다(다시 시도로 미리보기가 바뀌면 이전 의견은 숨는다). 그 작업이 아직 대기 중이면 입력에 실린 구역에 `{status: PENDING}`. PUT 본문의 `ai_opinion` 은 받지 않는다(항목 레코드가 모르는 필드를 버린다).

## 5. 버린 대안

| 대안 | 버린 까닭 |
|---|---|
| 새 작업 유형(`vlm_zone_opinion`) | 워커는 한 번에 하나만 대여해서 순서상 이득이 없고, 이번 프레임 얼굴 처리를 두 번 하며 계약·서버 경로가 하나 더 생긴다(Lead 결정 1) |
| 워커가 크롭 JPEG 을 올리고 그 주소를 보임 | 객체가 구역마다 늘고 업로드 시간이 든다(10차 세션 병목이 S3 업로드). 원본 프레임이 이미 사람 지운 배경이라 얼굴 노출 차이도 작다. 정규화 박스면 화면과 VLM 이 같은 영역을 본다(Lead 결정 5) |
| 보조 스레드로 정밀 굽기와 동시에 의견 | 단일 워커 구조를 바꾸고 GPU·CPU 경합 측정이 필요하다. 이번은 기존 초안 시점을 따른다(Lead 결정 6) |
| 전·후 각자 가장 잘 보이는 카메라(다른 카메라 허용) | 시점이 다르면 VLM·관리자 모두 "바뀜"을 시점 차이와 구별하기 어렵다. 같은 카메라가 없으면 `NOT_VISIBLE` |
| 왜곡 계수 포함 투영(`overlay.project`, cv2) | 초안 크롭과 같은 핀홀로 맞췄다. 크롭은 여백 25% 로 왜곡 오차를 흡수하고, 서비스 경로에 cv2 의존을 늘리지 않는다 |

## 6. 남은 위험

- **"후" 사진 시점**: 단일 워커라 초안 작업은 정밀 굽기 뒤 대여될 수 있다(수 분). 그동안 화면은 "AI 의견 준비 중"이다. 관리자가 먼저 확인해도 문제는 없다(의견은 참고).
- **REUSE 세션 사진 공백**(조사 위험 1): REUSE 로 끝난 세션은 `CAMERA_FRAME` 이 없어, 그 다음 NEW 세팅에서 이전 Scene 의 주인 세션을 찾더라도 사진은 그 Scene 을 만든 NEW 세션의 것이다(카메라 위치가 그 사이 바뀌었으면 `NOT_VISIBLE` 이 늘 수 있다). 후속 티켓 대상.
- **핀홀 근사**: 광각 가장자리 구역은 크롭이 수 % 어긋날 수 있다(여백으로 흡수 기대).
- **VLM 품질**: 2B 급 모델이 작은 물체 변화·조명 차이를 "바뀜"으로 볼 수 있다. 신뢰도는 표시하지 않고(모델 보정 안 됨) 배지·한 줄만 보인다.
- **서명 주소 만료**: 화면이 목록을 다시 받아 새 주소로 바꾼다. 다시 받기 전에 끝나면 그 사진만 "사진 없음".
- 계약 묶음 v7.44.0·V66 은 `-658`(v7.43.0·V65) 다음 번호다. MR 전에 dev 와 열린 브랜치의 번호를 다시 확인한다.

## 7. GPU 세션 확인 항목

1. 변경 의심 구역이 있는 NEW + World 이어짐 세팅에서 `vlm_zone_draft` 입력에 `suspected_zones`·`previous_frames` 가 실리는지(EC2 로그·작업 입력).
2. 워커 로그 `zone_opinions=[(zone, status, reason|action)]`, 구역당 VLM 지연, 작업 총시간(150 s 안), `TIME_BUDGET` 이 나오는지.
3. 고른 카메라·크롭이 실제 구역을 담는지(화면 썸네일로 눈 확인), 전후 같은 카메라인지.
4. 의견 한 줄이 한국어 한 문장이고 사람 이야기를 하지 않는지, KEEP·MODIFY·REMOVE 분포.
5. 정밀 굽기 동안 "AI 의견 준비 중" → 의견으로 바뀌는 시간.

## 8. 명세 정리 MR 대상

- 5.4 용도 4 에 "변경 의심 이어 쓴 구역의 전후 사진 비교 의견(유지·수정·삭제 권고, `zone_opinions`)" 한 줄 추가. 지금 명세는 새 구역 초안만 적는다.
- 12.5 `vlm_zone_draft` 1.1.0 입력·결과 필드, 12.6 구역 계획 응답의 `ai_opinion`.
- 8.2 세팅 구역 확인(-629·-630) 문단에 AI 의견 표시(참고 표시, 자동 반영 없음).
