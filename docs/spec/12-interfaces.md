# 12. 인터페이스 명세

I0의 영상 프레임(바이너리, 12.7)을 제외한 모든 구간은 공통 봉투를 쓰고, 이미지는 메시지에 넣지 않고 객체 저장소 주소로 주고받는다. 아래 필드는 v1.0 기준이며 필드 정의의 정본은 저장소의 JSON Schema 파일로 관리한다. 스키마 파일은 저장소 `contracts/`에서 관리하며(21.2), 아직 스키마가 없는 메시지는 이 절의 예시가 기준이다.

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
| I1 | RunPod Tier 0 → EC2 | WebSocket + JSON | `world_state`(10 Hz), `health`(1 Hz), `observation_batch`(묶음), `calibration_update`, risk_event_transition, event_state_snapshot, 역방향 event_ack·config_bundle, `event_frame_ready` |
| I2 | EC2 → 뷰어 | WebSocket(STOMP) + REST | 토픽 `/topic/world`, `/topic/events`, `/topic/health` / REST: 리플레이, 구역 CRUD, Scene 자산, 레이아웃 |
| I3 | EC2 → PWA | Web Push + REST | 푸시 `risk_alert` / REST: 티켓 상태 변경, 사진 업로드 주소 발급 |
| I4 | EC2 ↔︎ 워치 | WebSocket + JSON | 아래로 `watch_alert`, `heartbeat` / 위로 `ack`, `gait_features` |
| I5 | EC2 ↔︎ RunPod | REST(비동기) | `POST /jobs` → `job_id` 반환 → RunPod이 `POST /callbacks/jobs/{id}`로 결과 전달(HMAC 서명) |
| I0 | 노트북 ↔︎ RunPod Tier 0 | 카메라별 TLS 연결(프레임 헤더 + 압축 프레임), 제어는 WebSocket | 위로: 프레임 헤더를 붙인 카메라 4대 압축 프레임(12.7), 하트비트 / 아래로: local_alert, alarm_stop, health / 위로(제어): site_ack(현장 확인) |

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
    "cameras": [{"id": "cam1", "fps": 14.8, "last_capture": 1760342400100, "sync_offset_ms": 6, "status": "OK"}],
    "pipeline_lag_ms": 120,
    "calibration_state": "CALIBRATED",
    "gpu_util": 0.71
  }
}
```

`event_frame_ready`는 EC2가 발급한 임시 업로드 주소로 프레임을 올린 뒤 그 주소와 촬영 시각·카메라만 보낸다.

`risk_event_transition`은 RunPod Tier 0 서비스가 전이를 확정할 때마다 outbox를 거쳐 보낸다(3.2).

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

## 12.4 I2·I3·I4 주요 메시지

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
- I3 `risk_alert`와 I4 `watch_alert`는 `event_id`, 등급, 구역, 방향(워치), 한 줄 요약만 담는다.
- I4 `ack`는 `event_id`, 진동 시각, 수신 확인 시각을 담는다.

## 12.5 I5 작업 유형

| `job_type` | 입력 | 출력 |
|---|---|---|
| `vlm_rationale` | 이벤트 프레임 주소, 구조화 상태 | 근거 문장, 상향 의견 |
| `tbm_report` | 규칙 계산 결과, RAG 질의 | 보고서 본문, 인용 조항 ID |
| `static_reconstruct` | 카메라 프레임 주소, 보정 버전 | glTF 주소, 품질 지표 |
| `calibration_ba` | 2D 관측 묶음, 초기 보정 | 보정 후보 |
| `hmr_refine` (확장·EXP-02) | 프레임·마스크·keypoint, World ID·보정 버전 | MHR 메시·포즈, 관절 출처, 품질 메타데이터 |
| `hmr_appearance_update` (EXP-02 실험) | 동일 World ID의 동의된 관측 샘플, 이전 시각 자산 | 신규 텍스처/외형 버전, 관측 영역·불확실성·아티팩트 참조 |

작업은 시간 제한을 두고, 초과 시 실패로 처리한다. 분석 워커 실패는 VLM·정적 3D·보고서에만 영향을 준다. Tier 0 서비스 실패는 실시간 관측·World XYZ·위험 판정을 멈추고 UNSAFE_UNKNOWN이 되며, Tier 0와 EC2 사이 연결만 실패하면 판정과 노트북 경보는 유지되고 원격 전달만 지연된다(6.5).

## 12.6 REST API (I2·I3)

경로는 기본안이며, 확정본은 Spring Boot에서 생성하는 OpenAPI 문서로 관리한다.

| 메서드 | 경로 | 용도 | 권한 |
|---|---|---|---|
| POST | /api/auth/login | 로그인, JWT 발급 | 공개 |
| GET | /api/health | 현재 System Health | 열람 |
| GET | /api/world?at=\{t\} | 시각 t의 World 상태 스냅샷 | 열람 |
| GET | /api/replay?from=\{t1\}&amp;to=\{t2\} | 구간 궤적·골격·이벤트 | 열람 |
| GET · POST · PUT | /api/zones | 구역 조회·생성·새 버전 | 쓰기는 관리자 |
| GET | /api/rules | 규칙 버전 조회 | 열람 |
| GET · POST | /api/events, /api/events/\{id\}/ack | 이벤트 목록, 확인 | 반장·관리자 |
| GET | /api/scenes/current | COMMITTED Scene과 glTF 주소 | 열람 |
| POST | /api/scenes/rebuild | 정적 3D 재생성 요청 | 관리자 |
| GET · POST | /api/layouts, /api/layouts/\{id\}/evaluate | 가상 레이아웃, 계획 모드 평가 | 관리자 |
| POST | /api/reports/tbm | TBM 보고서 생성 요청 | 관리자 |
| GET · PATCH | /api/tickets, /api/tickets/\{id\} | 조치 티켓 조회·상태 변경 | 반장 |
| POST | /api/uploads/sign | 사진·프레임·클립 임시 업로드 주소 | 기기·반장 |
| POST | /api/push/subscribe | Web Push 구독 등록 | 반장 |
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
- 전송: 헤더 + 프레임을 길이 접두 방식으로 카메라별 독립 TLS over TCP 연결에 싣는다(기본안: 한 카메라의 지연이 다른 카메라를 막지 않도록). 4대를 하나로 묶은 다중화 연결은 G0 비교군이다. RunPod Pod의 Direct TCP 포트를 쓰고, TLS는 Tier 0 서비스가 직접 종료하며, 접속 시 노트북 기기 토큰으로 인증한다. 제어 메시지(local_alert, alarm_stop, health)는 같은 방식으로 인증한 WebSocket으로 주고받는다.
- 누락: frame_sequence 빈 구간은 노트북 → RunPod 구간의 손실(gateway_to_runpod_loss)로 기록하고 재요청하지 않는다(실시간 우선). 카메라 → 노트북 구간은 RTSP over TCP라 손실이 번호 빈칸이 아니라 도착 간격의 끊김으로 나타나므로, 프레임 도착 간격과 RTP 타임스탬프 간격으로 camera_to_gateway_gap을 잰다. 정책상 건너뛴 프레임(skipped_by_policy)은 손실이 아니다. 카메라 내부에서 버린 프레임은 관측할 수 없으므로 상류 구간의 실제 손실률은 확정할 수 없다. 세 값은 health와 G0에 따로 보고한다.
- 역순·지연 도착: 시간 보간 창(예: 200ms) 안이면 정렬해 쓰고, 창을 넘긴 프레임은 버리고 기록만 한다.
- 재접속: 새 stream_epoch로 시작하고, 노트북은 연결 직후 코덱 정보(코덱 종류, SPS/PPS, PTS timebase)를 먼저 보낸 뒤 키프레임부터 전송한다. 코덱 정보와 키프레임이 모두 도착할 때까지 그 카메라는 관측 없음으로 처리한다. 핵심 요구는 연결 개수가 아니라 프레임과 시각이 잘못 결합되지 않고 카메라별 최신성이 유지되는 것이다.
- 촬영 시각은 3.9의 촬영 시각 계약을 따른다.
