# EC2 이벤트 클립 수신·조회·보존 표시

S15P31A101-261. 명세 5.1(이벤트 클립 링 버퍼), 11.3(`media_clip`), 11.5(14일 보관·CRITICAL 자동 보존·아차사고 보존 표시·조회 기록),
12.3(I1 `media_clip`·`media_clip_retention`), 12.6(`GET /api/clips/{id}`, `PUT /api/clips/{id}/retention`, 관리자), 14(권한자 열람·조회 기록),
FR-59·99, 계약 `media_clip`·`media_clip_retention` 1.x.
코드는 `server/.../domain/clip/`(`MediaClipIngestor`·`MediaClipRetentionIngestor`·`ClipService`·`ClipController`), 시험은 `MediaClipIntegrationTest`.
보내는 쪽은 `ai/services/tier0/clips/record.py`(기록 형식), 화면은 `front/apps/admin/src/features/clips/`(S15P31A101-253).

## 흐름

1. Tier 0 가 클립을 `POST /api/uploads/sign`(EVENT_CLIP, S15P31A101-69)으로 받은 주소에 올리고 I1 `media_clip` 을 보낸다.
2. EC2 는 계약 스키마 전체로 검사하고, 객체 키·형식·크기·해시·이벤트가 그 클립에 발급한 `upload_grant` 와 같은지 맞춘 뒤 `media_clip` 한 행을 쓰고
   발급의 `used_at` 을 채운다. 처음부터 보존 표시가 있으면(CRITICAL 자동) `media_clip_retention` 에 `CRITICAL_AUTO` 이력을 남긴다.
3. 보낸 뒤 이벤트가 CRITICAL 로 오르면 Tier 0 가 `media_clip_retention` 을 보낸다. 이력 한 행을 더하고 클립의 현재 보존 표시를 켠다.
4. 관리자 화면이 `GET /api/events/{id}/clips` 로 목록을, `GET /api/clips/{id}` 로 재생 주소를 받고, `PUT /api/clips/{id}/retention` 으로 보존 표시를 붙이거나 뗀다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 이벤트별 목록 | `GET /api/events/{id}/clips`(관리자) 신설. `{event_id, clips[]}`, 위험 구간(resume_count)·카메라 순. 지운 클립도 `deleted_at_ms` 와 함께 보인다. 없는 이벤트 404 `EVENT_NOT_FOUND` | 12.6 에 목록 경로가 없다. 관제 앱이 이 경로로 mock 을 만들어 두었다(S15P31A101-253). 이벤트 상세에 넣으면 반장도 받게 되어 권한이 섞인다 |
| 클립 칸 | 계약 `media_clip` 과 같은 snake_case, 시각은 `*_ms`(UTC ms). 서명 주소·객체 키는 목록에 싣지 않는다. `auto_retained`(CRITICAL 연결분이라 뗄 수 없음)를 더한다 | 화면이 '떼기' 를 막을 수 있게. 객체 키는 재생 주소 발급 때만 쓴다 |
| 재생 주소 | `{clip_id, url, expires_at_ms, content_type, container}`. 5분(`download-ttl`) GET 서명 주소. 요청마다 새로 서명하고 `clip_access_log` 한 행(사용자·발급 시각·주소 만료). 서명에 실패하면 기록하지 않는다 | 14장 "열람은 권한자, 조회 기록". 주소를 받은 순간을 열람으로 본다(재생 여부는 서버가 알 수 없다) |
| 재생 형식 | EC2 는 영상을 변환하지 않는다. 원본 `video/h264`·`h264-annexb` 를 그대로 서명하고 형식을 알려 화면이 브라우저에서 MP4 로 감싸 재생한다(재인코딩 없음) | EC2 는 영상 바이트를 다루지 않는다(서명만, 14장). 서버 변환은 ffmpeg·객체 내려받기·다시 올리기가 들고 EC2 디스크(11.5 70% 기준)를 쓴다 |
| 삭제된 클립 | 재생 주소 410 `CLIP_DELETED`. 목록에는 남는다 | 기록은 남고 객체만 지운다(11.5 `deleted_at`). 없는 것(404)과 지운 것을 화면이 구분하게 |
| 권한 | 세 API 모두 관리자. 반장·열람 403 | 12.6 "관리자", 14장 권한자 제한 |
| 보존 표시 떼기 | 자동 보존(`auto_retained` = 보존 표시가 켜져 있고 클립 등급이 CRITICAL 이거나 `CRITICAL_AUTO` 이력이 있음)은 409 `CLIP_AUTO_RETAINED`. 이벤트 등급만 CRITICAL 이고 보존 표시가 꺼진 클립은 자동 보존으로 보지 않는다. 사유 입력은 받지 않는다 | 11.5 "CRITICAL 이벤트 연결분은 자동 보존, 아차사고는 관리자가 붙이거나 뗌". 표에 사유 칸이 없다 |
| 보존 표시 이력 | 값이 바뀔 때만 `ADMIN_HOLD`·`ADMIN_RELEASE` 한 행(관리자 id). 같은 값이면 바뀐 것 없이 현재 클립을 돌려준다. 바꾼 뒤에는 다시 읽어 `auto_retained` 까지 맞춘 클립을 돌려준다. 지운 클립에 붙이기는 410 | 이력이 실제 변경만 담게. 지운 객체는 되살릴 수 없다 |
| Tier 0 보존 기록 재전송 | V11 로 `media_clip_retention.msg_id`(유일)를 더해 같은 메시지는 한 행 | 이력 표라 재전송마다 행이 늘던 것을 막는다(12.1) |
| 올라온 키 검증 | 키가 `event-clips/<event_id>/<clip_id>.` 로 시작하고, `upload_grant`(EVENT_CLIP)의 형식(소문자로 맞춤)·크기·해시·이벤트가 기록과 같아야 한다. 형식과 `container` 도 맞아야 한다(`ClipFormat`: `video/h264`↔`h264-annexb`, `video/mp4`↔`mp4`, 업로드 서명의 키 확장자와 같은 표). 다르면 계약 위반으로 버린다(`result=invalid`). 발급의 `used_at` 은 EC2 가 받은 시각 | 저장소는 서명에 묶은 내용만 받으므로, 같으면 객체가 기록이 말하는 내용이다(-69 남은 일) |
| 이벤트보다 먼저 온 클립·클립보다 먼저 온 보존 기록 | 받은 봉투 그대로 `i1_deferred_message`(V11)에 보관하고 `ClipDeferredRetrier` 가 1초부터 두 배씩(최대 1분) 다시 처리한다. 저장할 때 받은 시각은 처음 받은 시각. 24시간(`simai.clip.deferred-give-up`) 안에 앞선 메시지가 오지 않으면 경고를 남기고 버린다 | 클립 기록·보존 기록은 저널 메시지가 아니라 Tier 0 가 다시 보내지 않는다. 실패로 두면 기록이 사라지고 객체만 남는다(재연결 때 저널 재생과 스풀 클립이 겹친다) |
| 이벤트 확인 | 클립의 이벤트는 그 Tier 0(`gateway_id`)의 LIVE 이벤트여야 한다(아니면 계약 위반). 위험 구간(`resume_count`)과 클립을 만든 전이(`event_sequence`, `risk_event_update`)가 이미 와 있어야 하고(아니면 보관), 클립 등급은 그 전이의 등급과 같아야 한다(아니면 계약 위반) | 계약 "클립은 LIVE 이벤트에만"(AGENTS 5) |
| 보존 표시 출처 | CRITICAL 클립은 `retention_hold` 가 false 로 와도 켜서 저장하고(경고) `CRITICAL_AUTO` 이력을 남긴다. HIGH 의 보존 표시는 이력 없이 현재 값만. 지운 클립에 온 보존 기록은 반영하지 않는다(중복으로 셈). `media_clip_retention` 은 그 클립을 보낸 Tier 0 의 것만 받는다 | 계약은 HIGH 의 CRITICAL_AUTO 를 막는다. 다른 Tier 0 가 남의 클립을 잠그지 못하게 |
| 같은 기록이 동시에 올 때 | 같은 위험 구간·카메라를 트랜잭션 잠금(`pg_advisory_xact_lock`)으로 세워 하나씩 처리한다. 나중 것은 중복 | 재연결이 겹치면 같은 기록이 동시에 처리될 수 있다. 행이 없는 클립은 행 잠금으로 막지 못한다 |
| 같은 위험 구간·카메라의 다른 clip_id, 같은 clip_id 의 다른 내용(해시·Tier 0·이벤트·위험 구간·카메라) | 계약 위반으로 버린다. 같은 clip_id 의 같은 내용만 중복 | 계약 "(event_id, resume_count, camera_id)마다 클립은 하나", 보낸 기록은 고치지 않는다 |
| 늦게 생긴 클립 알림 | 실시간 알림은 두지 않는다. 화면이 이벤트 상세를 열 때 목록을 다시 받는다 | 클립은 사후 구간(20초)과 업로드 뒤에 생겨 관제 경보와 무관하다 |

## 시험

`MediaClipIntegrationTest`(실제 포트, I1 + REST): CRITICAL 클립 수신 → 목록·발급 사용 표시·`CRITICAL_AUTO` 이력, 재생 주소(서명 주소·만료 5분·형식)와
요청마다 조회 기록·반장·열람 403(기록 없음)·없는 클립 404·지운 클립 410·없는 이벤트 404, 관리자 보존 표시 붙이기·떼기와 바뀔 때만 이력·CRITICAL 떼기 409·본문 없음 400·
지운 클립 붙이기 410, Tier 0 보존 기록 → 표시 켜짐·재전송 한 행·이후 떼기 409, 발급과 다른 해시·발급 없음·같은 구간 다른 clip_id·다른 게이트웨이·형식과 다른 container·
다른 Tier 0·REPLAY 이벤트는 invalid, 대문자 형식은 받음, 재전송 duplicate, 이벤트 전 클립은 보관(failed 아님), 발급 사용 시각 = 받은 시각, 보존 표시가 있는 HIGH 클립은 이력 없이 관리자가 뗌·
다른 Tier 0 의 보존 기록은 invalid.

## 시험(추가)

`MediaClipIntegrationTest` 에 이벤트보다 먼저 온 클립·클립보다 먼저 온 보존 기록을 보관했다가 이벤트가 오면 저장(받은 시각은 처음 받은 시각, 보관함 재전송 한 번),
보존 표시가 꺼진 CRITICAL 클립을 켜서 저장, 전이와 등급이 다른 클립 invalid, 지운 클립의 보존 기록은 반영하지 않음을 더했다.

## 남은 일

- 관제 앱(S15P31A101-253): 응답에 `contract: DEMO_UNCONFIRMED` 를 싣지 않으므로 그 검사를 빼고, `h264-annexb` 를 브라우저에서 MP4 로 감싸 재생한다.
- 명세 12.6 API 표에 `GET /api/events/{id}/clips` 행을 더한다(명세 정리).
- 클립 삭제 작업과, 이벤트가 CRITICAL 로 올랐는데 Tier 0 보존 기록이 오지 않은 클립의 보존 표시는 S15P31A101-72 에서 했다(`server-data-retention.md`).
- Tier 0 가 `media_clip`·`media_clip_retention` 을 I1 로 보내는 연결(ai, 형식은 `record.py`): S15P31A101-748 에서 했다(`S15P31A101-748-event-clip-upload.md`).
- 실제 S3 로 재생 주소를 열어 본다(시험은 서명 계산만 본다).
