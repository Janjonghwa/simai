# 12. 인터페이스 명세

I0의 영상 프레임(바이너리, 12.7)을 제외한 모든 구간은 공통 봉투를 쓰고, 이미지는 메시지에 넣지 않고 객체 저장소 주소로 주고받는다. 아래 필드는 v1.0 기준 예시이며 필드 정의의 정본은 저장소 `contracts/`의 JSON Schema 파일이다(21.2). 아직 스키마가 없는 메시지는 이 절의 예시가 기준이다. v1.3 에서 바뀐 규칙 중 현장 경보 목록(`health.active_site_alerts`), 시계 차이 교환(`clock_sync_request`·`clock_sync_response`, `health.laptop_clock_offset`), 엔드포인트 등록(`tier0_endpoint`), 2D 관측 요약(`observation_summary`), I5 작업 대여(`job_lease_request`·`job_request`·`job_heartbeat`·`job_callback`·`job_lease_status`), 확인 필요 이유(`confidence_reasons`), 이벤트 재개(`resumed`·`resume_count`), 확인 대상(`site_ack`·`event_ack`·`alarm_stop` 의 `event_sequence`), `world_state.config_version`, 판정이 아닌 전이의 근거 null, 노트북 송신 큐 버림(`health` 의 `laptop_skipped`·`unattributed_gap`), 이벤트 클립 기록(`media_clip`·`media_clip_retention`)은 계약에 반영했다. 아직 계약에 없는 것은 19.1 에 둔다.

## 12.1 공통 봉투와 버전

```json
{
  "schema_version": "1.0.0",
  "msg_type": "world_state",
  "msg_id": "uuid",
  "sent_at": 1760342400123,
  "source_id": "tier0-01",
  "payload": {}
}
```

- 시각은 UTC int64 ms, 좌표는 World(미터, Z-up), 각도는 라디안.
- 필드 추가는 마이너 버전(하위 호환), 필드 삭제·의미 변경은 메이저 버전. 수신 측은 모르는 필드를 무시한다.
- `msg_id`로 중복 수신을 제거한다. source_id는 실제 송신 주체(예: tier0-01, laptop-01, ec2)이며, Tier 0가 보내는 메시지의 source_id는 이벤트의 gateway_id와 같은 Tier 0 인스턴스 식별자다.

## 12.2 구간 목록

| ID | 구간 | 방식 | 메시지 |
|---|---|---|---|
| I1 | RunPod Tier 0 ↔︎ EC2 | WebSocket + JSON | 위로(Tier 0 → EC2): `world_state`(10 Hz), `health`(1 Hz), `observation_summary`(필요 시 카메라별 요약만. 2D 관측 원본 `observation_batch` 는 RunPod 에 Parquet 로 보관, 11.2), `calibration_update`, `risk_event_transition`, `event_state_snapshot`, `config_ack`, `event_frame_ready`, `media_clip`·`media_clip_retention`(이벤트 클립 기록, 11.3), `tier0_endpoint`(엔드포인트 등록, 3.12) / 아래로(EC2 → Tier 0): `event_ack`, `journal_ack`, `config_bundle` |
| I2 | EC2 → 뷰어 | WebSocket(STOMP) + REST | 토픽 `/topic/world`, `/topic/events`, `/topic/health` / REST: 리플레이, 구역 CRUD, 작업면 등록, Scene 자산, 레이아웃(12.6) |
| I3 | EC2 ↔︎ PWA | Web Push + REST | 푸시 `risk_alert`, 시험 푸시 / REST: 티켓 상태 변경, 사진 업로드 주소 발급, 푸시 구독·해지, 수신 확인(12.6) |
| I4 | EC2 ↔︎ 워치 | — | 이동(v1.3) → 부록 C |
| I5 | RunPod 분석 워커 → EC2 | 작업 가져가기(HTTPS long-poll 기본, WebSocket 대안), TLS + 워커 토큰 | 워커가 EC2 에 먼저 접속해 `job_lease_request`로 작업을 대여하고(응답 `job_request`), `job_heartbeat`로 대여를 연장하며, 결과·실패를 `job_callback`으로 제출한다. 하트비트·제출 응답은 `job_lease_status`(12.5) |
| I0 | 노트북 ↔︎ RunPod Tier 0 | 카메라별 TLS 연결(프레임 헤더 + 압축 프레임), 제어는 WebSocket(wss) | 위로: 프레임 헤더를 붙인 카메라 4대 압축 프레임(`frame_header`, 12.7), `codec_info`, `laptop_heartbeat` / 위로(제어): `site_ack`(현장 확인) / 아래로: `local_alert`, `alarm_stop`, `health`(활성 현장 경보 목록 `active_site_alerts` 포함, 3.10) / 양방향: 노트북–Pod 시계 차이 교환(아래로 `clock_sync_request`, 위로 `clock_sync_response`, 3.9) |

노트북 → EC2 는 HTTPS(기기 토큰)로 `laptop_heartbeat`(1초)를 보내고 Tier 0 엔드포인트(`tier0_endpoint`)를 조회한다(12.6).

## 12.3 I1 주요 메시지

```json
{
  "msg_type": "world_state",
  "payload": {
    "t_latest_global": 1760342400100,
    "calibration_version": "cal-7",
    "scene_version": "scene-3",
    "tracks": [{
      "world_id": "w-0012",
      "t_last_valid": 1760342400080,
      "observation_state": "OBSERVED",
      "track_lifecycle": "CONFIRMED", "identity_confidence": "HIGH", "identity_revision": 2,
      "contact_validity": "VALID", "frame_age_ms": 140,
      "position_xy": [3.40, 5.01], "pelvis_xyz": [3.40, 5.01, 0.92],
      "ground_anchor_xyz": [3.41, 5.02, 0.0], "support_surface_id": "floor", "support_surface_validity": "VALID",
      "position_xy_sigma": [0.08, 0.08], "ground_anchor_sigma": [0.06, 0.06, 0.03], "head_z_sigma": 0.05,
      "velocity": [0.9, 0.1, 0.0],
      "head_z": 1.68, "z_validity": "VALID",
      "cameras": ["cam1", "cam3"],
      "skeleton": {"joints": [[3.40, 5.01, 0.92]], "visible": [true], "validity": ["VALID"], "joint_source": ["observed"]}
    }]
  }
}
```

```json
{
  "msg_type": "health",
  "payload": {
    "system_health": "NOMINAL",
    "cameras": [{"id": "cam1", "fps": 14.8, "last_capture": 1760342400100, "sync_offset_ms": 6, "status": "OK"}],
    "pipeline_lag_ms": 120,
    "calibration_state": "CALIBRATED",
    "gpu_util": 0.71,
    "active_site_alerts": [],
    "active_site_alerts_as_of": 1760342400120,
    "laptop_clock_offset": {"offset_ms": 12.5, "uncertainty_ms": 21.5, "round_trip_ms": 43, "sample_count": 30, "estimated_at": 1760342399800}
  }
}
```

`world_state` 는 그 tick 에 적용돼 있던 구성 묶음 버전 `config_version`(선택)을 함께 싣는다(3.2).

`health`는 활성 현장 경보 목록(`active_site_alerts`, 3.10)과 노트북–Pod 시계 차이·불확실성(`laptop_clock_offset`, 3.9. 모르면 null)도 싣는다. `pipeline_lag_ms` 는 시계 차이를 보정한 T_lag 다(6.1). 카메라별 프레임 집계(`cameras[].frames`)에는 손실 3종과 따로 노트북 송신 큐 버림(`laptop_skipped`)과 구분 불가 빈칸(`unattributed_gap`)을 싣는다(12.7).

`event_frame_ready`는 EC2가 발급한 임시 업로드 주소로 프레임을 올린 뒤 그 주소와 촬영 시각·카메라만 보낸다.

`media_clip` 은 이벤트 클립(카메라 하나 × 위험 구간 하나)을 임시 업로드 주소로 올린 뒤 객체 키, 촬영 시각 구간, 해시·크기, 얼굴 흐림 여부(`face_redaction`, 기본 NONE), 구간 안 빈칸(`has_gaps`), 삭제 예정일, 보존 표시를 보낸다. 영상은 싣지 않는다. 보낸 뒤 이벤트가 CRITICAL 로 오르면 기록을 고치지 않고 `media_clip_retention` 으로 보존 표시를 더한다(11.5).

`risk_event_transition`은 RunPod Tier 0 서비스가 전이를 확정할 때마다 저널·outbox를 거쳐 보낸다(3.2).

```json
{
  "msg_type": "risk_event_transition",
  "payload": {
    "event_id": "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f", "event_sequence": 3,
    "transition": "UPDATED", "occurred_at": 1760342400350, "gateway_id": "tier0-01",
    "observation_time": 1760342400100, "evaluated_at": 1760342400340,
    "rule_id": "R2", "rule_version": 3, "config_version": "cfg-12",
    "calibration_version": "cal-7", "scene_version": "scene-3",
    "source": "LIVE", "severity": "CRITICAL", "confidence": "HIGH",
    "verification": "VERIFIED", "world_ids": ["w-0012"], "zone_id": "z-04",
    "metrics": {"edge_distance_m": 0.82, "height_diff_m": 2.1},
    "evidence_ref": "world_state msg_id", "resent": false
  }
}
```

`evidence_ref` 키는 늘 싣는다. 판정 전이는 판정에 쓴 `world_state` 의 msg_id 이고, 판정이 아닌 전이(운영자 확인·재시작 복원·ID 교정)는 근거를 모르면 null 이다(3.2 저널 계약).

## 12.4 I2·I3 주요 메시지

```json
{
  "msg_type": "risk_event",
  "payload": {
    "event_id": "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f", "rule_id": "R2", "rule_version": 3,
    "source": "LIVE", "lifecycle": "RAISED", "verification": "VERIFIED",
    "severity": "CRITICAL", "confidence": "HIGH", "needs_check": false,
    "world_ids": ["w-0012"], "zone_id": "z-04",
    "metrics": {"edge_distance_m": 0.82, "height_diff_m": 2.1},
    "rationale": null, "proposed_severity": null
  }
}
```

- `rationale`은 VLM 결과가 오면 `risk_event` 갱신으로 채워진다.
- “확인 필요”는 `confidence_reasons[]`로 싣는다(7.1). 위 예시의 `needs_check`는 이전 버전 호환용으로 남은 같은 뜻의 값이다. 재개 횟수는 `resume_count` 다(6.4).
- I3 `risk_alert`는 `event_id`, 등급, 구역, 한 줄 요약(규칙 근거)만 담는다. 시험 푸시는 `risk_alert`가 아니며 “시험”을 표시한다(3.10).

## 12.5 I5 작업 유형

**작업 가져가기.** 분석 워커가 EC2 로 먼저 접속해(long-poll 또는 WebSocket) 작업을 가져가 실행하고 결과를 제출한다. 그래서 Pod 로 들어오는 포트, IP 제한, HMAC 서명 콜백이 필요 없다. 연결은 TLS 와 워커 토큰으로 보호한다(14장).

- 대여: 워커가 `POST /api/worker/jobs/lease`(`job_lease_request`, long-poll)를 보내면 EC2 는 작업이 생길 때 작업 하나와 대여(`lease_id`, 만료 시각)를 담은 `job_request` 를 돌려준다. 한 작업에는 한 시점에 유효한 대여가 하나뿐이고, 우선순위는 3.2 를 따른다.
- 연장·제출: 워커는 실행 중 `job_heartbeat` 로 대여를 연장하고, 결과·실패를 `job_callback` 으로 제출한다. 응답은 `job_lease_status`(ACTIVE·ACCEPTED·DUPLICATE·REJECTED)다. 이미 반영한 대여(`lease_id`)의 재제출은 대여가 만료된 뒤라도 처음 반영한 것을 유지하고 DUPLICATE 로 답한다(만료 검사보다 먼저 본다).
- 만료·재할당: 하트비트도 결과도 없이 대여가 만료되면 EC2 는 시도 횟수 안에서 같은 작업을 새 대여로 다시 내준다. 아직 반영한 적 없는 만료·재할당 대여의 하트비트·제출은 거부(REJECTED)하고 늦은 결과는 버린다.
- `job_request`·`job_callback` 은 2.0.0(메이저)에서 콜백 주소와 HMAC 서명 필드를 없앴다. 경로는 기본안이며 확정본은 서버 OpenAPI 문서다.

| `job_type` | 입력 | 출력 |
|---|---|---|
| `vlm_rationale` | 이벤트 프레임 주소, 구조화 상태 | 근거 문장, 상향 의견 |
| `tbm_report` | 규칙 계산 결과, RAG 질의 | 보고서 본문(LLM 외부 API, 텍스트만), 인용 조항 ID |
| `static_reconstruct` | 카메라 프레임 주소, 보정 버전 | glTF 주소, 품질 지표 |
| `calibration_ba` | 2D 관측 묶음(Network Volume), 초기 보정 | 보정 후보 |
| `hmr_refine` (확장·EXP-02) | 프레임·마스크·keypoint, World ID·보정 버전 | MHR 메시·포즈, 관절 출처, 품질 메타데이터 |
| `hmr_appearance_update` (EXP-02 실험) | 동일 World ID의 동의된 관측 샘플, 이전 시각 자산 | 신규 텍스처/외형 버전, 관측 영역·불확실성·아티팩트 참조 |

작업은 시간 제한(`timeout_ms`)을 두고, 넘기면 TIMED_OUT 으로 끝내며 다시 내주지 않는다. 분석 워커 실패는 VLM·정적 3D·보고서에만 영향을 준다. Tier 0 서비스 실패는 실시간 관측·World XYZ·위험 판정을 멈추고 UNSAFE_UNKNOWN이 되며, Tier 0와 EC2 사이 연결만 실패하면 판정과 노트북 경보는 유지되고 원격 전달만 지연된다(6.5).

## 12.6 REST API (I2·I3)

경로는 기본안이며, 확정본은 Spring Boot에서 생성하는 OpenAPI 문서로 관리한다.

| 메서드 | 경로 | 용도 | 권한 |
|---|---|---|---|
| POST | /api/auth/login | 로그인, JWT·갱신 토큰 발급 | 공개 |
| GET | /api/health | 현재 System Health | 열람 |
| GET | /api/world?at=\{t\} | 시각 t의 World 상태 스냅샷 | 열람 |
| GET | /api/replay?from=\{t1\}&amp;to=\{t2\} | 구간 궤적·이벤트(최소 리플레이, 골격·버전은 확장) | 열람 |
| GET · POST · PUT | /api/zones | 구역 조회·생성·새 버전 | 쓰기는 관리자 |
| GET · POST · PUT | /api/static-objects | 정적 객체·작업면(OBB, 상판 높이) 조회·등록·새 버전(7.5) | 쓰기는 관리자 |
| GET | /api/rules | 규칙 버전 조회 | 열람 |
| POST · GET | /api/config-bundles, /api/config-bundles/\{version\} | 구성 묶음 발행, 현장 적용 상태 조회 | 발행은 관리자, 조회는 열람 |
| GET · POST | /api/events, /api/events/\{id\}/ack | 이벤트 목록, 확인 | 반장·관리자 |
| GET | /api/scenes/current | COMMITTED Scene과 glTF 주소 | 열람 |
| POST | /api/scenes/rebuild | 정적 3D 재생성 요청 | 관리자 |
| GET · POST | /api/layouts | 가상 레이아웃 | 관리자 |
| POST | /api/layouts/\{id\}/evaluate | 계획 모드 단독 평가(확장. M3 TBM 보고서에서 계산을 어떻게 요청할지는 확인 필요) | 관리자 |
| POST | /api/reports/tbm | TBM 보고서 생성 요청 | 관리자 |
| GET · PATCH | /api/tickets, /api/tickets/\{id\} | 조치 티켓 조회·상태 변경 | 반장(조회는 관리자도) |
| POST | /api/uploads/sign | 사진·프레임·클립 임시 업로드 주소 | 기기·반장 |
| GET | /api/clips/\{id\} | 이벤트 클립 조회(임시 서명 주소) | 관리자 |
| PUT | /api/clips/\{id\}/retention | 클립 보존 표시 붙이기·떼기 | 관리자 |
| POST · DELETE | /api/push/subscribe | Web Push 구독 등록·해지 | 반장 |
| POST | /api/push/receipts | 푸시 수신 확인(Service Worker) | 반장 기기 |
| POST | /api/push/test | 시험 푸시(RiskEvent 아님, “시험” 표시) | 관리자 |
| POST | /api/devices/heartbeat | 노트북 하트비트(`laptop_heartbeat`, 1초) | 기기 |
| GET | /api/endpoints/tier0 | Tier 0 공개 IP·포트·공개키 지문 조회(`tier0_endpoint` 형식, 등록은 I1) | 기기 |
| GET | /api/metrics | 시스템 지표 조회 | 관리자 |

## 12.7 I0 프레임 식별과 전송

영상과 메타데이터를 다른 채널로 보내면 촬영 시각이 엉뚱한 프레임에 붙을 수 있다. 삼각측량이 이 결합에 기대므로 같은 연결, 같은 메시지에 싣는다.

- 노트북은 디코딩하지 않고 카메라가 보낸 압축 프레임(H.264 접근 단위) 단위로 읽어 각 프레임 앞에 헤더를 붙인다.

```json
{
  "camera_id": "cam_A",
  "stream_epoch": 12,
  "frame_sequence": 10324,
  "source_pts": 9876543,
  "pts_timebase": "1/90000",
  "estimated_capture_time_ms": 1760342400100,
  "timestamp_source": "RECEIVE_ESTIMATED",
  "timestamp_uncertainty_ms": 18,
  "keyframe": true
}
```

- `stream_epoch`는 카메라 스트림이 재연결될 때마다 증가한다. `(camera_id, stream_epoch, frame_sequence)`가 프레임의 유일 식별자다.
- `stream_epoch` 는 재시작에도 줄지 않는다. 노트북 중계는 카메라마다 지금까지 쓴 가장 큰 epoch 를 로컬 파일에 먼저 저장한 뒤 쓰고(세션 시작 UTC ms 를 하한으로 둘 수 있어 작은 수가 아닐 수 있다), 저장 파일이 깨졌으면 0 부터 다시 세지 않고 시작을 멈춘다. 같은 epoch 안의 전송 재접속(TLS 연결만 바뀜)은 번호를 이어 센다.
- Tier 0 는 그 카메라에서 이미 본 것보다 작은 epoch 를 받지 않는다(같은 식별자에 다른 프레임·시각이 붙을 수 있다). 그런 `codec_info` 가 오면 연결 기록 형식 안의 거절 알림(STREAM_REJECT)을 보내고 끊으며, 노트북은 더 큰 epoch 로 다시 시작할 때까지 그 카메라를 다시 붙이지 않는다. 새 TLS 연결이라는 이유로 epoch·번호 비교를 지우지 않는다.
- 접속: 노트북은 EC2 에서 받은 현재 엔드포인트(`tier0_endpoint`, 3.12)로 접속한다.
- 전송: 헤더 + 프레임을 길이 접두 방식으로 카메라별 독립 TLS over TCP 연결에 싣는다(한 카메라의 지연이 다른 카메라를 막지 않도록). RunPod Pod의 Direct TCP 포트를 쓰고, TLS는 Tier 0 서비스가 직접 종료하며, 접속 시 노트북 기기 토큰으로 인증한다. 제어 메시지(local_alert, alarm_stop, health, site_ack, laptop_heartbeat, clock_sync_request, clock_sync_response)는 같은 방식으로 인증한 WebSocket(wss)으로 주고받는다.
- 서버 확인(공개키 고정): Tier 0 TLS 키는 한 번 만들어 Network Volume 비밀 폴더에 둔다. 노트북은 그 공개키 지문만 신뢰한다(또는 팀 사설 CA). 인증서 검증을 끄는 구현은 금지한다(14장).
- 송신 큐: 노트북은 카메라별 송신 큐에 상한을 두고 소켓 송신 버퍼를 작게 잡는다. 큐가 넘치면 다음 키프레임까지 버리고, 버린 프레임은 노트북 쪽 skipped 로 따로 센다. 이것은 전송 손실(gateway_to_runpod_loss)이나 Tier 0 의 skipped_by_policy 와 다르다.
- 버림 알림(SKIP_NOTICE): 노트북은 버린 `frame_sequence` 범위를 연결 기록 형식 안의 알림으로 Tier 0 에 알린다(계약 스키마 밖, 형식 정본은 `gateway/relay` 의 `wire.py`). 지금 epoch 의 버린 구간은 겹치거나 맞닿은 것끼리 합친 구간 집합으로 들고 있다가 새 연결마다 다시 보낸다(같은 알림을 여러 번 받아도 늘지 않는다). Tier 0 는 알린 범위의 번호 빈칸을 손실로 세지 않고 `health.cameras[].frames.laptop_skipped` 로 보고한다. 구간 수가 상한을 넘거나 재시작으로 버림 기록이 잘려 손실인지 버림인지 모르는 빈칸은 `unattributed_gap`(구분 불가)으로 따로 세고, 처음 보는 epoch 의 첫 번호 앞은 세지 않는다(지어내지 않음).
- 누락: frame_sequence 빈 구간은 노트북 → RunPod 구간의 손실(gateway_to_runpod_loss)로 기록하고 재요청하지 않는다(실시간 우선). 버림 알림 범위(laptop_skipped)와 구분 불가 빈칸(unattributed_gap)은 손실에서 뺀다. 카메라 → 노트북 구간은 RTSP over TCP라 손실이 번호 빈칸이 아니라 도착 간격의 끊김으로 나타나므로, 프레임 도착 간격과 RTP 타임스탬프 간격으로 camera_to_gateway_gap을 잰다. 정책상 건너뛴 프레임(skipped_by_policy)은 손실이 아니다. 카메라 내부에서 버린 프레임은 관측할 수 없으므로 상류 구간의 실제 손실률은 확정할 수 없다. 이 값들은 health와 G0에 따로 보고한다.
- 역순·지연 도착: 시간 보간 창(예: 200ms) 안이면 정렬해 쓰고, 창을 넘긴 프레임은 버리고 기록만 한다.
- 재접속: 카메라 RTSP 스트림이 다시 연결되면 새 stream_epoch로 시작한다(노트북 → Tier 0 TLS 연결만 바뀐 전송 재접속은 위 규칙대로 같은 epoch·번호를 이어 쓴다). 어느 재접속이든 노트북은 연결 직후 코덱 정보(`codec_info`: 코덱 종류, SPS/PPS, PTS timebase)를 먼저 보낸 뒤 키프레임부터 전송한다. 코덱 정보와 키프레임이 모두 도착할 때까지 그 카메라는 관측 없음으로 처리한다. 핵심 요구는 연결 개수가 아니라 프레임과 시각이 잘못 결합되지 않고 카메라별 최신성이 유지되는 것이다.
- 촬영 시각은 3.9의 촬영 시각 추정과 촬영 시각 계약을 따른다.
