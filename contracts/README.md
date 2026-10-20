# contracts

구간 계약의 정본(JSON Schema, draft 2020-12). 명세 12장, 20.4, 21.2 기준. **v5.2.0** (5.2.0: 녹화 manifest 1.2.0 — `consent.purpose`(선택, `dev_test`·`dataset`) 추가, 없으면 `dataset` 과 같아 기존 manifest 는 그대로 통과(명세 11.5, -204). 5.1.0: World ID 분할(명세 5.2, Lead 승인 2026-10-20) — I1 `world_state` 2.1.0 트랙의 분할 출처 `split_from`(선택, 분할로 생긴 새 ID 에만 원래 ID, 그 Track 이 살아 있는 동안 같은 값, 2.0.x 이하 문서는 싣지 않음)과 `identity_revision` 설명 보강(병합·분할 모두 교정 차수를 올림) — S15P31A101-201. 5.0.0: 운영자 종료(명세 6.4, Lead 승인 2026-10-20) — I1 `risk_event_transition` 2.0.0·I2 `risk_event` 2.0.0 의미 변경(메이저): 선택 필드 `resolution`(공통 `operator_resolution`: kind OPERATOR_CLOSED·close_id·관리자·요청 시각·사유)이 있는 RESOLVED 는 관측 해소가 아니므로 VERIFIED·`evidence_ref` 를 요구하지 않는다(조건 완화라 메이저, 수신 측은 RESOLVED 를 관측 해소로 읽기 전에 `resolution` 을 확인). 1.x 문서는 `resolution` 없이 1.x 규칙으로 검사. I1 `event_close`(EC2 → Tier 0 관리자의 구역 대상 이벤트 종료 명령)·`event_close_result`(Tier 0 → EC2 수락·중복·거부) 신설(1.0.0, 1.1.0 거부 사유 `JOURNAL_UNAVAILABLE` 재시도 가능) — S15P31A101-200. 4.0.0: I5 `job_request` 3.0.0 의미 변경(메이저) — `attempt` 는 반납을 세지 않는 시도 번호, `max_attempts` 는 최대 시도 수(같은 `attempt` 가 여러 대여에 되풀이될 수 있음, 워커는 2.x `job_request` 를 받지 않음), `job_callback` 2.1.0 반납 `error.consume_attempt: false`(Tier 0 우선으로 시작하지 않았거나 멈춘 작업을 시도 횟수 없이 돌려줌, `job_lease_status` 설명 보강), I1 `health` 2.1.0 `tier0_warnings` code `LIVE_MARKER_WRITE_FAILED`(Lead 승인 2026-10-19), `run_record` 2.1.0 `experiment_id` `VAL-01`(운영 경로 검증 Run) — S15P31A101-197. 3.0.0: I1 `world_state` 2.0.0·`health` 2.0.0 의미 변경(메이저) — `head_z_validity` 가 UNCERTAIN 이면 그 UNCERTAIN 값을 `head_z`·`head_z_sigma` 에 싣는다(1.x 는 null, 값이 있어도 유효성을 반드시 확인), `health.pipeline_lag_ms` null(T_lag 모름)·하한 `pipeline_lag_lower_bound_ms`, Tier 0 운영 경고 `tier0_warnings`, 카메라 동기화 근거 `cameras[].sync_valid`(null = 근거 없음) — S15P31A101-179. 2.3.0: `world_state` 트랙의 머리 높이 유효성 `head_z_validity`(값은 VALID 일 때만), `event_state_snapshot.journal_offset` null(아직 저널 기록 없음), `laptop_heartbeat` 처리 정체 해석 설명 보강 — S15P31A101-172. 2.2.0: `world_state` 트랙의 지지 표면 출처 `surface_source`·병합 별칭 `aliases`, `health.ec2_world_state_drops`(Tier 0 → EC2 world_state 송신 버림 누적) — S15P31A101-170. 2.1.0: `world_state.config_version`, 판정이 아닌 전이의 `evidence_ref` null, health 노트북 버림 집계, 이벤트 클립 기록 `media_clip`·`media_clip_retention` — S15P31A101-169. 2.0.0: v1.3 에서 쓰지 않는 보정 출처(CHARUCO·MANUAL_PNP)·스케일 근거(MARKER·FLOOR_PLAN)와 `hmac_signature` 삭제, I5 작업 대여 방식 — S15P31A101-167·-157. 그 사이 추가: 녹화 manifest(-158), I0·I1 현장 경보 재동기화·시계 교환·엔드포인트 등록·2D 관측 요약(-155). 1.1.0: 보정 입력 선택 항목화, `MODEL_ESTIMATE` 출처 추가 — S15P31A101-148).

| 경로 | 내용 | 명세 |
|---|---|---|
| `common/defs.schema.json` | 공통 타입(좌표, 시각, 상태 enum, `input_provenance`, `reproducibility`). `calibration_source` 를 쓰는 계약(calibration, calibration_update, run_record, scene_output, human_mesh_output)은 2.0.0 | 4.1, 6.2, 12.1, 20.4 |
| `messages/envelope.schema.json` | 공통 봉투 | 12.1 |
| `messages/world_state.schema.json` | I1 `world_state` (LIVE 계약, Lead 승인 필요). 1.1.0: 입력 출처 `input_provenance`(선택), `position_xy` 관측 공백 때 시각 기준 설명 보강. 1.2.0: 구성 묶음 버전 `config_version`(선택, -169). 1.3.0: 트랙의 지지 표면 출처 `surface_source`(observed/inferred, 접지점이 있을 때만)·병합 별칭 `aliases`(선택, -170). 1.4.0: 트랙의 머리 높이 유효성 `head_z_validity`(VALID/UNCERTAIN/UNAVAILABLE, 선택, -172. `head_z` 값은 VALID 일 때만). 2.0.0(메이저, -179): UNCERTAIN 이면 그 UNCERTAIN 값을 `head_z`·`head_z_sigma` 에 싣는다. 값이 있어도 `head_z_validity` 를 확인하고, UNCERTAIN 값은 확정 근거로 쓰지 않는다. 1.x 문서는 1.x 규칙(UNCERTAIN ↔ null)으로 검사. 2.1.0(-201): 분할로 생긴 새 ID 의 원래 ID `split_from`(선택, 살아 있는 동안 같은 값, 자기 ID·별칭 불가는 송신 측 시험), `identity_revision` 은 분할에도 오른다 | 3.2, 5.2, 6.1, 12.3, 20.4 |
| `messages/risk_event_transition.schema.json` | I1 Tier 0 → EC2 LIVE 이벤트 전이(저널 기록, 운영자 확인 포함). 1.3.0: 확인 필요 이유 `confidence_reasons`, 해소 후 재개 `resumed`. 1.4.0: 판정이 아닌 전이(확인·복원·ID 교정)의 `evidence_ref` null 허용(-169). 2.0.0(메이저, -200): 운영자 종료 해소 `resolution`(RESOLVED 에만, 있으면 VERIFIED·`evidence_ref` 필수 아님). 1.x 문서는 `resolution` 없음 | 3.2, 6.4, 7.1, 12.3 |
| `messages/event_state_snapshot.schema.json` | I1 Tier 0 → EC2 재동기화 후 활성 이벤트 최종 상태. 1.3.0: `journal_offset` null = 아직 저널 기록이 없음(0 과 다름, 그때 활성 이벤트 없음, -172) | 3.2 |
| `messages/health.schema.json` | I1 Tier 0 → EC2(및 I0 노트북) 1 Hz 상태·손실 3종. 1.4.0: 카메라별 노트북 송신 큐 버림 `laptop_skipped`·구분 불가 `unattributed_gap`(선택, -169). 1.5.0: Tier 0 → EC2 world_state 송신 버림 누적 `ec2_world_state_drops`(선택, -170). 2.0.0(메이저, -179): `pipeline_lag_ms` null = T_lag 모름(0·하한으로 채우지 않음, 그때 UNSAFE_UNKNOWN)·하한 `pipeline_lag_lower_bound_ms`, Tier 0 운영 경고 `tier0_warnings`(닫힌 code + 처음 확인 시각), 카메라 동기화 근거 `cameras[].sync_valid`(true 는 `sync_offset_ms` 필수, null = 근거 없음). 2.1.0(-197): `tier0_warnings` code `LIVE_MARKER_WRITE_FAILED`(GPU 상태 표시 파일 쓰기 실패, 정보 경고) | 3.11, 5.1, 6.2, 12.3, 12.7 |
| `messages/observation_batch.schema.json` | Tier 0 카메라별 2D 관측 묶음(RunPod Network Volume 보관 형식, EC2 로는 `observation_summary`) | 5.1, 11.2 |
| `messages/observation_summary.schema.json` | I1 Tier 0 → EC2 2D 관측 카메라별 요약(원본은 RunPod 보관) | 11.2, 12.2 |
| `messages/calibration_update.schema.json` | I1 Tier 0 → EC2 보정 상태·버전 변경 (2.0.0 메이저: v1.3 에서 쓰지 않는 보정 출처 값 삭제) | 4.4, 4.5 |
| `messages/media_clip.schema.json` | I1 Tier 0 → EC2 이벤트 클립 업로드 기록(객체 키·구간·해시·크기·얼굴 흐림 여부·삭제 예정일·보존 표시). 1.0.0 신설(-169) | 5.1, 11.3, 11.5, 12.3 |
| `messages/media_clip_retention.schema.json` | I1 Tier 0 → EC2 보낸 클립에 보존 표시를 붙이는 추가 기록(CRITICAL 자동 보존). 1.0.0 신설(-169) | 11.5, 12.3 |
| `messages/event_frame_ready.schema.json` | I1 Tier 0 → EC2 이벤트 프레임 업로드 완료(주소만) | 12.3 |
| `messages/tier0_endpoint.schema.json` | I1 Tier 0 → EC2 Pod 엔드포인트 등록(공개 IP·포트·공개키 지문). 노트북 조회 응답도 이 형식 | 3.12, 12.6, 12.7 |
| `messages/config_bundle.schema.json` | I1 EC2 → Tier 0 구성 묶음 발행 | 3.2, 11.1 |
| `messages/config_ack.schema.json` | I1 Tier 0 → EC2 구성 묶음 적용·거부 | 3.2 |
| `messages/event_ack.schema.json` | I1 EC2 → Tier 0 운영자 확인(관리자·반장) | 3.2 |
| `messages/event_close.schema.json` | I1 EC2 → Tier 0 관제 화면 관리자의 구역 대상 이벤트 종료(멱등 키 `close_id`, 누가·왜). 1.0.0 신설(-200) | 6.4, 12.2 |
| `messages/event_close_result.schema.json` | I1 Tier 0 → EC2 `event_close` 결과(ACCEPTED·DUPLICATE·REJECTED와 거부 사유). 1.0.0 신설, 1.1.0 거부 사유 `JOURNAL_UNAVAILABLE`(저널에 넣지 못함, 이벤트 그대로·재시도 가능)(-200) | 6.4, 12.3 |
| `messages/journal_ack.schema.json` | I1 EC2 → Tier 0 저널 전송 확인(acked_offset)·빈 구간 재요청 | 3.2 저널 계약 |
| `messages/frame_header.schema.json` | I0 노트북 → Tier 0 프레임 헤더(봉투 없음) | 3.9, 12.7 |
| `messages/codec_info.schema.json` | I0 노트북 → Tier 0 연결 첫 기록: 코덱·SPS/PPS·PTS timebase(봉투 없음) | 12.7 |
| `messages/local_alert.schema.json` | I0 Tier 0 → 노트북 현장 경보 | 3.10, 7.3 |
| `messages/alarm_stop.schema.json` | I0 Tier 0 → 노트북 반복 소리 정지(누가 확인했는지 포함) | 3.2, 3.10 |
| `messages/site_ack.schema.json` | I0 노트북 → Tier 0 현장 확인 버튼 | 3.10 |
| `messages/clock_sync_request.schema.json` | I0 Tier 0 → 노트북 시계 차이 교환 요청(t1) | 3.9, 6.1 |
| `messages/clock_sync_response.schema.json` | I0 노트북 → Tier 0 시계 차이 교환 응답(t1·t2·t3) | 3.9, 6.1 |
| `messages/laptop_heartbeat.schema.json` | 노트북 → Tier 0(I0)·EC2(HTTPS) 1초 하트비트, Tier 0 연결 상태. 설명 보강(-172, 형식·버전 그대로): `tier0.control=CONNECTED` + `site_display=CONTROL_HALTED` 는 관제 정지(처리 정체·판정 불가·영상 단절)로 읽는다 | 3.2, 3.10, 6.5 |
| `messages/risk_event.schema.json` | I2 EC2 → 뷰어 `/topic/events`. 1.3.0: `confidence_reasons`, 재개 횟수 `resume_count`. 2.0.0(메이저, -200): 운영자 종료 해소 `resolution`(LIVE RESOLVED 에만, 있으면 VERIFIED 필수 아님). 1.x 문서는 `resolution` 없음 | 6.4, 7.1, 12.4 |
| `messages/risk_alert.schema.json` | I3 EC2 → 반장 PWA Web Push | 10.1, 12.4 |
| `messages/job_lease_request.schema.json` | I5 분석 워커 → EC2 작업 대여 요청(HTTPS long-poll, 워커 토큰). I5 전송·인증 규칙 | 12.2, 12.5, 14 |
| `messages/job_request.schema.json` | I5 EC2 → 분석 워커 대여 응답: 작업 하나와 lease_id·만료 시각, 재할당 규칙 (2.0.0 메이저). 3.0.0(메이저, -197): `attempt` = 반납을 세지 않는 시도 번호, `max_attempts` = 최대 시도 수(같은 `attempt` 의 대여가 여럿일 수 있음) | 12.5 |
| `messages/job_heartbeat.schema.json` | I5 분석 워커 → EC2 대여 연장 | 12.5 |
| `messages/job_callback.schema.json` | I5 분석 워커 → EC2 결과·실패 제출, 중복·만료 대여 제출 처리 (2.0.0 메이저, HMAC 서명 없음). 2.1.0(-197): 반납 `error.consume_attempt: false`(Tier 0 우선 사유만, 시도 횟수 그대로 다시 대기열) | 12.2, 12.5 |
| `messages/job_lease_status.schema.json` | I5 EC2 → 분석 워커 하트비트·제출 응답(ACTIVE·ACCEPTED·DUPLICATE·REJECTED) | 12.5 |
| `messages/vlm_rationale.schema.json` | I5 `vlm_rationale` 입력·결과 정의(메시지는 job_request·job_callback) | 5.4, 12.5 |
| `providers/calibration.schema.json` | CalibrationProvider 출력 (2.0.0 메이저: v1.3 에서 쓰지 않는 보정 출처·스케일 근거 값 삭제) | 4장, 20.4 |
| `providers/world_tracks.schema.json` | WorldTrackProvider 출력 (JSONL 한 줄) | 5.2, 20.4 |
| `experiments/scene_output.schema.json` | EXP-01 정적 장면 결과 (2.0.0 메이저: 공통 보정 출처 값 삭제) | 5.3, 20.2 |
| `experiments/human_mesh_output.schema.json` | EXP-02 `avatar_asset` / `pose_frame` (2.0.0 메이저: 공통 보정 출처 값 삭제) | 5.6, 20.3, 20.4 |
| `experiments/run_record.schema.json` | 실험 Run 기록 (2.0.0 메이저: 공통 보정 출처 값 삭제). 2.1.0(-197): `experiment_id` `VAL-01`(운영 경로 검증 Run, `e_gate` 불가) | 20.5 |
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
- `ground_anchor_xyz` 가 null 이면 `surface_source`(지지 표면 출처)를 싣지 않는다. `aliases` 는 중복 없이, 별칭이 없으면 싣지 않는다(빈 배열 불가). `health.ec2_world_state_drops` 는 세기 시작한 시각(`since_ms`)과 함께, 값 객체는 닫힘
- `head_z_validity` 가 VALID 면 `head_z` 는 수, UNAVAILABLE 이면 `head_z`·`head_z_sigma` 는 null. UNCERTAIN 은 `world_state` 2.0.0 이상이면 `head_z` 가 수(UNCERTAIN 값), 1.x 문서면 `head_z`·`head_z_sigma` 가 null. `head_z_validity` 가 없는 이전 버전 문서에는 적용하지 않는다
- `health.pipeline_lag_ms` 가 null(T_lag 모름)이면 `system_health` 는 UNSAFE_UNKNOWN, 하한 `pipeline_lag_lower_bound_ms` 는 `pipeline_lag_ms` 가 null 일 때만. 운영 경고 `tier0_warnings` 는 닫힌 code 와 처음 확인 시각(값 객체 닫힘). 카메라 `sync_valid` 가 true 면 `sync_offset_ms` 는 수, false 면 status 가 OK 아님, status SYNC_INVALID 면 `sync_valid` 는 false
- I1 전이·현장 경보·푸시(`risk_event_transition`, `local_alert`, `risk_alert`)의 `source`는 LIVE만. 현장 경보·푸시는 CRITICAL·HIGH만 (7.3)
- 활성 현장 경보 목록(`health.active_site_alerts`)은 LIVE·CRITICAL·HIGH 만, `(event_id, event_sequence)`·`acknowledged` 필수. 시계 차이는 불확실성과 함께, 모르면 null. 엔드포인트 등록은 공개키 지문과 대칭 매핑(70000 이상) 포트, 영상 포트는 카메라별, 비밀 값 불가(payload 닫힘). `observation_summary` 에는 2D 원본(frames·detections·bbox·keypoint 등)을 싣지 않는다(payload·카메라 집계 닫힘)
- `event_id`는 UUIDv7. 관측 해소(RESOLVED, `resolution` 없음)는 `verification`이 VERIFIED일 때만(운영자 종료 `resolution` 은 2.0.0 부터 RESOLVED 에만, 검증 상태·근거를 요구하지 않음), 재동기화 스냅샷에는 활성 이벤트만. 스냅샷 `journal_offset` 이 null(아직 저널 기록 없음)이면 활성 이벤트는 빈 배열
- I1 전이의 `evidence_ref` 키는 늘 싣는다. RAISED·관측 RESOLVED·재개(`resumed`)는 null 불가, 판정이 아닌 전이(확인·복원·ID 교정)는 근거를 모르면 null, 운영자 종료(`resolution`)도 판정이 아닌 전이처럼 직전 판정 근거를 싣고 모르면 null
- 이벤트 클립(`media_clip`)은 LIVE·CRITICAL·HIGH 만, 영상은 객체 키로만(스킴·쿼리 있는 주소 불가, payload 닫힘), 삭제 예정일 필수, 흐림 없음(NONE)이면 `redactor_version` null·흐림이면 필수. 보존 표시 추가 기록(`media_clip_retention`)은 붙이기만(true), CRITICAL 자동 보존은 CRITICAL 만
- 운영자 확인은 누가(`actor_role`·`actor_id`)·어디서(`channel`) 했는지 필수. 현장 확인 버튼은 `site_ack`로만, 관제 화면·PWA 확인은 `event_ack`로만
- 운영자 종료(`event_close`)는 관제 화면 관리자(`CONTROL_ROOM`·`ADMIN`)만, 멱등 키 `close_id` 와 비지 않은 사유 필수(값 객체 닫힘). 결과의 거부 사유는 REJECTED 에만, ACCEPTED 는 종료 전이의 `event_sequence` 필수
- VLM `proposed_severity`는 `severity`보다 높은 등급이거나 null(하향·같은 등급 불가). VLM 결과에는 해소·끄기 필드가 없다. 푸시 근거 한 줄은 규칙 근거(`summary_source: RULE`)만
- VLM 입력 프레임은 얼굴 흐림·제외(`face_policy`) 필수. I5 대여 응답·하트비트·제출은 `lease_id` 필수, 대여 응답과 ACTIVE 응답은 만료 시각 필수, REJECTED 는 이유 필수. 반납(`job_callback` `error.consume_attempt: false`)은 FAILED·retryable true·code `TIER0_PRIORITY`/`LIVE_EXP_BLOCKED`/`LIVE_PREEMPTED` 만
- I0 프레임 헤더는 (`camera_id`, `stream_epoch`, `frame_sequence`)와 촬영 시각 계약(`timestamp_source`, `timestamp_uncertainty_ms`) 필수. 손실 3종은 섞지 않는다
- 이미지는 메시지에 넣지 않고 주소로만. 이벤트·VLM 프레임 주소(`frame_uri`)는 객체 저장소 주소 `s3://<bucket>/<key>`(`object_uri`)만 받는다. `data:` URI·Base64 본문·임시 서명 HTTPS 주소 불가
- 이벤트·VLM 프레임(`event_frame_ready`, `vlm_rationale` 입력)도 촬영 시각 계약(`estimated_capture_time_ms`, `timestamp_source`, `timestamp_uncertainty_ms`) 필수 (3.9)
- `DATASET_GT`·`SIM`·`MODEL_ESTIMATE` 보정은 CALIBRATED 불가, CALIBRATED는 독립 검증점 결과 필수
- 이미지만 넣어 모델이 카메라를 추정하는 Run은 `input_provenance.calibration`을 생략한다. 이전 Run의 추정 카메라를 다시 넣으면 출처는 `MODEL_ESTIMATE`다
- 정답·시뮬레이션 입력(`is_upper_bound: true`) 결과는 COMMITTED Scene 불가. 운영 경로 검증 Run(`experiment_id` `VAL-01`)은 `e_gate` 를 싣지 않는다
- HMR 포즈의 root 출처는 `TIER0`만 허용, 외형 텍스처는 `face_policy`(EXCLUDED/BLURRED)와 `consent_ref` 필수
- 좌표계 선언은 World(m, 오른손, +Z)만 허용
- 녹화 manifest: 실제 카메라(RTSP) 녹화는 동의·보관 기한(`consent`) 필수, 사람이 찍히면 동의서 ID·범위 필수(11.5). RTSP 주소에 계정 불가, 저장 위치는 `volume://`·`s3://`만, 파일 경로는 세션 디렉터리 기준 상대 경로만. `input_kinds` 는 카메라 입력 종류(`cameras[].source.kind`)를 모두 담고, RTSP 가 아닌 입력이 있으면 `is_test_input: true`(그때만). 세그먼트 무결성 검사는 모두 true

스키마로 표현할 수 없는 규칙(R의 직교성, 관절 배열 길이 일치, `is_upper_bound`와 출처의 일치, 봉투 `source_id`와 `gateway_id`의 일치, `event_sequence`·`journal_offset` 단조 증가, `codec_info`와 `frame_header`의 `stream_epoch` 일치, I5 대여의 `lease_expires_at > leased_at`·`attempt ≤ max_attempts`·반납(`consume_attempt: false`) 뒤 다음 대여의 같은 `attempt`, `health.active_site_alerts` 의 `event_id` 유일, 시계 교환의 `t3_ms ≥ t2_ms`·`uncertainty_ms ≥ round_trip_ms/2`, `observation_summary` 의 `window_end_ms > window_start_ms`, `tier0_endpoint` 의 VIDEO `camera_id` 유일과 `gateway_id`·`source_id` 일치, 녹화 manifest 시각 범위의 from ≤ to·`camera_id` 중복, 판정 UPDATED(등급·수치·구역·검증 상태 변경)의 `evidence_ref` 존재, `media_clip` 의 `clip_end_capture_ms ≥ clip_start_capture_ms`·`delete_after_ms = created_at_ms + 보관 기간`·`gateway_id` 와 `source_id` 일치, `world_state` 1.3.0 이상에서 접지점이 있으면 `surface_source` 존재·`aliases` 에 자기 `world_id` 없음·한 번 실린 별칭이 Track 이 살아 있는 동안 빠지지 않음, `ec2_world_state_drops` 의 같은 `since_ms` 안 단조 증가, `world_state` 1.4.0 이상 송신 측의 `head_z_validity` 늘 싣기와 관절 게이트 결과 일치, 2.0.0 의 UNCERTAIN `head_z` 가 관측(observed) 귀·눈에서만 나옴, `health.tier0_warnings` 의 code 유일·원인 상태가 풀리면 빠짐, `sync_valid` true 는 동기화 측정 근거가 있을 때만, 스냅샷 `journal_offset` null 은 저널에 기록이 한 번도 없을 때만, 하트비트 `tier0.control` 과 `site_display` 를 함께 읽기 등)은 각 모듈의 계약 시험에서 검사한다(명세 21.3).

## 검증

```
uv run contracts/scripts/validate_examples.py
```

- `examples/<schema>[.<variant>].json`은 통과해야 한다.
- `examples/invalid/<schema>.<case>.json`은 실패해야 한다. 불변 규칙을 하나 추가하면 그 위반 예시도 하나 추가한다.
- 스키마를 바꾸면 예시도 같이 바꾼다. 버전 규칙: 필드 추가는 마이너, 삭제·의미 변경은 메이저(12.1).
