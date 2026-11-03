# 12. 인터페이스 명세

I0의 영상 프레임(바이너리, 12.7)을 제외한 모든 구간은 공통 봉투를 쓰고, 이미지는 메시지에 넣지 않고 객체 저장소 주소로 주고받는다. 아래 필드는 v1.0 기준 예시이며 필드 정의의 정본은 저장소 `contracts/`의 JSON Schema 파일이다(21.2). 아직 스키마가 없는 메시지는 이 절의 예시가 기준이다. v1.3 에서 바뀐 규칙 중 현장 경보 목록(`health.active_site_alerts`), 시계 차이 교환(`clock_sync_request`·`clock_sync_response`, `health.laptop_clock_offset`), 엔드포인트 등록(`tier0_endpoint`), 2D 관측 요약(`observation_summary`), I5 작업 대여(`job_lease_request`·`job_request`·`job_heartbeat`·`job_callback`·`job_lease_status`), 확인 필요 이유(`confidence_reasons`), 이벤트 재개(`resumed`·`resume_count`), 확인 대상(`site_ack`·`event_ack`·`alarm_stop` 의 `event_sequence`), `world_state.config_version`, 판정이 아닌 전이의 근거 null, 노트북 송신 큐 버림(`health` 의 `laptop_skipped`·`unattributed_gap`), 이벤트 클립 기록(`media_clip`·`media_clip_retention`), 운영자 종료(`event_close`·`event_close_result`, `risk_event_transition`·`risk_event` 2.0.0 의 운영자 종료 해소 `resolution`: 있으면 해소에 VERIFIED·판정 근거를 요구하지 않음, 6.4), 운영자 상향(`event_escalate`·`event_escalate_result`, `risk_event_transition`·`risk_event` 2.1.0 의 `escalation`·`escalations`, 5.4), I5 작업 유형 `bev_floorplan`·`incident_summary`와 `tbm_report` 보고서 종류(`job_request` 3.1.0), I2 화면 상태 `viewer_health`·조치 불일치 `action_mismatch`(12.4), 아바타 포즈 스트림(I6 `avatar_pose_batch`·`avatar_asset`, I2 `/topic/avatar` 의 `avatar_frame` 1.0.0, 계약 묶음 6.2.0, 12.2·12.4)은 계약에 반영했다. 아직 계약에 없는 것은 19.1 에 둔다.

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

- 시각은 UTC int64 ms, 좌표는 World(미터, Z-up), 각도는 라디안(아바타 관절 회전은 축각 rotation vector 라디안 하나, 공통 `axis_angle`).
- 필드 추가는 마이너 버전(하위 호환), 필드 삭제·의미 변경은 메이저 버전. 수신 측은 모르는 필드를 무시한다.
- `msg_id`로 중복 수신을 제거한다. source_id는 실제 송신 주체(예: tier0-01, worker-01, laptop-01, ec2)이며, Tier 0가 보내는 메시지의 source_id는 이벤트의 gateway_id와 같은 Tier 0 인스턴스 식별자다.

## 12.2 구간 목록

| ID | 구간 | 방식 | 메시지 |
|---|---|---|---|
| I1 | RunPod Tier 0 ↔︎ EC2 | WebSocket + JSON | 위로(Tier 0 → EC2): `world_state`(10 Hz), `health`(1 Hz), `observation_summary`(필요 시 카메라별 요약만. 2D 관측 원본 `observation_batch` 는 RunPod 에 Parquet 로 보관, 11.2), `calibration_update`, `risk_event_transition`, `event_state_snapshot`, `config_ack`, `event_frame_ready`, `media_clip`·`media_clip_retention`(이벤트 클립 기록, 11.3), `video_segment`(상시 영상 조각 기록, 5.1·11.3), `tier0_endpoint`(엔드포인트 등록, 3.12), `event_close_result`(운영자 종료 결과, 6.4), `event_escalate_result`(운영자 상향 결과, 5.4) / 아래로(EC2 → Tier 0): `event_ack`, `event_close`(관리자의 구역 대상 이벤트 종료, 6.4), `event_escalate`(관리자가 승인한 VLM 상향 의견 적용, 5.4), `journal_ack`, `config_bundle` |
| I2 | EC2 → 뷰어 | WebSocket(STOMP) + REST | 토픽 `/topic/world`(`world_state`), `/topic/avatar`(`avatar_frame`), `/topic/events`(`risk_event`), `/topic/health`(`viewer_health`)(12.4) / REST: 리플레이, 작업별 **현장 세팅 세션**(`/api/setup-sessions`, `/api/setup-sessions/{id}`, `/api/setup-sessions/{id}/retry`), 관제 화면 관리자 편집 모드 구역·작업면 저장·발행(`/api/zones/commit`), 보정·설치, Scene·아바타 자산(`/api/avatars/...`), 작업 관리(`PLANNED`→`IN_PROGRESS`→`DONE`, `/api/work-units/{id}/start`·`/end`), 레이아웃, TBM 보고서·작업 마무리·전자서명(`/api/reports/tbm/{reportId}/signatures`), 이벤트 상세·요약·상향(12.6) |
| I3 | EC2 ↔︎ PWA | Web Push + REST | 푸시 `risk_alert`, 시험 푸시 / REST: 티켓 상태 변경, 사진 업로드 주소 발급, 푸시 구독·해지, 수신 확인(12.6) |
| I4 | EC2 ↔︎ 워치 (확장, 부록 C.6) | WebSocket(wss) + JSON, 기기별 토큰 | 아래로(EC2 → 워치): `watch_alert`(진동 알림), `watch_heartbeat` / 위로(워치 → EC2): `watch_ack`(수신 확인, 운영자 확인이 아님), `gait_features`(걸음 특징), `watch_heartbeat`. 계약 6.10.0(S15P31A101-334), 연결·끊김 판정은 `docs/design/watch-i4-contract.md` |
| I5 | RunPod 분석 워커 → EC2 | 작업 가져가기(HTTPS long-poll 기본, WebSocket 대안), TLS + 워커 토큰 | 워커가 EC2 에 먼저 접속해 `job_lease_request`로 작업을 대여하고(응답 `job_request`), `job_heartbeat`로 대여를 연장하며, 결과·실패를 `job_callback`으로 제출한다. 하트비트·제출 응답은 `job_lease_status`(12.5) |
| I6 | RunPod 분석 워커 → EC2 (아바타 스트림) | WebSocket(wss) + JSON, 워커 토큰(I1 과 분리된 별도 연결, 기본 경로 `/ws/worker/avatar`) | 위로(워커 → EC2, 단방향): `avatar_pose_batch`(1.0.0, 사람별 2\~5 Hz 묶음, MHR 축각 회전만·위치 없음), `avatar_asset`(1.0.0, 스키닝 glTF·UV 텍스처·표면 출처 지도 버전 알림, 처음·버전 변경·재접속 직후·10초 재알림). 상시 아바타 루프(`services/avatar`)는 유휴 중에도 `recv(timeout=0)` 와 10초 ping 으로 반쯤 닫힌 연결을 감지해 재접속 직후 캐시된 `avatar_asset` 을 다시 알린다(`docs/design/hmr-avatar-loop.md`). `avatar_asset.mesh.vertex_count` 는 glTF 정점 수(UV 이음매에서 나눈 뒤, MHR 자산 기준 19,455)이고 MHR 정점 수(18,439)와 다르다. 관절 순서의 기준은 MHR 자산 관절 인덱스다(자산에 관절 이름이 있어 glTF 노드 이름에 넣지만 기준은 인덱스, 0 `body_world`·1 `root`(pelvis) …). 몸 방향은 `root_rotation` 에 싣으므로 관절 0·1 회전은 늘 [0, 0, 0] 이다. `skeleton.rig_source` 는 자산 출처(HF `facebook/sam-3d-body-dinov3@11aaa34`)를 적는다(S15P31A101-88, `docs/design/mhr-skinned-gltf.md`). 세부는 `docs/design/avatar-pose-stream.md` 와 `contracts/`(묶음 6.2.0)가 정본이고, 오프라인 Run 기록은 `experiments/human_mesh_output`(2.0.0)을 그대로 둔다 |
| I0 | 노트북 ↔︎ RunPod Tier 0 | 카메라별 TLS 연결(프레임 헤더 + 압축 프레임), 제어는 WebSocket(wss) | 위로: 프레임 헤더를 붙인 카메라 4대 압축 프레임(`frame_header`, 12.7), `codec_info`, `laptop_heartbeat` / 위로(제어): `site_ack`(현장 확인) / 아래로: `local_alert`, `alarm_stop`, `health`(활성 현장 경보 목록 `active_site_alerts` 포함, 3.10) / 양방향: 노트북–Pod 시계 차이 교환(아래로 `clock_sync_request`, 위로 `clock_sync_response`, 3.9) |

EC2 내부: Spring ↔ Risk Engine Python 컨테이너(`deploy/risk-engine`)는 compose 내부망 HTTP/JSON 이다(공개 포트 없음). (1) PLANNED 판정(`POST /v1/plan/evaluate`): Spring 이 `risk_plan_request`(출처 PLANNED, 판정 기준 시각, 규칙 묶음, 레이아웃의 구역·작업면·선택 계획 작업 위치)를 보내고 컨테이너가 `risk_plan_result`(등급 + 근거 위험 항목, 판정에 쓴 위치 목록, 또는 오류)로 답한다. 결과는 `sim.*` 에만 저장하고 알림 채널로 보내지 않는다(7.4, 8.3). 오류 판정 순서·HTTP 상태·본문 없는 실패의 처리는 계약(`contracts/`)이 정본이다. MVP 는 PLANNED 만 쓰고 REPLAY·WHAT_IF 재계산은 확장(부록 C)이다. (2) TBM·일일 보고서 생성(`POST /v1/report/generate`, v1.3 수정 49): Spring 이 보고서 종류(`TBM`·`DAILY`), PLANNED 판정 결과, 해당 구역 최근 `risk_event` 발생 이력 요약(`zone_history`)을 보내면, 같은 컨테이너가 PostgreSQL 법령 지식 그래프(`rag_node`·`rag_edge`) 하이브리드 검색·참조 엣지 1단계 확장과 외부 LLM 호출·코드 4단계 검증을 수행해 본문(`sections`)·인용(`citations`)·서브그래프(`subgraph`)·실행 단계(`steps`)를 돌려준다(GPU·Pod 불필요, `docs/design/tbm-legal-graph.md`). (3) 작업 종료 시 TBM 보고서 “작업 마무리” 생성(v1.3 수정 52): 관리자가 `POST /api/work-units/{id}/end` 로 작업을 종료하면 Spring 이 그 작업의 실제 진행 구간(`[actual_started_at, actual_ended_at]`)과 대상 구역에서 발생한 LIVE `risk_event` 집계 사실(등급별·규칙별 건수, 확인/종료 건수, 작업 외 진입 건수)을 컨테이너에 넘기고, 컨테이너(외부 LLM, 숫자 검증·타임아웃 시 결정론적 템플릿 폴백)가 작성한 “작업 마무리” 요약(`wrap_up_summary`)을 받아 새 `report_version` 으로 저장한다(8.3, `docs/design/work-session-flow.md`).

노트북 → EC2 는 HTTPS(기기 토큰)로 `laptop_heartbeat`(1초)를 보내고 Tier 0 엔드포인트(`tier0_endpoint`)를 조회한다(12.6). 또한 관제 화면 CCTV 원격 시청(3.3, v1.3 수정 46)을 위해 기기 토큰 헤더로 `wss://<ec2>/ws/devices/cctv` 연결을 상시 유지하고, EC2 가 보내는 `cctv_remote_control`(1.0.0, `demand active`)이 `true` 인 동안에만 `simai-cctv-preview/1` 형식(`cctv_preview` 1.1.0 `hello`·`codec` 과 이진 프레임)으로 영상을 올린다. 로그인한 관리자는 `POST /api/cctv/remote/tickets` 로 유효 60 s 일회용 입장권을 받아 원시 WebSocket `wss://<ec2>/ws/cctv/view?ticket=…` 로 시청한다(묶음 6.11.0, `docs/design/cctv-remote-view.md`).

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
      "track_lifecycle": "CONFIRMED", "identity_confidence": "HIGH", "identity_revision": 2, "aliases": ["w-0009", "w-0011"],
      "contact_validity": "VALID", "frame_age_ms": 140,
      "position_xy": [3.40, 5.01], "pelvis_xyz": [3.40, 5.01, 0.92],
      "ground_anchor_xyz": [3.41, 5.02, 0.0], "surface_source": "observed", "support_surface_id": "floor", "support_surface_validity": "VALID",
      "position_xy_sigma": [0.08, 0.08], "ground_anchor_sigma": [0.06, 0.06, 0.03], "head_z_sigma": 0.05,
      "velocity": [0.9, 0.1, 0.0],
      "head_z": 1.68, "head_z_validity": "VALID", "z_validity": "VALID",
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
    "cameras": [{"id": "cam1", "fps": 14.8, "last_capture": 1760342400100, "sync_offset_ms": 6, "sync_valid": true, "status": "OK"}],
    "pipeline_lag_ms": 120,
    "calibration_state": "CALIBRATED",
    "tier0_warnings": [],
    "gpu_util": 0.71,
    "active_site_alerts": [],
    "active_site_alerts_as_of": 1760342400120,
    "laptop_clock_offset": {"offset_ms": 12.5, "uncertainty_ms": 21.5, "round_trip_ms": 43, "sample_count": 30, "estimated_at": 1760342399800}
  }
}
```

`world_state` 는 그 tick 에 적용돼 있던 구성 묶음 버전 `config_version`(선택)을 함께 싣는다(3.2). `head_z` 는 `head_z_validity` 가 VALID 나 UNCERTAIN 이면 값을, UNAVAILABLE 이면 null 을 싣는다. 값이 있어도 `head_z_validity` 를 확인하고 UNCERTAIN 값은 확정 근거로 쓰지 않는다(5.2. `world_state` 2.0.0 의미 변경, 1.x 는 VALID 일 때만 값). `identity_revision` 은 병합·분할 때 오르고, 병합으로 합쳐진 이전 ID 는 `aliases`, 분할로 갈라져 나간 새 ID 의 원래 ID 는 `split_from`(선택, 2.1.0)으로 싣는다(5.2).

`health`는 활성 현장 경보 목록(`active_site_alerts`, 3.10)과 노트북–Pod 시계 차이·불확실성(`laptop_clock_offset`, 3.9. 모르면 null)도 싣는다. `pipeline_lag_ms` 는 시계 차이를 보정한 T_lag 이고, 모르면 null(그때 UNSAFE_UNKNOWN, 아는 하한은 `pipeline_lag_lower_bound_ms`)이다(6.1). 카메라별 `sync_valid` 는 동기화 근거(true/false, 근거 없으면 null)다(6.1). Tier 0 운영 경고(체크포인트·저널·EC2 전송 보류·재동기화 스냅샷)는 `tier0_warnings` 에 code 와 처음 확인한 시각으로 싣고, 빈 배열은 경고 없음, 필드가 없으면 모름이다(6.5). 카메라별 프레임 집계(`cameras[].frames`)에는 손실 3종과 따로 노트북 송신 큐 버림(`laptop_skipped`)과 구분 불가 빈칸(`unattributed_gap`)을 싣는다(12.7).

`event_frame_ready`는 LIVE R1\~R4 CRITICAL 이벤트가 처음 발생할 때(병합 이벤트당 1회) Tier 0 가 60초 압축 링 버퍼에서 대상 관측 카메라 우선 최대 2대 × 2시점(이벤트 0\~500 ms 전, 3\~5초 전, 사후 프레임 제외)을 디코딩·JPEG 인코딩해 EC2 가 발급한 임시 업로드 주소(`EVENT_FRAME`)로 올린 뒤 그 객체 키와 촬영 시각 세 값(3.9)·카메라만 보낸다(5.4, `docs/design/vlm-rationale.md`).

`media_clip` 은 이벤트 클립(카메라 하나 × 위험 구간 하나)을 임시 업로드 주소로 올린 뒤 객체 키, 촬영 시각 구간, 해시·크기, 얼굴 흐림 여부(`face_redaction`, 기본 NONE), 구간 안 빈칸(`has_gaps`), 삭제 예정일, 보존 표시를 보낸다. 영상은 싣지 않는다. 보낸 뒤 이벤트가 CRITICAL 로 오르면 기록을 고치지 않고 `media_clip_retention` 으로 보존 표시를 더한다(11.5).

`video_segment` 는 상시 영상 조각(카메라 하나 × 일정 길이)을 임시 업로드 주소(목적 `VIDEO_SEGMENT`, `segment_id`)로 올린 뒤 객체 키, 촬영 시각 구간, stream_epoch, 해시·크기, 구간 안 빈칸, 삭제 예정일을 보낸다. 영상은 싣지 않는다. 이벤트와 묶이지 않고 보존 표시가 없다(5.1, 11.5, v1.3 수정 36).

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

`evidence_ref` 키는 늘 싣는다. 판정 전이는 판정에 쓴 `world_state` 의 msg_id 이고, 판정이 아닌 전이(운영자 확인·재시작 복원·ID 교정·운영자 상향)는 근거를 모르면 null 이다(3.2 저널 계약).

`event_escalate` 는 관리자가 VLM 상향 의견을 승인했을 때만 EC2 가 보낸다(거절은 EC2 `vlm_assessment` 에 기록하고 `risk_event` 투영의 `proposed_severity` 를 비움, 5.4). Tier 0 는 활성 이벤트이고 올릴 등급이 지금 등급보다 높을 때만 같은 `event_id` 의 UPDATED 전이(`escalation` 포함, `resumed` false)를 저널에 기록하고 `event_escalate_result`(`ACCEPTED`, `DUPLICATE`, `REJECTED`)로 답한다. 같은 `escalation_id` 는 다시 기록하지 않는다(DUPLICATE). EC2 는 `REJECTED` 사유가 `NOT_ACTIVE`·`NOT_UPWARD` 이면 상향 제안을 해제하고, `JOURNAL_UNAVAILABLE` 이면 같은 `escalation_id` 로 재시도할 수 있게 둔다(`docs/design/server-event-journal.md`). 이 전이로 등급이 오르므로 3.10 의 상향 재경보를 따른다. 올린 등급은 관측 해소까지 바닥값이다(5.4). Tier 0 는 모든 `risk_event_transition` 을 2.1.0 으로 보낸다.

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
- `/topic/events` 경보 처리: `source` LIVE, `severity` CRITICAL·HIGH, `lifecycle` RAISED·UPDATED 인 이벤트가 관제 화면 소리·점멸 대상이다(8.1, 10장). 확인 상태는 `acks` 로 읽고, 지금 경보 단계를 연 `event_sequence` 이후를 대상으로 한 확인이 있으면 모든 관제 화면의 반복 소리를 멈춘다(3.10). 별도 경보 메시지 타입은 두지 않는다.
- 운영자 상향을 거친 등급은 `escalations` 로 표시해 규칙 판정 등급과 구분한다(2.1.0).
- `/topic/world` 는 I1 `world_state` 를 형식 그대로 다시 싣는다. EC2 는 봉투만 새로 쓰고(`source_id` `ec2`) payload 를 바꾸지 않는다. 뷰어가 밀리면 최신 것만 남긴다.
- `/topic/health` 는 `viewer_health` 다. EC2 가 Tier 0 `health`(원문은 `tier0_health`), 노트북 하트비트, `config_ack`, COMMITTED Scene 을 모아 1초마다(바뀌면 즉시) 보낸다. 원격 단절 구분(`remote_state`), 데이터 연령, 구성 묶음 적용 상태, Scene 불일치(Tier 0 가 쓰는 `world_state.scene_version` 과 EC2 의 COMMITTED Scene 버전이 다르면 경고, FR-12)를 싣는다. 확장(부록 C.6): 등록된 워치마다 I4 연결 상태(CONNECTED·LOST, 마지막 하트비트·배터리·전송 경로)와 등록·활성 워치 수를 `watches` 에 싣는다(1.2.0, S15P31A101-336). 워치는 개인 보조 채널이라 `system_health`·`remote_state` 를 바꾸지 않는다.
- 관리자 토픽 `/topic/admin/events` 와 조치 불일치 `action_mismatch` 는 취소했다(8.4, 2026-11-02). 관제 앱 구독(S15P31A101-423)과 계약 `action_mismatch`·EC2 구독 권한을 지웠다(계약 묶음 7.0.0, S15P31A101-411). 이 토픽을 구독하면 다른 정해진 토픽 밖 구독처럼 거부한다.
- `/topic/avatar` 는 `avatar_frame`(1.0.0, 계약 묶음 6.2.0)이다. EC2 는 I6 으로 받은 사람별 최신 포즈에 그 포즈의 아바타 자산 내려받기 경로(`/api/avatars/...`)를 붙여 중계한다. 브로커가 밀리면 오래된 `avatar_frame` 부터 버리고 `/topic/world`·`/topic/events` 전달은 방해하지 않는다. 사람의 존재·위치·접지·관측 상태·ID 신뢰도는 `/topic/world` 가 정본이며, `/topic/world` 에서 빠지거나 오래 갱신되지 않은 사람은 지운다. 중복·역전 거르기, 자산 매칭, 크기·인원 상한 등 세부는 `docs/design/avatar-pose-stream.md` 와 `contracts/` 가 정본이다.

## 12.5 I5 작업 유형

**작업 가져가기.** 분석 워커가 EC2 로 먼저 접속해(long-poll 또는 WebSocket) 작업을 가져가 실행하고 결과를 제출한다. 그래서 Pod 로 들어오는 포트, IP 제한, HMAC 서명 콜백이 필요 없다. 연결은 TLS 와 워커 토큰으로 보호한다(14장).

- 대여: 워커가 `POST /api/worker/jobs/lease`(`job_lease_request`, long-poll)를 보내면 EC2 는 작업이 생길 때 작업 하나와 대여(`lease_id`, 만료 시각)를 담은 `job_request` 를 돌려준다. 한 작업에는 한 시점에 유효한 대여가 하나뿐이고, 우선순위는 3.2 를 따른다.
- 연장·제출: 워커는 실행 중 `job_heartbeat` 로 대여를 연장하고, 결과·실패를 `job_callback` 으로 제출한다. 응답은 `job_lease_status`(ACTIVE·ACCEPTED·DUPLICATE·REJECTED)다. 이미 반영한 대여(`lease_id`)의 재제출은 대여가 만료된 뒤라도 처음 반영한 것을 유지하고 DUPLICATE 로 답한다(만료 검사보다 먼저 본다).
- 만료·재할당: 하트비트도 결과도 없이 대여가 만료되면 EC2 는 시도 횟수 안에서 같은 작업을 새 대여로 다시 내준다. 아직 반영한 적 없는 만료·재할당 대여의 하트비트·제출은 거부(REJECTED)하고 늦은 결과는 버린다.
- 반납: Tier 0 우선(3.2: 지연 초과·모름·큐 대기 초과(창당 상한 초과)로 hold 상한까지 시작하지 못함, LIVE 관제 중 오프라인·배치 EXP 유형(I5 배치 `hmr_*` 포함, I6 상시 아바타 스트림 루프는 3.2 지연 감시 예외), 도는 중 LIVE 시작으로 멈춤) 때문에 돌려주는 작업은 `job_callback` FAILED 에 `error.retryable: true`·`consume_attempt: false` 를 싣는다. EC2 는 시도 횟수를 쓰지 않고 같은 attempt·새 대여로 다시 내준다(마지막 시도도 FAILED 로 끝내지 않음). 처리기 실패·입력 실패·시간 초과는 반납이 아니고 시도를 쓴다(`job_callback` 2.1.0). 그래서 `job_request` 3.0.0(메이저)부터 `attempt` 는 반납을 세지 않는 시도 번호, `max_attempts` 는 최대 시도 수이고, 같은 `attempt` 가 여러 대여에 되풀이될 수 있다(2.x 는 대여 순번·최대 대여 횟수). 워커는 3.x 가 아닌 `job_request` 를 받지 않는다.
- 객체 저장소: 작업 입력에 있는 객체(프레임·보정 문서 등)를 받거나 결과를 올릴 때 워커는 대여에 묶인 서명 API `POST /api/worker/jobs/{job_id}/objects/sign`(워커 토큰, `lease_id`·`op` GET|PUT)으로 임시 서명 주소를 받는다. GET 은 그 작업 입력에 있는 주소만, PUT 키는 EC2 가 그 작업 전용으로 정한다(14장).
- `job_request`·`job_callback` 은 2.0.0(메이저)에서 콜백 주소와 HMAC 서명 필드를 없앴다. 경로는 기본안이며 확정본은 서버 OpenAPI 문서다.

| `job_type` | 입력 | 출력 |
|---|---|---|
| `vlm_rationale` | 이벤트 프레임 주소(프레임마다 카메라·촬영 시각 세 값(3.9)·`face_policy`, 워커는 `observation_time` 뒤 프레임을 쓰지 않음), 구조화 상태 `structured_state`(계약은 객체만 정한다. 워커는 `tracks[]` 의 `world_id`·`position_xy`·`head_z` 와 `zone` 의 `zone_id`·`judge_type` 을 읽고 없는 키는 건너뛴다). 우선순위 `VLM_FAST`(`timeout_ms=5000`, `max_attempts=2`, `deadline_at = observation_time + 5000 ms`) | 근거 문장(한국어 1\~3문장), 상향 의견(입력 등급보다 높을 때만 남기고 같거나 낮으면 `null` + `NOT_UPWARD` 로그, MVP 는 CRITICAL 만 대상이라 늘 null, 5.4), `clause_ids`(지금 늘 `[]`: 입력에 조항이 없고 워커는 조항을 검색하지 않는다, 19.1). 성공 시 EC2 가 `vlm_assessment`(G7 지연 포함)와 `risk_event` 투영을 갱신하고 `/topic/events` 로 발행(`docs/design/vlm-rationale.md`) |
| `tbm_report` (**폐기 예정**, 2026-11-02, S15P31A101-415) | **폐기 예정(EC2 Python 컨테이너로 이전, 5.4·8.3·12.2):** TBM·일일 보고서 생성은 GPU 가 필요 없으므로 EC2 의 Python 컨테이너(`deploy/risk-engine`)에서 PostgreSQL 법령 지식 그래프(`rag_node`·`rag_edge`) 하이브리드 검색과 외부 LLM 호출·4단계 코드 검증으로 직접 실행한다. 호환을 위해 `contracts/` 스키마(`tbm_report.schema.json`, `job_request` 3.3.0, `job_callback` 2.3.0)는 남겨 두되 신규 보고서 생성 경로에서는 I5 `tbm_report` 작업을 등록하지 않는다 | 결과(`job_callback` 2.3.0 호환 유지): 신규 경로(`POST /api/reports/tbm`·`GET /api/reports/tbm/{reportId}`·`POST /api/reports/daily`)는 EC2 Python 컨테이너 결과를 받아 `subgraph`(`nodes`·`edges`)와 `steps`(4단계 실행 상태)를 함께 저장·반환한다(`docs/design/tbm-legal-graph.md`) |
| `incident_summary` | 이벤트, 이벤트 전후 20초 클립 객체 키(`media_clip`), 얼굴 처리 `face_policy`, 구조화 상태 | 요약 문장(상향 의견 없음, 분석 워커 처리기는 진행 중 S15P31A101-271). 우선순위 REPORT |
| `bev_floorplan` | `bev_floorplan.schema.json` 입력(`job_request` 3.2.0): 보정 버전·보정 문서 주소와 해시, 프레임 출처, 카메라마다 사람 없는 프레임 한 장(주소·촬영 시각), 선택 Scene 버전 | BEV 평면도 이미지·메타데이터 주소, 픽셀 ↔ World 대응, 방 외곽, `input_provenance`(보조 화면, 안전 판단에 쓰지 않음, 4.1, `job_callback` 2.2.0). 우선순위 BACKGROUND |
| `static_reconstruct` · `scene_regenerate` | `static_reconstruct.schema.json` 입력(`job_request` 3.2.0): 보정 버전·보정 문서 주소와 해시(`PROVISIONAL`·`CALIBRATED`), 카메라 4대의 프레임(`FRAME`) 또는 Tier 0 사람 지운 배경(`CLEAN_BACKGROUND`) 주소·해시·촬영 시각, 옵션(`resolution_tier`, `use_person_mask`). `scene_regenerate`(S15P31A101-427·-428, v1.3 수정 52)는 관리자가 관제 화면에서 **“맵 재생성”** 버튼(`POST /api/scenes/regenerate`)을 눌렀을 때 그때까지 쌓인 사람 지운 배경으로 정적 3D 배경 전체를 다시 구성하는 수동 재생성 작업이다(부분 재구성 미사용). `static_reconstruct` 는 LIVE 관제 중 대여에서 제외·LIVE 진입 시 반납하며(3.2, 5.3), `scene_regenerate` 를 Tier 0 우선 게이트(`-412`) 아래 LIVE 중 두 번째 예외로 허용할지는 11차 GPU 세션 측정 뒤 결정한다(19.1 미결) | `scene_output` 2.1.0(`CANDIDATE`, `VISUAL`): Open3D TSDF + `texrecon` 으로 구운 텍스처 메시(`GLTF_VISUAL`, 도구 부재 시 정점색 glTF 폴백)와 표면 출처 지도(`SURFACE_SOURCE_MAP`), `metrics.json`(스툴 축척 점검 포함), `run_record.json` 을 `static-3d/{job_id}/{lease_id}/...` 에 올리고 반환. EC2 `SceneJobListener` 는 `CANDIDATE` 로만 저장하고 자동 활성화하지 않으며 관리자가 확인 후 활성화(`commit`)한다(`docs/design/worker-static-reconstruct.md`, `docs/design/scene-version-from-job.md`, `docs/design/work-session-flow.md`). 우선순위 BACKGROUND |
| `vlm_zone_draft` (마이너 제안) | 현장 세팅 VLM 위험구역·고소 작업면 초안 요청(`docs/design/site-setup-pipeline.md`, `docs/design/work-session-flow.md`): 현장 세팅 보정(`CALIBRATED`)·점구름/미리보기 Scene 자산 주소(`scene_asset_uri`), 보정 문서 주소·해시(`calibration_ref`·`calibration_hash`), 깨끗한 배경 대표 프레임(`frames[]`), 얼굴 처리(`face_policy: BLURRED`). 세팅 4단계에서 빠른 미리보기 맵 생성과 병렬 실행. 우선순위 BACKGROUND | 정적 3D 배경·보정에서 추출한 바닥·상판·객체 기하 후보에 Cosmos3-Nano(16B)가 이름(`label`)·추천 심각도(`recommended_severity`: `CRITICAL`\|`HIGH`)·제안 이유(`rationale`)를 붙인 초안 목록(`drafts[]`, 고소 작업면 후보는 OBB + 둘레 `HIGH` 구역 다각형 포함). 세팅 완료 직후 관제 화면 관리자 편집 모드에 빠른 미리보기 맵 위 “AI 초안”으로 표시하며, 사람이 조정 후 `POST /api/zones/commit` 으로 발행(`config_bundle`)하는 것이 곧 확정(발행 전 판정 미사용, 5.4·8.2) |
| `calibration_ba` · `site_setup` (마이너 제안) | 현장 세팅 파이프라인(`site_setup`, `docs/design/site-setup-pipeline.md`): 작업 단위(`work_unit_id`), 카메라별 실측 높이(`camera_heights_m`), 깨끗한 배경 묶음(`clean-background`) → 상주 MapAnything 초기 자세·점구름 + World 자동 정의(바닥 Z=0, 벽 `+X` 또는 `CAMERA_LAYOUT`, 모서리 원점, 높이 스케일) → 인접 카메라 쌍(`A–B–C–D–A`) ALIKED+LightGlue 배경 특징점 공동 번들 조정(V2) 및 실측 정물 검증 → 빠른 미리보기 맵(`preview_scene`) 선출력 후 백그라운드 `texrecon` 완성(사람 관절 `observation_2d` 는 선택적 보조로만 사용) | 새 `calibration_version`(CALIBRATED), 빠른 미리보기 맵(`preview_scene_version`) 및 백그라운드 완료 정식 `scene_version`, 홀드아웃 재투영 오차·정물 검증 요약(실패 시 `failure_code`·`failure_message`) |
| `hmr_refine` (시연 범위·EXP-02) | LIVE 관제 중에는 I5 대여 작업이 아니라 분석 워커의 상시 스트림 루프(Tier 0 공유 메모리 입력, 3.2 지연 감시 적용)로 돌며, 출력을 I5 콜백이 아닌 I6 아바타 스트림(`avatar_pose_batch`·`avatar_asset`)으로 보낸다. I5 배치 대여는 관제 외 오프라인 재처리·실험에만 쓰며 LIVE 중에는 받지 않는다(오프라인 산출물은 `experiments/human_mesh_output`) | I6 `avatar_pose_batch`·`avatar_asset` (오프라인 배치는 MHR 메시·포즈, 관절 출처, 품질 메타데이터) |
| `hmr_appearance_update` (시연 범위·EXP-02 2단계) | 동일 World ID의 연속 관측 샘플, 이전 시각 자산(I5 배치 작업은 LIVE 중 받지 않으며, 상시 아바타 루프 `services/avatar` 에서는 `SIMAI_AVATAR_APPEARANCE=auto` 로 CUDA 에서 기본 실행돼 갱신된 `1024×1024` `albedo.png`·`surface_source.png` 를 `AVATAR_ASSET` 업로드 후 I6 `avatar_asset` 으로 알림, 5.6) | 신규 텍스처/외형 버전(`appearance_version`), 표면 출처 지도·비율(`surface_source`·`face_ratios`), 아티팩트 참조 |

VLM 을 부르는 작업(`vlm_rationale`·`incident_summary`·`vlm_zone_draft`)은 분석 워커가 VLM 호출 직전에 `face_policy` 대로 얼굴 영역을 흐리고, 이벤트 작업(`vlm_rationale`·`incident_summary`)은 보정 파라미터로 투영한 위험 구역 경계선과 대상 인물 표시를 VLM 입력 이미지 사본 위에 그려 넣는다(5.4). 외부 LLM 호출은 I5 워커가 아니라 EC2 의 Python 컨테이너(`deploy/risk-engine`) 안에서만 한다(SSAFY GMS 경유, 설정 `SIMAI_LLM_BASE_URL`·`SIMAI_LLM_MODEL`·`SIMAI_LLM_API_KEY`·`SIMAI_LLM_TIMEOUT_S` 는 EC2 `.env`, 14장). 따라서 현행 활성 I5 작업 유형은 `vlm_rationale`, `incident_summary`, `bev_floorplan`, `static_reconstruct`, `scene_regenerate`(수동 맵 재생성, S15P31A101-427·-428), `vlm_zone_draft`(현장 세팅 VLM 위험구역 초안, 마이너 제안), `calibration_ba` / `site_setup`(현장 세팅 배경 특징점 공동 BA 및 미리보기·정밀 맵 생성, 마이너 제안), `hmr_refine`(오프라인 배치), `hmr_appearance_update`(오프라인 배치)다(`tbm_report` 는 폐기 예정). `bev_floorplan` 은 CALIBRATED 가 되거나 보정 버전이 바뀌면 EC2 가 등록한다.

작업은 시간 제한(`timeout_ms`)을 두고, 넘기면 TIMED_OUT 으로 끝내며 다시 내주지 않는다. 분석 워커 실패는 VLM·정적 3D·보고서에만 영향을 준다. Tier 0 서비스 실패는 실시간 관측·World XYZ·위험 판정을 멈추고 UNSAFE_UNKNOWN이 되며, Tier 0와 EC2 사이 연결만 실패하면 판정과 노트북 경보는 유지되고 원격 전달만 지연된다(6.5).

## 12.6 REST API (I2·I3)

경로는 기본안이며, 확정본은 Spring Boot에서 생성하는 OpenAPI 문서로 관리한다.

| 메서드 | 경로 | 용도 | 권한 |
|---|---|---|---|
| POST | /api/auth/login | 로그인, JWT·갱신 토큰 발급 | 공개 |
| GET | /api/health | 현재 System Health | 열람 |
| GET | /api/world?at=\{t\} | 시각 t의 World 상태 스냅샷 | 열람 |
| GET | /api/replay?from=\{t1\}&amp;to=\{t2\} | 구간 궤적·이벤트(최소 리플레이, 골격·버전은 확장) | 열람 |
| GET · POST · PUT | /api/zones | 구역 조회·생성·새 버전(신규·편집 심각도 선택지는 `CRITICAL`·`HIGH` 2종, 8.2) | 쓰기는 관리자 |
| POST | /api/zones/commit | 실시간 관제 화면 관리자 편집 모드에서 구역(`zone`)·고소 작업면(`static_object`, 둘레 `HIGH` 구역과 `zone_id` 연결) 새 버전 저장과 구성 묶음(`config_bundle`) 발행을 한 트랜잭션으로 처리(VLM 위험구역 초안의 발행 확정 포함, 7.5·8.2, S15P31A101-397·-410·-426) | 관리자 |
| GET | /api/zones/\{zoneId\}/versions | 구역 버전 이력(리플레이는 그 시각 유효 버전) | 열람 |
| GET · POST · PUT | /api/static-objects | 정적 객체·작업면(OBB, 상판 높이) 조회·등록·새 버전(7.5) | 쓰기는 관리자 |
| GET | /api/rules | 규칙 버전 조회 | 열람 |
| POST | /api/setup-sessions | 작업 시작 전 **현장 세팅 세션 시작**(본문 `work_unit_id`, 카메라별 실측 높이 `camera_heights_m` — 생략 시 직전 세팅 높이 사용, 화면 기본 채움 값 함께 반환). 깨끗한 배경 누적 → 무마커 내부 파라미터 → 상주 MapAnything·World 자동 정의 → 인접 쌍 배경 특징점 공동 BA(V2)·정물 검증 → 빠른 미리보기 맵 + VLM 위험구역 초안 병렬 생성 실행(목표 약 1분 30초, 확인 필요, 4.3·8.1·13.4, `docs/design/site-setup-pipeline.md`) | 관리자 |
| GET | /api/setup-sessions/\{setupSessionId\} | 현장 세팅 세션 진행 단계(`COLLECTING_BACKGROUND` → `INITIAL_POSE_AND_WORLD` → `JOINT_BA_AND_VERIFY` → `PREVIEW_AND_VLM_DRAFT` → `READY_FOR_ZONES` / 백그라운드 `BAKING_TEXTURE` → `COMPLETED` / 실패 `FAILED`), 전체 진행률(%), 카메라별 배경 수집률, 생성된 `calibration_version`·`preview_scene_version`·`scene_version`·`zone_drafts`, 실패 코드·조치 안내(`failure_code`·`failure_message`) 조회 | 열람 |
| POST | /api/setup-sessions/\{setupSessionId\}/retry | 현장 세팅 실패 또는 카메라·물체 조정 후 세팅 재시도 | 관리자 |
| POST | /api/calibrations | 자동 보정 시작(하위 호환용, 신규 작업별 현장 세팅은 `/api/setup-sessions` 사용, 4.3) | 관리자 |
| GET | /api/calibrations/current | 보정 상태(UNCALIBRATED·PROVISIONAL·CALIBRATED·DRIFTED) | 열람 |
| POST | /api/calibrations/\{id\}/verifications | 독립 검증점 결과 제출(4.4) | 관리자 |
| POST | /api/calibrations/\{id\}/confirmation | 보정 결과를 새 버전으로 확정 | 관리자 |
| GET · POST | /api/scale-references | 실측 정물 목록(이름·치수·측정일) 조회·등록(4.3) | 쓰기는 관리자 |
| GET | /api/cameras/coverage-guides | 커버리지 및 인접 쌍 배경 특징점 매칭 기반 카메라 각도·물체 배치 안내(4.5) | 관리자 |
| GET | /api/floor-plans/current | BEV 평면도(보조 화면, `bev_floorplan` 결과, 4.1) | 열람 |
| POST · GET | /api/config-bundles, /api/config-bundles/\{version\} | 구성 묶음 발행, 현장 적용 상태 조회 | 발행은 관리자, 조회는 열람 |
| GET · POST | /api/events, /api/events/\{id\}/ack | 이벤트 목록(기본 `page=0`·`size=20`·최대 100, 최신 발생순, 같은 사람의 여러 규칙은 한 묶음), 확인 | 반장·관리자 |
| GET | /api/events/\{id\} | 이벤트 상세(위치·근거·관련 구역·조치 상태·VLM 근거·상향 의견) | 반장·관리자 |
| POST | /api/events/\{id\}/summary | 사고 구간 요약 요청(`incident_summary`, 5.4) | 관리자 |
| POST | /api/events/\{id\}/close | 구역 대상 이벤트 운영자 종료(본문 사유 1\~500자·요청 멱등 키). 멱등 키는 EC2 가 따로 저장해 같은 키 재요청에 처음 결과를 돌려준다. EC2 가 발급한 `close_id` 로 I1 `event_close` 를 보내고 결과 `event_close_result` 를 돌려줌(6.4) | 관리자 |
| POST | /api/events/\{id\}/escalation | VLM 상향 의견 승인·거절(본문 `assessmentId` 숫자 ID 또는 `job_id`, `decision` `APPROVED`·`REJECTED`). 거절은 `vlm_assessment` 에 기록하고 투영 `proposed_severity` 를 비우며, 승인은 `event_escalation` 기록 후 I1 `event_escalate` 로 보냄(관제 이벤트 상세 `EscalationControl` UI, 5.4, `docs/design/server-event-journal.md`) | 관리자 |
| GET | /api/scenes/current | 현재 활성(`COMMITTED`) Scene 과 glTF 서명 주소(`gltf_url`), 표면 출처 지도 서명 주소 배열(`surface_source_map_urls`, `SURFACE_SOURCE_MAP` 이 있을 때), Scene 을 만든 보정의 상태(`calibration_state`, 뷰어 ‘임시 보정 기준 배경’ 표시) | 열람 |
| GET | /api/scenes/\{sceneId\} | Scene 생성 상태(CANDIDATE·VALIDATING·COMMITTED·REJECTED) | 관리자 |
| POST | /api/scenes/rebuild · /api/scenes/regenerate | 정적 3D 재생성 요청(`rebuild`) 및 관제 화면 수동 **“맵 재생성”** 버튼 요청(`POST /api/scenes/regenerate`: 그때까지 누적된 사람 지운 배경으로 I5 `scene_regenerate` 전체 재구성 작업 등록 → 새 Scene `CANDIDATE` 생성 후 관리자 활성화, 5.3·12.5, S15P31A101-427·-428, v1.3 수정 52) | 관리자 |
| POST | /api/scenes | 관리자가 올린 glb(업로드 목적 `SCENE_GLTF`)를 Scene 후보(CANDIDATE)로 등록. PROVISIONAL·CALIBRATED 보정에만 | 관리자 |
| POST | /api/scenes/\{sceneId\}/commit · /reject | 후보 Scene 활성화(COMMITTED, 워커 생성 후보도 직접 등록과 같이 `PROVISIONAL`·`CALIBRATED` 보정에서 관리자 확인으로, 4.5, `scene_activation` 이력 기록, 이전 현재 Scene 은 `valid_to` 로 닫고 서버 재기동 없이 `/api/scenes/current`·`/topic/health` 반영, 관리 명령 `admin scene list\|activate`·작업 등록 `admin job enqueue --type static_reconstruct` 지원, `docs/design/scene-version-from-job.md`)·거절(REJECTED, 사유) | 관리자 |
| GET | /api/sites/current | 현장(MVP 하나) 조회 | 열람 |
| POST · PUT | /api/sites · /api/sites/current | 현장 등록(없을 때만)·수정(이름·원점 근거, 구성이 붙기 전이면 `site_id`) | 관리자 |
| GET · POST · PUT · DELETE | /api/work-units, /api/work-units/\{id\} | 작업 관리 목록(`?days=`, 구간 LIVE 이벤트 수·상태 `PLANNED`\|`IN_PROGRESS`\|`DONE`·활성 현장 세팅 `active_setup_session_id`·실제 시작/종료 시각 포함)·단건 조회·등록·수정·삭제. 작업마다 현장 세팅·구역 발행 뒤 확정된 대상 구역(`zone_id`), 가상 레이아웃(`layout_id`), 공종(`work_type`)을 연결한다(8.3, `docs/design/server-work-units.md`·`docs/design/work-session-flow.md`·`docs/design/site-setup-pipeline.md`) | 조회는 열람, 쓰기는 관리자 |
| POST | /api/work-units/\{id\}/start · /api/work-units/\{id\}/end | 작업 시작(`PLANNED` → `IN_PROGRESS`, 해당 작업의 현장 세팅·구역 발행이 완료된 경우에만 허용, `actual_started_at` 기록 및 관제 화면 세션 연동) · 작업 종료(`IN_PROGRESS` → `DONE`, `actual_ended_at` 기록 후 진행 구간·구역의 실제 LIVE `risk_event` 집계 사실로 EC2 Python 컨테이너가 TBM 보고서 “작업 마무리” 문단을 작성해 새 `report_version` 저장, 8.3) | 관리자 |
| GET · POST | /api/layouts | 가상 레이아웃 | 관리자 |
| GET · PUT | /api/layouts/\{id\} | 가상 레이아웃 조회·수정 | 조회는 열람, 수정은 관리자 |
| POST | /api/layouts/\{id\}/evaluate | 계획 모드 단독 평가(확장, 부록 C.4. M3 TBM 보고서는 아래 `/api/reports/tbm` 안에서 계산) | 관리자 |
| POST | /api/reports/tbm | TBM 보고서 생성 요청(본문 작업 ID `workId`, 하위 호환 `layoutId`). EC2 가 그 작업의 레이아웃으로 Python 컨테이너(`deploy/risk-engine`)에 PLANNED 계산(`sim.*`) + 구역 최근 `risk_event` 이력 요약 + 비개인정보 공종(`work_type`) + 법령 지식 그래프(`rag_node`·`rag_edge`) 하이브리드 검색·1단계 확장 + 외부 LLM 4단계 생성·검증을 요청해 `report`(작업별 새 `report_version` 추가, 머리말은 서버가 부착, `subgraph`·`steps` 포함)에 저장·반환한다(GPU·Pod 불필요, 5.4·8.3, v1.3 수정 49) | 관리자 |
| GET | /api/reports/tbm, /api/reports/tbm/\{reportId\} | 작업별 최신 보고서·버전 이력(`?workId=`) 및 단건 조회: 생성 상태·머리말·본문(`sections`)·계산 근거 표·인용 목록(`citations`)·법령 지식 그래프 서브그래프(`subgraph`: `nodes`·`edges`)·4단계 실행 상태(`steps`)·구역 최근 이벤트 요약(`zone_history`)·작업 마무리(`wrap_up_summary`)·버전 잠금 시각(`locked_at`)·본문 해시(`content_sha256`)·결재 시각(`approved_at`, UTC ms 또는 null)·관리자 계정 표시명(`approved_by_name`, 실명 아님, 표시명 없으면 login_id, 미결재는 null) 조회(PDF 는 브라우저 인쇄, 별도 내보내기 API 없음) | 열람 |
| GET · POST | /api/reports/tbm/\{reportId\}/signatures | TBM 보고서 참석자 서명·관리자 최종 결재 등록·조회(요청·응답 `signer_role`은 `PARTICIPANT`(생략 시 기본·기존 행 포함) 또는 `APPROVER`(버전당 1회), 승인자는 요청 관리자 계정(`recorded_by` = `approved_by`), 결재 시 `approved_at` 기록, 참석자 0명도 허용하되 화면 경고, 결재 후 두 역할 모두 409 `REPORT_APPROVED`, 실명 `signer_name` 과 손글씨 서명 이미지 서버 전용 저장, 역할 무관 첫 서명 시 해당 `report_version` 잠금 `locked_at` 및 본문 정규화 해시 `content_sha256` 고정·결재 시에도 같은 해시, 보고서 수정·작업 마무리 추가 시 새 버전 생성 후 재서명·재결재(과거 기록 보존), `"공인 전자서명 아님, 내부 기록용 전자 서명"` 고지, 외부 LLM 미전송, 8.3·14장, v1.3 수정 52) | 관리자 |
| POST | /api/reports/daily | 일일 일지 생성 요청(EC2 Python 컨테이너 보고서 생성, `report_kind` DAILY, `subgraph`·`steps` 포함) | 관리자 |
| GET · PATCH | /api/tickets, /api/tickets/\{id\} | 조치 티켓 조회·상태 변경 | 반장(조회는 관리자도) |
| POST | /api/uploads/sign | 사진·프레임·클립 임시 업로드 주소 | 기기·반장 |
| GET | /api/events/\{id\}/clips | 이벤트의 클립 목록(위험 구간·카메라 순, 보존 표시, 지운 클립은 삭제 시각과 함께, 주소 없음) | 관리자 |
| GET | /api/clips/\{id\} | 이벤트 클립 조회(임시 서명 주소) | 관리자 |
| PUT | /api/clips/\{id\}/retention | 클립 보존 표시 붙이기·떼기 | 관리자 |
| GET | /api/videos?from=\{t1\}&amp;to=\{t2\}&amp;camera_id= | 촬영 구간이 겹치는 상시 영상 조각 목록(주소 없음, 24시간 이내) | 관리자 |
| GET | /api/videos/\{segmentId\} | 상시 영상 조각 재생(임시 서명 주소, 조회 기록) | 관리자 |
| POST | /api/cctv/remote/tickets | 관제 화면 CCTV 원격 시청용 일회용 입장권 발급(유효 60 s, 1회 소비, `wss://<ec2>/ws/cctv/view?ticket=…`, 3.3, v1.3 수정 46) | 관리자 |
| GET | /api/push/vapid-public-key | Web Push 구독용 VAPID 공개키 | 반장 |
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
- 전송: 헤더 + 프레임을 길이 접두 방식으로 카메라별 독립 TLS over TCP 연결에 싣는다(한 카메라의 지연이 다른 카메라를 막지 않도록). GPU 인스턴스의 공개 TCP 포트(Direct TCP, 3.12)를 쓰고, TLS는 Tier 0 서비스가 직접 종료하며, 접속 시 노트북 기기 토큰으로 인증한다. 제어 메시지(local_alert, alarm_stop, health, site_ack, laptop_heartbeat, clock_sync_request, clock_sync_response)는 같은 방식으로 인증한 WebSocket(wss)으로 주고받는다.
- 서버 확인(공개키 고정): Tier 0 TLS 키는 한 번 만들어 Tier 0 저장 위치의 비공개 폴더(3.12)에 둔다. 저장 위치가 컨테이너 디스크라 인스턴스와 함께 사라지면 새 키의 지문을 노트북에 다시 고정한다. 노트북은 그 공개키 지문만 신뢰한다(또는 팀 사설 CA). 인증서 검증을 끄는 구현은 금지한다(14장).
- 송신 큐: 노트북은 카메라별 송신 큐에 상한을 두고 소켓 송신 버퍼를 작게 잡는다. 큐가 넘치면 다음 키프레임까지 버리고, 버린 프레임은 노트북 쪽 skipped 로 따로 센다. 이것은 전송 손실(gateway_to_runpod_loss)이나 Tier 0 의 skipped_by_policy 와 다르다.
- 버림 알림(SKIP_NOTICE): 노트북은 버린 `frame_sequence` 범위를 연결 기록 형식 안의 알림으로 Tier 0 에 알린다(계약 스키마 밖, 형식 정본은 `gateway/relay` 의 `wire.py`). 지금 epoch 의 버린 구간은 겹치거나 맞닿은 것끼리 합친 구간 집합으로 들고 있다가 새 연결마다 다시 보낸다(같은 알림을 여러 번 받아도 늘지 않는다). Tier 0 는 알린 범위의 번호 빈칸을 손실로 세지 않고 `health.cameras[].frames.laptop_skipped` 로 보고한다. 구간 수가 상한을 넘거나 재시작으로 버림 기록이 잘려 손실인지 버림인지 모르는 빈칸은 `unattributed_gap`(구분 불가)으로 따로 세고, 처음 보는 epoch 의 첫 번호 앞은 세지 않는다(지어내지 않음).
- 누락: frame_sequence 빈 구간은 노트북 → RunPod 구간의 손실(gateway_to_runpod_loss)로 기록하고 재요청하지 않는다(실시간 우선). 버림 알림 범위(laptop_skipped)와 구분 불가 빈칸(unattributed_gap)은 손실에서 뺀다. 카메라 → 노트북 구간은 RTSP over TCP라 손실이 번호 빈칸이 아니라 도착 간격의 끊김으로 나타나므로, 프레임 도착 간격과 RTP 타임스탬프 간격으로 camera_to_gateway_gap을 잰다. 정책상 건너뛴 프레임(skipped_by_policy)은 손실이 아니다. 카메라 내부에서 버린 프레임은 관측할 수 없으므로 상류 구간의 실제 손실률은 확정할 수 없다. 이 값들은 health와 G0에 따로 보고한다.
- 역순·지연 도착: 시간 보간 창(예: 200ms) 안이면 정렬해 쓰고, 창을 넘긴 프레임은 버리고 기록만 한다.
- 재접속: 카메라 RTSP 스트림이 다시 연결되면 새 stream_epoch로 시작한다(노트북 → Tier 0 TLS 연결만 바뀐 전송 재접속은 위 규칙대로 같은 epoch·번호를 이어 쓴다). 어느 재접속이든 노트북은 연결 직후 코덱 정보(`codec_info`: 코덱 종류, SPS/PPS, PTS timebase)를 먼저 보낸 뒤 키프레임부터 전송한다. 코덱 정보와 키프레임이 모두 도착할 때까지 그 카메라는 관측 없음으로 처리한다. 핵심 요구는 연결 개수가 아니라 프레임과 시각이 잘못 결합되지 않고 카메라별 최신성이 유지되는 것이다.
- 촬영 시각은 3.9의 촬영 시각 추정과 촬영 시각 계약을 따른다.
