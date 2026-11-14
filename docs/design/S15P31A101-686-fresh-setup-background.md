# S15P31A101-686 세팅 배경은 세팅 시작 뒤 새로 찍은 화면만 쓴다

관련 명세: 3.2(Tier 0·분석 워커 자원, 배경 누적), 4.3(세팅 1단계 사람 지운 배경), 8.1·13.4(설치·보정 화면). 계약 `messages/site_setup` 1.16.0.

## 문제(15차 GPU 세션, 2026-11-13)

사용자가 세팅 화면 안내대로 카메라 1을 돌린 뒤 세팅을 시작했는데 FLOOR_NOT_FOUND 로 두 번 실패했다. 원인은 세 가지였다.

1. 워커 `ProductionSiteSetupStages.collect` 가 세션 시작 이후 표본을 요구하지 않고 Tier 0 `state/clean-background/status.json` 의 기존
   완성 묶음(16:16 회전 도중 화면으로 만든 것)을 그대로 읽었다.
2. Tier 0 `_regenerate_bundle` 은 카메라별 `sample_count >= max_samples_per_camera(60)` 일 때만 최근 창(RECENT)을 쓰고 아니면 첫 기준
   (REFERENCE_FALLBACK)으로 대신했다. 2304×1296 표본 1장(BGR 약 9 MB + 마스크 약 3 MB ≈ 12 MB)이면 `max_bytes` 2.2 GiB 상한에서 4대가
   카메라당 약 48장에서 축출을 시작해 60장은 영원히 안 찼다 → 늘 첫 기준. `generation` 은 보정 문서 기준이라 물리 회전을 모른다.
3. 화면에 "카메라 앞을 비워 주세요" 같은 안내가 없었다.

## 바꾼 흐름

1. 1단계 '다음'(겹침 위험 확인창 뒤)이 세션을 바로 만들지 않고 **배경 촬영 창**을 띄운다. 「시작」을 눌러야 `POST /api/setup-sessions`
   를 보낸다. 창은 Esc·바깥 클릭으로 닫히지 않는다. 멈춘 세팅 이어 가기(paused_reason → resume)는 배경을 다시 찍지 않아 창 없이 보낸다.
2. 워커가 대여를 받아 `collect` 를 처음 부를 때 처리기 시계를 표본 하한(`since_ms`)으로 Tier 0 에 **배경 촬영 요청**을 쓴다
   (`state/clean_background/regenerate.request.json` 에 `fresh_since_ms`·`purpose: SITE_SETUP`, 기존 다시 만들기 경로 재사용).
3. Tier 0 는 요청이 열린 동안 표본 간격을 1 s 로 줄이고, 새 표본이 들어올 때마다 그 하한 뒤 표본만 센 카메라별 상태를 응답
   (`regenerate.response.json`, `status: COLLECTING`)에 쓴다. 모든 카메라가 준비되면 **하한 뒤 표본만으로** 묶음을 만들어
   `status.json` 을 먼저 바꾸고 READY 로 응답한 뒤 요청을 닫는다. 옛 묶음·첫 기준으로 대신하지 않는다.
4. 워커는 응답의 카메라별 상태를 site_setup 진행 `background_capture` 로 COLLECTING_BACKGROUND 하트비트에 싣고, READY 묶음이 이 요청의
   하한(`sample_since_ms`)·활성 보정과 맞을 때만 쓴다. 대여가 끝나면(성공·실패·취소) 남은 요청을 거둔다.
5. EC2 는 `setup_session.background_capture`(V71)에 마지막 값을 두고 세션 조회에 싣는다. 화면 창은 카운트다운·카메라 4대 영상·
   카메라별 상태(완료 / 촬영 중 n/12 / "카메라 3 앞에 사람이 보입니다 · 화면 밖으로 이동하면 촬영을 이어갑니다" / 영상 없음)를 보이고,
   세션이 다음 단계로 넘어가면 저절로 닫힌다. 끝내 못 찍어 `BACKGROUND_INCOMPLETE` 로 끝나면 그 카메라를 알리고 「다시 촬영」(같은 높이로
   retry)·「닫기」를 둔다.

## 정한 수치와 근거

| 값 | 위치 | 근거 |
|---|---|---|
| 세팅 표본 간격 1,000 ms | `CleanBackgroundSettings.setup_sample_interval_ms` | 평소 5 s 면 20 s 에 4장뿐이다. 1 s 면 걷는 사람이 한 자리를 지나는 동안(약 1 s) 표본이 겹치지 않고, 카메라 4대 × 1장/s × 약 12 MB = 약 48 MB/s 로 공유 메모리 칸(4 × 10 MB)·자식 프로세스가 감당한다. 누적기 간격은 min(평소, 세팅)이고 평소 간격은 `offer` 가 지킨다. 보관 창(평소 간격 × 최대 표본 수 = 300 s)은 그대로다. |
| 카메라당 최소 표본 12장 | `setup_min_samples_per_camera` | 12 s 안에 끝나 화면 카운트다운 20 s 안에 8 s 여유(검출 큐가 바빠 버린 표본·사람 가림)가 남는다. 유효 마스크 규칙(드러난 횟수 ≥ max(2, ⌈n/2⌉) = 6, 분산 ≤ 225)이 의미를 갖는 수다. 4대 × 12장 × 12 MB ≈ 576 MB 로 2.2 GiB 상한 안이다. |
| 못 채운 비율 상한 2% | 기존 `max_unrevealed_ratio` | 사람에 가린 픽셀은 기존 never_revealed·mask 규칙 그대로다. 사람이 서 있으면 그 카메라만 PERSON_OCCLUDED 로 남고, 나가면 다음 표본부터 드러나 준비된다(다른 카메라는 다시 찍지 않는다). |
| 영상 없음 판정 5 s | Tier 0 `NO_VIDEO_GRACE_MS`, 워커 `CAPTURE_NO_VIDEO_GRACE_MS` | 세팅 간격의 5배. Tier 0 는 다른 카메라 관측 시각(데이터 시계)으로, 워커는 Tier 0 응답이 없거나 멈춘 경우도 처리기 시계로 본다(하한 뒤 프레임이 하나도 없을 때만). |
| 촬영 요청 자체 만료 600 s | `setup_capture_max_ms` | 워커가 죽어 요청이 남아도 Tier 0 가 관측 시각 기준 10분 뒤 NOT_READY(CAPTURE_EXPIRED)로 닫고 평소 간격으로 돌아간다. 워커 대기 상한(240 s)·작업 실행 상한(900 s)보다 길다. |
| 워커 대기 상한 240 s | 기존 `SIMAI_SETUP_BACKGROUND_WAIT_S` | 그대로. 최소 12 s 에 사람이 화면 밖으로 나갈 여유. 넘으면 재시도 가능한 BACKGROUND_INCOMPLETE 이고, 시간 초과 전에 마지막 카메라별 상태를 먼저 보낸다. |
| 화면 카운트다운 20 s | `CAPTURE_SECONDS` | 12장 × 1 s + 여유. 워커가 처음 진행을 알린 때부터 센다(대여를 기다리는 동안은 "촬영을 준비하는 중"). 0 이 돼도 서버가 준비될 때까지 "거의 다 찍었습니다"로 기다린다. |
| 관제 중 다시 만들기 '가득 찬 창' | `_full_samples` = min(최대 표본 수, ⌊max_bytes ÷ (표본 1장 크기 × 카메라 수)⌋), 최소 표본 수 아래로 두지 않음 | 2304×1296 4대면 48장. 60장은 메모리 상한 때문에 도달할 수 없었다. 모자라면 NOT_READY + 카메라별 사유(대체 없음). |

## 계약·마이그레이션

- `messages/site_setup` 1.16.0: progress 선택 `background_capture`(`since_ms`, `required_samples`, 선택 `sample_interval_ms`,
  `cameras{state, sample_count, person_visible?}`). `job_heartbeat` 2.15.0. 계약 묶음 v7.48.0(마이너, v7.47.0 은 -693).
- DB V71 `setup_session.background_capture jsonb`. 같은 대여에서 값 없는 스냅샷은 이전 값을 두고, 재대여는 지운다(`keepAnchor` 와 같은 규칙).
- Tier 0 ↔ 워커 요청·응답 파일(`fresh_since_ms`, 응답 `status: COLLECTING|READY|NOT_READY`, 카메라별 `state`·`sample_count`·
  `person_visible`·`never_revealed_ratio`·`last_frame_ms`)은 같은 Pod 안 파일 규약이라 계약 밖이다(기존 regenerate 파일과 같다).
- 배포 순서: EC2 먼저(옛 EC2 는 새 필드가 든 하트비트를 계약 검사로 거부한다), 그다음 워커·Tier 0 이미지. 프론트는 EC2 와 함께.
  옛 Tier 0 + 새 워커는 `fresh_since_ms` 를 몰라 기존 다시 만들기(가득 찬 창)로 처리하므로, 묶음의 `sample_since_ms` 가 없어 워커가
  BACKGROUND_INCOMPLETE 로 끝낸다(옛 묶음을 쓰지 않는다). Tier 0 와 워커는 같은 이미지라 함께 바뀐다.

## 버린 대안

- **보정 generation 으로 회전 감지**: 물리 회전은 보정 문서를 바꾸지 않아 generation 이 그대로다. 세팅 시작 시각 하한이 확실하다.
- **옛 기준(REFERENCE_FALLBACK)을 화질 보강용으로 남기기**: 카메라가 움직였는지 모르는 채 옛 화면을 섞으면 15차와 같은 실패가 난다. 모르면 비운다.
- **세팅 중 사람 없는 표본만 세기**: 관리자가 카메라 시야 안 노트북에 앉아 있으면 영원히 못 찍는다. 기존 마스크 규칙(가린 자리만 빼고
  드러난 자리는 쓴다)이 사람 이동에 더 강하다. 대신 마지막 표본에 사람이 보이면 그 카메라를 PERSON_OCCLUDED 로 알린다.
- **서버 입력에 하한 시각(`background_since_ms`)을 새로 싣기**: 계약 입력·서버 시계·Pod 시계 맞춤이 더 필요하다. 「시작」이 곧 세션 생성이고
  워커 대여가 곧바로 뒤따르므로 대여 시각을 하한으로 쓴다(그 사이 표본을 버릴 뿐이라 보수적이다).
- **실패한 카메라만 재시도(새 세션) 때 이전 표본 이어 쓰기**: 하한을 세션 사이로 넘기려면 retry 입력·Tier 0 상태 공유가 필요하다. 같은
  대여 안에서는 준비된 카메라를 다시 찍지 않으므로(사람이 나가면 그 카메라만 이어 찍음) 240 s 안에서는 충족한다.
- **창과 아래 칸이 영상 연결을 같이 쓰기**: 미리보기 렌더러가 카메라당 캔버스 하나라, 창이 열린 동안 아래 칸 영상(CameraMapPanel)을 내리고
  창이 자기 연결을 연다(한 번에 하나).

## 잔여 위험

- 세팅 촬영 중 관제(Tier 0) 판정은 그대로 돌지만 배경 누적 자식 프로세스 부하가 1 s 간격으로 5배 오른다(약 12 s~수십 초 동안). 평소
  `offload` 자식 프로세스·유계 큐라 판정 스레드는 막지 않는다(큐가 차면 표본을 버리고 dropped 만 늘어난다).
- `scene_regenerate`(관제 중 다시 만들기)는 이제 가득 찬 창(4대 2304×1296 기준 48장 × 5 s ≈ 4분 분량)이 모이기 전에는 NOT_READY 로 실패한다
  (예전에는 첫 기준으로 대신해 성공했다).
- 같은 요청 파일을 세팅과 관제 중 다시 만들기가 함께 쓴다. 세팅은 관제 중(작업 IN_PROGRESS)에 시작할 수 없고 `scene_regenerate` 는 관제
  중에만 받으므로 겹치지 않는다고 본다.
- 처리기 시계(워커)와 프레임 관측 시각(게이트웨이 촬영 시각 추정)이 어긋나면 하한이 그만큼 밀린다. 워커가 앞서면 표본을 조금 더 기다리고,
  뒤처지면 시작 직전 몇 초 표본이 섞일 수 있다(두 시계 모두 NTP).
- 카운트다운은 브라우저 시계로 센다(표시용).

## 넘긴 일

- 15·16차 GPU 세션에서 확인: 카메라를 돌린 직후 세팅이 새 화면으로 FLOOR_NOT_FOUND 없이 지나가는지, 사람이 한 카메라 앞에 서 있을 때 그
  카메라만 PERSON_OCCLUDED 로 남았다가 나가면 이어 찍히는지, 1 s 간격 동안 Tier 0 지연 p95 가 늘지 않는지.
- 카메라 확인 카드 등 2단계의 다른 흐름은 -687·-691 이 바꾼다(이 티켓은 1단계 → 2단계 전환의 배경 촬영 창만).
- 명세 4.3(세팅 배경 수집)·8.1 화면 흐름에 "세팅 시작 뒤 표본만·배경 촬영 창"을 명세 정리 MR 에서 반영.

## 코드·시험 위치

- Tier 0: `ai/services/tier0/clean_background.py`(`_regenerate`·`_regenerate_fresh`·`_assemble_bundle`·`_full_samples`·`capture_state`),
  `ai/services/tier0/settings.py`, 누적기 `ai/src/simai_ai/calibration/world_frame/clean_background.py`(`since_ms`·`min_samples`).
- 워커: `ai/services/worker/site_setup_stages.py`(`collect`·`release_capture`), `ai/services/worker/site_setup.py`(`background_capture` 하트비트).
- 서버: `SetupBackgroundCapture`, `SetupService`, `V71__setup_session_background_capture.sql`.
- 화면: `front/apps/admin/src/features/setup/BackgroundCaptureModal.tsx`·`backgroundCapture.ts`·`useBackgroundCapture.ts`, `SetupPage.tsx`.
- 시험: `ai/tests/services/tier0/test_clean_background.py`(옛 묶음·요청 전 표본 기각, 사람·영상 없음 사유, 만료, 가득 찬 창 도달),
  `ai/tests/services/worker/test_site_setup_stages.py`, `SetupBackgroundCaptureTest`·`SetupSessionIntegrationTest`,
  `backgroundCapture.test.ts`·`SetupPage.test.tsx`(배경 촬영 창).
