# 배포 서버 시연용 목 데이터 (S15P31A101-478)

배포 서버(simai.live)의 히스토리 화면과 이벤트 패널을 시연·확인할 때 쓸 목 데이터를 넣고 지우는 방법이다. 실제 기록이 거의 없거나 추적 오류로 이벤트가 쏟아질 때 쓴다. 목 데이터는 나중에 지운다.

## 관련

- 히스토리 질의: `docs/design/db-verification.md` 5장 Q1~Q3 (`GET /api/work-units`, `GET /api/replay`)
- 이벤트 클립: `deploy/ec2/seed-mock-clips.sh` (S15P31A101-261). 상시 영상 조각: `docs/design/server-video-segments.md` (S15P31A101-330)
- 진행 중 이벤트의 카메라 버튼: `front/apps/admin/src/features/events/CameraButton.tsx`

## 코드 위치

- `deploy/ec2/seed-mock-history.sh`: 히스토리 목 작업·위치·시스템 상태·이벤트·영상 조각 적재(`seed`), 확인(`status`), 정리(`cleanup`). `--dry-run` 은 한 트랜잭션으로 돌려 보고 되돌린다(S3 복사·삭제는 하지 않는다).
- `front/apps/admin/src/features/history/`: 다시 보기의 CCTV 4칸이 재생 시각을 덮는 상시 영상 조각(`GET /api/videos`)을 영상으로 튼다(`replaySegments.ts`, `useReplaySegments.ts`, `ReplayVideoWall.tsx`). 덮는 조각이 없는 시각은 목 화면 그림이다. 이벤트 클립은 쓰지 않는다(실제 CCTV 증거라 히스토리 목 영상과 섞지 않는다).
- 이벤트 패널 확인용 목 이벤트·클립은 스크립트 없이 SQL 로 직접 넣었다(아래 "이벤트·클립 목").

## 동작

히스토리 화면이 읽는 표는 `work_unit`(목록), `world_track_state`·`system_health`·`risk_event_update`(다시 보기)다. 이 질의는 gateway 로 거르지 않는다. 그래서 두 가지로 섞임을 막는다.

- 넣기 전에 칸마다 `[시작-5초, 끝+5초]` 에 목이 아닌 기록이 있는지 보고, 있거나 아직 끝나지 않은 칸은 뺀다(뺀 칸과 이유를 보여 준다).
- 목 행에 표시를 붙인다. world·health·event·update 는 `gateway_id = tier0-mock-history`, `work_unit` 은 `created_by IS NULL` 이다(API 로 만든 작업은 로그인 사용자가 찬다).

작업마다 활성 카메라별 목 영상 조각(`video_segment`)을 작업 시작부터 `MOCK_VIDEO_SECONDS` 초 넣는다. 조각 행은 객체 키가 달라야 해서(`object_key` UNIQUE) `seed` 가 끝난 뒤 원본 S3 객체(`MOCK_VIDEO_KEY`)를 `MOCK_SEGMENT_PREFIX` 아래로 조각마다 복사하고, `cleanup` 이 복사본을 지운다. 원본 객체는 이 스크립트가 올리지 않고, 그 객체를 가리키는 클립 행에서 크기·해시를 읽는다(없으면 조각을 넣지 않는다). 복사본이 없으면 영상 칸은 "영상을 받지 못했습니다"로 보인다.

`seed` 는 목 행이 이미 있으면 멈춘다. `cleanup` 은 목 이벤트를 가리키는 다른 표 행(클립·확인 등)이 있으면 멈추고, 없으면 `work_unit` → `risk_event_update` → `risk_event` → `world_track_state` → `system_health` 순으로 지운다.

## 정한 값

| 값 | 정한 것 | 근거 |
|---|---|---|
| 기간 | `--end-date`(기본 오늘 KST)부터 `--days`(기본 6)일, 일요일 제외 | 목록은 끝난 지 7일 안의 작업만 보여 준다. 시연 직전에 `cleanup` 뒤 다시 `seed` 한다 |
| 시간대 | 08:00–09:30, 17:00–18:00 KST | 팀이 실제로 기록하는 시간(대개 10:00–16:30)을 피한다 |
| 작업 | 개구부 옆 비계 설치(4명), 자재 양중(3명), 전기 배선 정리(2명)를 돌려 쓴다 | 구역 이름이 "새 구역"이라 `place` 를 따로 채운다. 구역은 DB 의 활성 구역을 순서대로 쓴다 |
| 작업자 | 계획 인원 그대로 또는 1명 적게, 0~3분 사이에 엇갈려 들어온다. 둘째는 +20분에 5분 비운다 | 작업자 패널의 "잡힘·계획" 차이를 보인다 |
| 위치 | 2 Hz, 작업 장소 둘레를 타원으로 약 0.6 m/s | 90분 작업의 화면 step 이 1.8 s 라 2 Hz 면 충분하다 |
| 이벤트 | R5→R1(+10분, 구역 작업), R1(+40분, 90분 작업), R7(+25분), R4 낙상 1건. 전부 RESOLVED | 활성 경보로 남지 않고, DB 직접 INSERT 라 알림·워치로 나가지 않는다 |
| 데이터 없음 | 오전 작업 앞의 둘에 +30분부터 45초(world·health 모두 없음) | 타임라인의 "데이터 없음" 줄무늬를 보인다 |
| 영상 | 작업 × 활성 카메라, 작업 시작부터 179초, 조각마다 S3 복사본 | 다시 보기가 상시 영상 조각을 튼다. `video_segment.object_key` 가 UNIQUE 라 파일 하나를 같이 쓸 수 없다. `cleanup` 이 행과 복사본을 지운다 |
| 입력 출처 | `input_provenance` frames·tracks `SIM`, `is_upper_bound = true` | 규칙 7 의 관측·추정 구분. 목은 제품 성능으로 쓰지 않는다 |
| system_health | 가장 새 실제 행을 본떠 상태만 `OK` | 목 행이 가장 새 행이 되어도 계약 모양을 지킨다 |
| 이벤트 출처 | `LIVE` | 목록의 "위험 N 경고 N" 이 보인다. `risk_event_update` 가 LIVE 만 받는다 |

## 이벤트·클립 목

이벤트 패널(진행 중·최근 해소)과 클립 재생 확인용이다.

- `risk_event` 3건: event_id `0de00000-0000-7000-8000-00000000000{1,2,3}`, `scene_version = MOCK-DEMO`. 둘은 RESOLVED(위험·경고), 하나는 진행 중(UPDATED).
- 해소 이벤트마다 카메라 4대 `media_clip` 행(`redactor_version = MOCK-SEED`)을 넣고, 8행이 S3 객체 하나(`event-clips/mock-demo/walk-calibration-cam_C.mp4`)를 같이 가리킨다.
- 정리: 위 event_id 의 `clip_access_log`·`media_clip_retention`·`media_clip`, 누가 확인을 눌렀으면 `event_ack`, 마지막으로 `risk_event` 를 지우고 S3 객체를 지운다. 저장소 계정에 `s3:ListBucket` 이 없어 `s3 ls` 는 안 되고 `s3api head-object` 로 본다.

## 버린 대안

- REPLAY 로 넣기: 다시 보기에만 보이고 목록의 수가 0 이라 시연에서 밋밋하다. LIVE 는 이벤트 목록·보고서 집계에도 들어가는 대신 정리 명령으로 지운다.
- `skeleton_3d` 넣기: LEFT JOIN 이라 없어도 되고, 모르면 비운다는 원칙을 따른다.
- 행마다 도는 루프: 모두 `generate_series` 집합 INSERT 로 한다.

## 잔여 위험

- LIVE 목 이벤트는 이벤트 목록과 일일·TBM·마무리 보고서 집계에 섞인다. `risk_event` 에 출처 칸이 없어 표시는 `gateway_id` 뿐이다.
- 목 `system_health` 가 실제 기록이 없는 동안 가장 새 시스템 상태 행이 될 수 있다(시각이 오래돼 화면은 끊김으로 본다).
- 다시 보기가 조각을 틀 때마다 서버가 조회 기록(`video_access_log`)을 남긴다. 칸이 나타날 때마다 주소를 새로 받으므로 구간을 앞뒤로 오가면 기록이 늘어난다.
- 영상은 작업 시작 3분만 있고 나머지 시각은 그림이다. 작업 4건이면 복사본 16개(약 400MB)가 생긴다. 저장소 계정에 `s3:GetObjectTagging` 이 없어 복사는 `--copy-props none` 이다.
- `world_track_state`·`system_health` 는 7일 뒤 압축된다. 그 뒤 `cleanup` 은 느려진다.
- 첫 운영 배포의 DB 초기화 절차(`ec2-deploy.md`)에서 목 데이터도 함께 지워진다.
- 화면은 계약에 맞지 않는 줄을 알리지 않고 버린다. 넣은 뒤 로그인한 화면에서 `/api/replay` 응답을 받아 `contracts/messages` 스키마로 검사한다.
