# contracts

구간 계약의 정본(JSON Schema, draft 2020-12). 명세 12장, 20.4, 21.2 기준. **v2.0.0** (2.0.0: v1.3 에서 쓰지 않는 보정 출처(CHARUCO·MANUAL_PNP)·스케일 근거(MARKER·FLOOR_PLAN)와 `hmac_signature` 삭제, I5 작업 대여 방식 — S15P31A101-167·-157. 그 사이 추가: 녹화 manifest(-158), I0·I1 현장 경보 재동기화·시계 교환·엔드포인트 등록·2D 관측 요약(-155). 1.1.0: 보정 입력 선택 항목화, `MODEL_ESTIMATE` 출처 추가 — S15P31A101-148).

| 경로 | 내용 | 명세 |
|---|---|---|
| `common/defs.schema.json` | 공통 타입(좌표, 시각, 상태 enum, `input_provenance`, `reproducibility`). `calibration_source` 를 쓰는 계약(calibration, calibration_update, run_record, scene_output, human_mesh_output)은 2.0.0 | 4.1, 6.2, 12.1, 20.4 |
| `messages/envelope.schema.json` | 공통 봉투 | 12.1 |
| `messages/world_state.schema.json` | I1 `world_state` (LIVE 계약, Lead 승인 필요) | 5.2, 12.3 |
| `messages/risk_event_transition.schema.json` | I1 Tier 0 → EC2 LIVE 이벤트 전이(저널 기록, 운영자 확인 포함). 1.3.0: 확인 필요 이유 `confidence_reasons`, 해소 후 재개 `resumed` | 3.2, 6.4, 7.1, 12.3 |
| `messages/event_state_snapshot.schema.json` | I1 Tier 0 → EC2 재동기화 후 활성 이벤트 최종 상태 | 3.2 |
| `messages/health.schema.json` | I1 Tier 0 → EC2(및 I0 노트북) 1 Hz 상태·손실 3종 | 3.11, 5.1, 6.2, 12.3, 12.7 |
| `messages/observation_batch.schema.json` | Tier 0 카메라별 2D 관측 묶음(RunPod Network Volume 보관 형식, EC2 로는 `observation_summary`) | 5.1, 11.2 |
| `messages/observation_summary.schema.json` | I1 Tier 0 → EC2 2D 관측 카메라별 요약(원본은 RunPod 보관) | 11.2, 12.2 |
| `messages/calibration_update.schema.json` | I1 Tier 0 → EC2 보정 상태·버전 변경 (2.0.0 메이저: v1.3 에서 쓰지 않는 보정 출처 값 삭제) | 4.4, 4.5 |
| `messages/event_frame_ready.schema.json` | I1 Tier 0 → EC2 이벤트 프레임 업로드 완료(주소만) | 12.3 |
| `messages/tier0_endpoint.schema.json` | I1 Tier 0 → EC2 Pod 엔드포인트 등록(공개 IP·포트·공개키 지문). 노트북 조회 응답도 이 형식 | 3.12, 12.6, 12.7 |
| `messages/config_bundle.schema.json` | I1 EC2 → Tier 0 구성 묶음 발행 | 3.2, 11.1 |
| `messages/config_ack.schema.json` | I1 Tier 0 → EC2 구성 묶음 적용·거부 | 3.2 |
| `messages/event_ack.schema.json` | I1 EC2 → Tier 0 운영자 확인(관리자·반장) | 3.2 |
| `messages/journal_ack.schema.json` | I1 EC2 → Tier 0 저널 전송 확인(acked_offset)·빈 구간 재요청 | 3.2 저널 계약 |
| `messages/frame_header.schema.json` | I0 노트북 → Tier 0 프레임 헤더(봉투 없음) | 3.9, 12.7 |
| `messages/codec_info.schema.json` | I0 노트북 → Tier 0 연결 첫 기록: 코덱·SPS/PPS·PTS timebase(봉투 없음) | 12.7 |
| `messages/local_alert.schema.json` | I0 Tier 0 → 노트북 현장 경보 | 3.10, 7.3 |
| `messages/alarm_stop.schema.json` | I0 Tier 0 → 노트북 반복 소리 정지(누가 확인했는지 포함) | 3.2, 3.10 |
| `messages/site_ack.schema.json` | I0 노트북 → Tier 0 현장 확인 버튼 | 3.10 |
| `messages/clock_sync_request.schema.json` | I0 Tier 0 → 노트북 시계 차이 교환 요청(t1) | 3.9, 6.1 |
| `messages/clock_sync_response.schema.json` | I0 노트북 → Tier 0 시계 차이 교환 응답(t1·t2·t3) | 3.9, 6.1 |
| `messages/laptop_heartbeat.schema.json` | 노트북 → Tier 0(I0)·EC2(HTTPS) 1초 하트비트, Tier 0 연결 상태 | 3.2, 6.5 |
| `messages/risk_event.schema.json` | I2 EC2 → 뷰어 `/topic/events`. 1.3.0: `confidence_reasons`, 재개 횟수 `resume_count` | 6.4, 7.1, 12.4 |
| `messages/risk_alert.schema.json` | I3 EC2 → 반장 PWA Web Push | 10.1, 12.4 |
| `messages/job_lease_request.schema.json` | I5 분석 워커 → EC2 작업 대여 요청(HTTPS long-poll, 워커 토큰). I5 전송·인증 규칙 | 12.2, 12.5, 14 |
| `messages/job_request.schema.json` | I5 EC2 → 분석 워커 대여 응답: 작업 하나와 lease_id·만료 시각, 재할당 규칙 (2.0.0 메이저) | 12.5 |
| `messages/job_heartbeat.schema.json` | I5 분석 워커 → EC2 대여 연장 | 12.5 |
| `messages/job_callback.schema.json` | I5 분석 워커 → EC2 결과·실패 제출, 중복·만료 대여 제출 처리 (2.0.0 메이저, HMAC 서명 없음) | 12.2, 12.5 |
| `messages/job_lease_status.schema.json` | I5 EC2 → 분석 워커 하트비트·제출 응답(ACTIVE·ACCEPTED·DUPLICATE·REJECTED) | 12.5 |
| `messages/vlm_rationale.schema.json` | I5 `vlm_rationale` 입력·결과 정의(메시지는 job_request·job_callback) | 5.4, 12.5 |
| `providers/calibration.schema.json` | CalibrationProvider 출력 (2.0.0 메이저: v1.3 에서 쓰지 않는 보정 출처·스케일 근거 값 삭제) | 4장, 20.4 |
| `providers/world_tracks.schema.json` | WorldTrackProvider 출력 (JSONL 한 줄) | 5.2, 20.4 |
| `experiments/scene_output.schema.json` | EXP-01 정적 장면 결과 (2.0.0 메이저: 공통 보정 출처 값 삭제) | 5.3, 20.2 |
| `experiments/human_mesh_output.schema.json` | EXP-02 `avatar_asset` / `pose_frame` (2.0.0 메이저: 공통 보정 출처 값 삭제) | 5.6, 20.3, 20.4 |
| `experiments/run_record.schema.json` | 실험 Run 기록 (2.0.0 메이저: 공통 보정 출처 값 삭제) | 20.5 |
| `data/recording_manifest.schema.json` | 4대 동기화 녹화 세션 manifest(파일·해시·프레임 수·시각 범위·카메라 설정·동의·보관·저장 위치). 프레임 사이드카 한 줄은 `$defs/frame_line`(`frame_header` 재사용). 메시지·Provider 출력이 아닌 저장 문서라 `data/`에 둔다(설명은 `docs/data/`) | 11.5, 12.7, 15.3, 21.1 |

I4(워치, 확장)와 I2 `/topic/world`·`/topic/health` 토픽 형식은 아직 없다. I5 `vlm_rationale` 외 작업 유형의 `input`·`result` 형식은 각 작업 티켓에서 정한다.

## 메시지 규칙

- I0 영상 기록(`frame_header`, `codec_info`)을 뺀 모든 메시지는 공통 봉투(`envelope`)를 쓴다. 메시지 스키마는 `allOf`로 봉투를 가져오고 `msg_type`을 고정한다.
- 수신 측은 모르는 필드를 무시하므로 `payload`와 주요 항목(트랙, 카메라, 이벤트) 객체는 열어 둔다. 값 객체(확인 기록, 서명, 집계, VLM 결과 등)는 `additionalProperties: false`로 닫는다.
- 송신 주체 `source_id`: Tier 0 는 `tier0-01`(이벤트의 `gateway_id`와 같다), 노트북은 `laptop-01`, EC2 는 `ec2`, 분석 워커는 `worker-01`(워커 토큰에 묶인 워커 ID).
- `event_id`는 UUIDv7, `msg_id`·`ack_id`는 UUID(버전 무관)다.

## I0 전송 규칙 (스키마 밖)

명세 12.7, 3.12(수정 6). 연결 자체는 스키마로 검증하지 않으므로 여기 적는다.

- **영상:** 카메라마다 독립 연결을 연다(한 카메라의 지연이 다른 카메라를 막지 않도록). RunPod Pod 의 공개 Direct TCP 포트로 접속하고, TLS 는 Tier 0 서비스가 직접 종료한다. SSH 터널이나 Tailscale 로 보내지 않는다. 접속 시 노트북 기기 토큰으로 인증한다.
- **기록 순서:** 연결(또는 새 `stream_epoch`) 직후 `codec_info` 한 번, 그 뒤 키프레임부터 `frame_header` + 압축 프레임(H.264 접근 단위)을 길이 접두 방식으로 싣는다. 헤더와 프레임은 같은 기록에 싣는다. 코덱 정보와 키프레임이 모두 오기 전까지 그 카메라는 관측 없음이다.
- **누락:** `frame_sequence` 빈 구간은 `gateway_to_runpod_loss`로 기록하고 재요청하지 않는다. 카메라 → 노트북 끊김(`camera_to_gateway_gap`)과 정책상 건너뛴 프레임(`skipped_by_policy`)은 따로 센다.
- **제어:** `local_alert`, `alarm_stop`, `health`(아래로), `site_ack`, `laptop_heartbeat`(위로)는 같은 기기 토큰으로 인증한 WebSocket 으로 주고받는다.
- **시계 차이·엔드포인트:** 시계 차이 교환(`clock_sync_request`·`clock_sync_response`)도 같은 제어 WebSocket 으로 주고받는다. 노트북은 `GET /api/endpoints/tier0`(기기 토큰)로 받은 `tier0_endpoint` 로 접속하고, 공개키 지문이 미리 고정한 값과 다르면 접속하지 않는다.
- **예비 경로:** 막힌 망에서는 RunPod HTTPS 프록시(443) WebSocket 을 쓰고 토큰 인증이 필수다.
- 노트북 → EC2 `laptop_heartbeat`는 HTTPS(기기 토큰)로 1초마다 보낸다.

## 스키마로 막는 불변 규칙

- 지지 표면이 VALID가 아니거나 `z_validity`가 UNAVAILABLE이면 `ground_anchor_xyz`는 null (Z=0 대체 금지)
- I1 전이·현장 경보·푸시(`risk_event_transition`, `local_alert`, `risk_alert`)의 `source`는 LIVE만. 현장 경보·푸시는 CRITICAL·HIGH만 (7.3)
- 활성 현장 경보 목록(`health.active_site_alerts`)은 LIVE·CRITICAL·HIGH 만, `(event_id, event_sequence)`·`acknowledged` 필수. 시계 차이는 불확실성과 함께, 모르면 null. 엔드포인트 등록은 공개키 지문과 대칭 매핑(70000 이상) 포트, 영상 포트는 카메라별, 비밀 값 불가(payload 닫힘). `observation_summary` 에는 2D 원본(frames·detections·bbox·keypoint 등)을 싣지 않는다(payload·카메라 집계 닫힘)
- `event_id`는 UUIDv7. 해소(RESOLVED)는 `verification`이 VERIFIED일 때만, 재동기화 스냅샷에는 활성 이벤트만
- 운영자 확인은 누가(`actor_role`·`actor_id`)·어디서(`channel`) 했는지 필수. 현장 확인 버튼은 `site_ack`로만, 관제 화면·PWA 확인은 `event_ack`로만
- VLM `proposed_severity`는 `severity`보다 높은 등급이거나 null(하향·같은 등급 불가). VLM 결과에는 해소·끄기 필드가 없다. 푸시 근거 한 줄은 규칙 근거(`summary_source: RULE`)만
- VLM 입력 프레임은 얼굴 흐림·제외(`face_policy`) 필수. I5 대여 응답·하트비트·제출은 `lease_id` 필수, 대여 응답과 ACTIVE 응답은 만료 시각 필수, REJECTED 는 이유 필수
- I0 프레임 헤더는 (`camera_id`, `stream_epoch`, `frame_sequence`)와 촬영 시각 계약(`timestamp_source`, `timestamp_uncertainty_ms`) 필수. 손실 3종은 섞지 않는다
- 이미지는 메시지에 넣지 않고 주소로만. 이벤트·VLM 프레임 주소(`frame_uri`)는 객체 저장소 주소 `s3://<bucket>/<key>`(`object_uri`)만 받는다. `data:` URI·Base64 본문·임시 서명 HTTPS 주소 불가
- 이벤트·VLM 프레임(`event_frame_ready`, `vlm_rationale` 입력)도 촬영 시각 계약(`estimated_capture_time_ms`, `timestamp_source`, `timestamp_uncertainty_ms`) 필수 (3.9)
- `DATASET_GT`·`SIM`·`MODEL_ESTIMATE` 보정은 CALIBRATED 불가, CALIBRATED는 독립 검증점 결과 필수
- 이미지만 넣어 모델이 카메라를 추정하는 Run은 `input_provenance.calibration`을 생략한다. 이전 Run의 추정 카메라를 다시 넣으면 출처는 `MODEL_ESTIMATE`다
- 정답·시뮬레이션 입력(`is_upper_bound: true`) 결과는 COMMITTED Scene 불가
- HMR 포즈의 root 출처는 `TIER0`만 허용, 외형 텍스처는 `face_policy`(EXCLUDED/BLURRED)와 `consent_ref` 필수
- 좌표계 선언은 World(m, 오른손, +Z)만 허용
- 녹화 manifest: 실제 카메라(RTSP) 녹화는 동의·보관 기한(`consent`) 필수, 사람이 찍히면 동의서 ID·범위 필수(11.5). RTSP 주소에 계정 불가, 저장 위치는 `volume://`·`s3://`만, 파일 경로는 세션 디렉터리 기준 상대 경로만. `input_kinds` 는 카메라 입력 종류(`cameras[].source.kind`)를 모두 담고, RTSP 가 아닌 입력이 있으면 `is_test_input: true`(그때만). 세그먼트 무결성 검사는 모두 true

스키마로 표현할 수 없는 규칙(R의 직교성, 관절 배열 길이 일치, `is_upper_bound`와 출처의 일치, 봉투 `source_id`와 `gateway_id`의 일치, `event_sequence`·`journal_offset` 단조 증가, `codec_info`와 `frame_header`의 `stream_epoch` 일치, I5 대여의 `lease_expires_at > leased_at`·`attempt ≤ max_attempts`, `health.active_site_alerts` 의 `event_id` 유일, 시계 교환의 `t3_ms ≥ t2_ms`·`uncertainty_ms ≥ round_trip_ms/2`, `observation_summary` 의 `window_end_ms > window_start_ms`, `tier0_endpoint` 의 VIDEO `camera_id` 유일과 `gateway_id`·`source_id` 일치, 녹화 manifest 시각 범위의 from ≤ to·`camera_id` 중복 등)은 각 모듈의 계약 시험에서 검사한다(명세 21.3).

## 검증

```
uv run contracts/scripts/validate_examples.py
```

- `examples/<schema>[.<variant>].json`은 통과해야 한다.
- `examples/invalid/<schema>.<case>.json`은 실패해야 한다. 불변 규칙을 하나 추가하면 그 위반 예시도 하나 추가한다.
- 스키마를 바꾸면 예시도 같이 바꾼다. 버전 규칙: 필드 추가는 마이너, 삭제·의미 변경은 메이저(12.1).
