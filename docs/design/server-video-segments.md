# EC2 상시 영상 조각 기록·조회·삭제

S15P31A101-330. 팀 결정(2026-10-29): 카메라 영상을 상시 저장하고 나중에 구간별로 다시 본다. 명세 v1.3 수정 36(3장 데이터 흐름, 5.1 상시 영상 조각,
11.3·11.5, 12.3·12.6). 계약 `contracts/messages/video_segment.schema.json` 1.0.0(계약 묶음 6.7.0).
코드는 `server/.../domain/video/`(`VideoSegmentIngestor`·`VideoService`·`VideoController`·`VideoPurgeService`), 업로드 목적은
`domain/upload`(`UploadPurpose.VIDEO_SEGMENT`·`SignRequest.segment_id`), 표는 V15. 시험은 `VideoSegmentIntegrationTest`·`VideoPurgeIntegrationTest`.

이벤트 클립(-261 수신·조회, -72 삭제, -69 서명 업로드)과 같은 방식을 그대로 따랐다. 다른 점은 이벤트와 묶이지 않고 보존 표시가 없다는 것뿐이다.

## 흐름

1. Tier 0 가 카메라별 조각(초기값 1분, 키프레임부터 재인코딩 없이)을 자른다. — Tier 0 구현은 AI 파트 후속
2. `POST /api/uploads/sign`(Tier 0 토큰) `{purpose: VIDEO_SEGMENT, content_type, size_bytes, sha256, segment_id}` → 키 `video-segments/{segment_id}.h264`
   와 PUT 서명 주소. 형식·크기·해시는 서명에 묶여 저장소가 다른 내용을 거부한다. 발급은 `upload_grant` 에 남는다.
3. Tier 0 가 S3 에 올리고 I1 `video_segment` 를 보낸다.
4. EC2 가 계약 검사 → `gateway_id`=`source_id`·구간 순서·형식 검사 → 발급과 키·형식·크기·해시가 같은지 확인 → `video_segment` 한 행, 발급은 쓰인 것으로 표시.
5. 관리자 `GET /api/videos?from&to&camera_id` 로 구간이 겹치는 조각 목록(주소 없음), `GET /api/videos/{segmentId}` 로 조각마다 재생 주소(5분, `video_access_log`).
6. 10분마다 삭제 작업이 `delete_after` 가 지난 조각의 객체를 지우고 `deleted_at` 을 쓴다. 조각 기록이 끝내 오지 않은 업로드도 발급 만료 14일 뒤 지운다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 전달 경로 | 이벤트 클립과 같은 I1 기록 메시지(`video_segment`) | Tier 0 → EC2 연결은 I1 하나다. 업로드·기록·검증 방식을 클립과 같게 두면 Tier 0 업로더 코드(`ai/services/tier0/clips/upload.py`)를 다시 쓸 수 있다 |
| 객체 키 | `video-segments/{segment_id}.{ext}`, 서버가 정함 | 클라이언트가 키를 고르지 못하게(남의 객체 덮어쓰기 방지, -69). 같은 segment_id 는 같은 키라 재시도해도 같다 |
| 업로드 주체 | Tier 0 토큰만 | 팀 결정(Tier 0 가 올림). 노트북·사용자는 거부(403) |
| 크기 상한 | 128 MiB | 1분 1080p H.264 수 Mbps 면 수십 MB. 클립(256 MiB)보다 작게 |
| 형식 | 이벤트 클립과 같은 표(`ClipFormat`: h264-annexb·mp4) | 화면이 클립과 같은 방식(MP4 로 감싸 재생, -301)을 쓴다 |
| 보관 | Tier 0 가 정한 `delete_after_ms`(자른 시각 + 14일) 뒤 EC2 삭제 작업이 지움. 보존 표시 없음 | 팀 결정 14일. 사고 구간은 이벤트 클립 보존 표시로 남긴다 |
| 삭제 작업 | 10분 주기, 클립 삭제 작업과 같은 전용 스레드(`clipPurgeTaskScheduler`), 조각 하나씩 행 잠금(SKIP LOCKED)·객체 먼저 삭제, 연속 3회 실패면 다음 주기 | -72 와 같다. 1분 조각·4대면 10분에 40개라 주기마다 많아야 수십 개 |
| 열람 권한 | 관리자만, 재생 주소마다 조회 기록 | 얼굴 흐림 없는 원본이다(11.5·14장, 이벤트 클립과 같음) |
| 목록 구간 | 24시간 이내. 구간이 겹치는 조각(시작이 from 보다 이른 조각 포함, 조각 길이 1시간까지 거슬러 봄) | 1분 조각·4대 하루 5,760행. 시작 시각 색인을 쓴다 |
| 목록 응답 | 주소·객체 키 없음. 지운 조각도 기록은 보임(`deleted_at_ms`) | 주소를 낼 때마다 조회 기록을 남기므로 목록은 주소 없이 준다(클립 목록과 같음) |
| 지운 조각 재생 | 410 `SEGMENT_DELETED` | 클립과 같음 |
| 수신 검사(계약 밖) | 조각 길이 1시간 이하, `delete_after_ms - created_at_ms` = 14일 ± 1일, 시각은 9999년까지, 정수는 int 범위. 어기면 계약 위반으로 버림 | 긴 조각은 목록 질의(시작을 1시간 거슬러 봄)에서 빠지고, 삭제 예정일이 어긋나면 영구 보존되거나 곧바로 지워진다. DB 오류 대신 계약 위반으로 센다 |
| segment_id | 서명 때도 uuid_v7 만 받음(400) | 기록은 v7 만 받으므로 다른 판이면 올린 객체가 고아로 남는다 |
| 삭제 작업 양 | 한 주기 1,000개, 쓰이지 않은 조각 발급에 부분 색인(`upload_grant_unused_video_idx`) | 조각 발급은 카메라당 분당 하나라 빨리 쌓인다. 클립 삭제와 같은 스레드를 오래 잡지 않게 |

## 시험

- `VideoSegmentIntegrationTest`(실제 포트, 실제 I1): Tier 0 서명 → 기록 → 발급 used_at, 구간이 걸치는 조각 조회·카메라 필터·빈 구간, 재생 주소와 조회 기록,
  재전송은 한 번만(DUPLICATE), 크기 다름·발급 없음·구간 거꾸로·gateway_id 다름·1시간 넘는 조각·14일 아닌 삭제 예정일·범위 밖 시각은 버림(invalid 7), 관리자 아니면 403, 시각 400·24시간 초과 400,
  없는 조각 404, 지운 조각 410(목록엔 보임), 업로드는 Tier 0 만·segment_id 필수·uuid_v7 만·같은 id 같은 키.
- `VideoPurgeIntegrationTest`(가짜 저장소): 삭제 예정일 지난 조각만 지우고 deleted_at, 다음 주기에 다시 안 지움, 저장소 오류면 deleted_at 안 씀·다음 주기에 지움,
  기록 없는 업로드는 발급 만료 14일 뒤 지움, 저장소 미설정이면 아무것도 안 함.

## 남은 일

- **Tier 0 조각 업로드(AI 파트)**: 링 버퍼에서 카메라별로 1분마다 잘라 서명 → 업로드 → `video_segment` 송신. EC2 단절 시 Tier 0 저장 위치에 먼저 두는지(클립의 spool)는 AI 쪽에서 정한다.
- 관제 앱 화면(조각 목록·이어 재생)은 프론트 몫. 응답은 클립과 같은 형식 칸(`content_type`·`container`)을 준다.
- 계약 `video_segment` 는 I1 이다. Lead 확인 후 머지한다(AGENTS 계약 우선).
- 저장 비용: 4대 × 수 Mbps × 14일이면 수백 GB. 운영 버킷 용량·요금은 배포 쪽에서 확인한다.
- 쓰인 조각 발급(`upload_grant`)과 `video_segment` 행은 지우지 않아 계속 쌓인다(분당 4행). 발표 후 일괄 파기 때 지우거나, 조각을 지울 때 발급도 지우는 정리를 더한다.
- 18.4 주간 점검 쿼리에 `video_segment` 삭제 지연(`VideoPurgeService.overdue`)을 더하는 일.
