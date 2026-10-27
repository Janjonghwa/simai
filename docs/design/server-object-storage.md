# EC2 객체 저장소와 임시 서명 업로드 주소

S15P31A101-69. 명세 3.1(객체 저장소 S3 또는 Lightsail 버킷), 11(영상·사진·3D 자산은 객체 저장소, DB 에는 주소), 12.6(`POST /api/uploads/sign` 기기·반장), 14(비공개, 임시 서명 주소로만 업로드·조회).
코드는 `server/.../global/storage/ObjectStorage`(서명), `server/.../domain/upload/`(API). 받는 쪽은 `ai/services/tier0/clips/upload.py`(클립 업로더, 서명 클라이언트는 아직 없음).

## 흐름

1. Tier 0·기기·반장이 올릴 내용의 목적·형식·크기·SHA-256 을 `POST /api/uploads/sign` 으로 보낸다.
2. EC2 가 목적별 규칙을 검사하고 객체 키를 정해 PUT 서명 주소를 만든다. 발급은 `upload_grant` 에 한 행 남긴다.
3. 클라이언트는 `upload_url` 로 `headers` 를 그대로 실어 PUT 한다. 형식·크기·해시가 서명에 묶여 있어 다른 내용이면 저장소가 거부하고, 만료(5분) 뒤에는 저장소가 403 으로 거부한다.
4. 메시지에는 서명 주소가 아니라 `object_key`(media_clip) 또는 `object_uri` `s3://버킷/키`(event_frame_ready)를 싣는다. 조회는 EC2 가 그때그때 GET 서명 주소를 낸다(`ObjectStorage.signGet`, 클립 조회 API 가 쓴다).

요청·응답(필드는 snake_case):

```
POST /api/uploads/sign
{ "purpose": "EVENT_CLIP", "content_type": "video/h264", "size_bytes": 1234, "sha256": "<16진수 64자>",
  "event_id": "<uuid>", "clip_id": "<uuid>" }
→ 200 { "object_key": "event-clips/<event_id>/<clip_id>.h264", "object_uri": "s3://<버킷>/event-clips/...",
        "method": "PUT", "upload_url": "https://...", "headers": { "content-type": "...", "content-length": "1234",
        "x-amz-checksum-sha256": "<Base64>" }, "expires_at_ms": 1760342700000 }
```

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 저장소 | AWS S3(서울, `ap-northeast-2`). S3 호환(Lightsail·MinIO)은 `endpoint`·`path-style` 로 | 명세 3.1·11.6(AWS 서울 리전). SDK 는 AWS SDK for Java v2 `S3Presigner`(서명만, 저장소 호출 없음) |
| 자격 증명 | 운영은 EC2 인스턴스 역할(기본 공급자). 정적 키는 로컬 S3 호환 저장소에서만 | 비밀 값을 `.env` 에 두지 않게(14). 인스턴스 역할에 버킷의 `s3:PutObject`·`s3:GetObject` 만 준다 |
| 미설정 | `SIMAI_STORAGE_BUCKET` 이 비면 서버는 뜨고 서명은 503 `STORAGE_NOT_CONFIGURED`. 자격 증명을 못 찾으면 503 `STORAGE_UNAVAILABLE` | 업로더가 단절과 같이 다루고 다시 시도한다(upload.py `SignerUnavailable`) |
| 인증 | 한 경로에서 Tier 0 토큰·기기 토큰·사용자 JWT 를 받는다(`SecurityConfig` 업로드 체인). 기계 토큰이 맞으면 JWT 해석을 건너뛴다 | 12.6 권한 "기기·반장". 기계 토큰을 JWT 로 읽으면 401 이 난다(-222 에서 미뤄 둔 일) |
| 목적별 주체 | `EVENT_FRAME`·`EVENT_CLIP`: Tier 0 토큰·기기 토큰. `TICKET_PHOTO`: 반장 JWT. 그 밖은 403 `UPLOAD_NOT_ALLOWED`. 열람·관리자 JWT 와 워커 토큰은 받지 않는다 | 계약 event_frame_ready·media_clip 은 Tier 0 가 보낸다. db-tables 는 "Tier 0 는 기기 토큰" 이라 둘 다 받는다. 조치 사진은 반장(FR-41) |
| 형식·크기 | 프레임 `image/jpeg`·`image/png` 16 MiB, 클립 `video/h264`·`video/mp4` 256 MiB, 사진 `image/jpeg`·`image/png`·`image/webp` 20 MiB | 클립은 재인코딩 없는 H.264(media.py `video/h264`). 크기는 수십 초 클립·원본 사진이 들어가는 상한 |
| 내용 묶기 | `Content-Type`·`Content-Length`·`x-amz-checksum-sha256` 을 서명 헤더로 묶는다 | 받은 주소로 다른 내용·큰 파일을 올리지 못하게. 저장소가 해시를 확인한다 |
| 객체 키 | 서버가 정한다. 클립 `event-clips/<event_id>/<clip_id><확장자>`, 프레임 `event-frames/<event_id>/<무작위 uuid>`, 사진 `ticket-photos/<무작위 uuid>` | 클라이언트가 키를 고르면 남의 객체를 덮어쓸 수 있다. 클립은 clip_id 로 정해 재시도해도 같은 키 |
| 클립 재발급 | 같은 키는 같은 목적·주체이고 `used_at` 이 비었을 때만 다시 발급한다. 내용(형식·크기·해시)은 같거나 앞선 발급이 만료됐을 때만 바꾼다. 아니면 409 `UPLOAD_KEY_CONFLICT`. 발급 기록이 된 뒤에만 서명한다 | 업로더는 시도마다 새 주소를 받는다(upload.py). 다시 자른 클립은 앞선 주소가 만료된 뒤 올릴 수 있게, 살아 있는 주소나 기록된 클립은 덮어쓰지 않게 |
| 수명 | 업로드·조회 서명 5분(`upload-ttl`·`download-ttl`) | 업로더가 시도마다 새로 받으므로 짧게 둔다. S3 는 요청 시작 때 만료를 본다 |
| 발급 기록 | V9 로 `upload_grant` 에 `requester`·`content_type`·`size_bytes`·`sha256`·`event_id` 를 더한다 | Tier 0 토큰은 기기·사용자 칸이 비어 주체를 가를 칸이 필요하다. 내용 칸은 재발급 비교와 올라온 키 검증에 쓴다 |

## 시험

`UploadSignIntegrationTest`(MockMvc, 저장소 없이 서명 계산만): Tier 0 클립 주소의 키·`s3://` 주소·만료 300초·서명 헤더(형식·크기·Base64 해시)와 발급 기록,
같은 클립 재발급(새 서명·한 행)·내용 다름·다른 주체·기록 뒤 409, 기기 프레임·반장 사진·목적별 403, 열람·관리자 403·토큰 없음·틀린 토큰·워커 토큰 401,
목적·형식·크기·해시·식별자 400, GET 서명 주소와 버킷 미설정.

## 남은 일

- 실제 S3(또는 MinIO)로 업로드·만료 뒤 거부를 확인한다(시험은 서명 계산만 본다). 운영 버킷·인스턴스 역할 만들기는 배포 작업이다.
- 올라온 키 검증: media_clip·event_frame_ready 를 받을 때 `upload_grant` 의 키·크기·해시와 맞추고 `used_at` 을 채운다(클립 기록 수신 이슈).
- `GET /api/clips/{id}` 는 `ObjectStorage.signGet` 으로 조회 주소를 내고 `clip_access_log` 를 남긴다(클립 조회 이슈).
- 이벤트 존재 확인: 서명 때는 `event_id` 가 실제 이벤트인지 보지 않는다(이벤트 기록과 업로드 순서가 겹칠 수 있다). 클립·프레임 기록을 받을 때 이벤트와 발급을 함께 맞춘다.
- Tier 0 업로더의 서명 클라이언트(`UploadSigner` HTTP 구현)를 이 응답에 맞춰 붙인다(ai).
- 쓰이지 않은 발급(만료 뒤 `used_at` 없음)의 객체와 `upload_grant` 행은 클립 삭제 작업이 함께 정리한다. 프레임·사진은 요청마다 새 키라 행이 쌓인다(인증된 주체만 부르므로 호출 빈도 제한은 두지 않았다).
- 같은 클립을 다른 주체(Tier 0 토큰 → 기기 토큰)로 다시 받으면 409 가 이어진다. 업로더는 한 가지 토큰만 쓴다.
