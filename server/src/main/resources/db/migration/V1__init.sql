-- simAI V1: MVP·M1 테이블 (S15P31A101-211)
-- 설계: docs/design/db-tables.md · db-erd.md · db-verification.md 의 정의를 옮긴 첫 스키마다.
-- 적용한 뒤에는 이 파일을 고치지 않는다(Flyway 체크섬). 바꿀 것은 설계 문서를 고치고 새 마이그레이션(V2__…)으로 더한다.
-- 기획서 11장(데이터)·11.4(스키마 원칙)·11.5(보관)·16장. 시각은 timestamptz(UTC), PostGIS 는 SRID 0(World 로컬 미터).
-- 테이블 43개(MVP 34 · M1 9). M2 이후·확장 테이블과 그 FK 는 다음 마이그레이션에서 더한다.

-- 확장: TimescaleDB 는 shared_preload_libraries 에 있어야 한다(timescale/timescaledb-ha 이미지는 기본 포함).
-- PostGIS 는 신뢰 확장이 아니므로 Flyway 계정에 권한이 없으면 DB 관리자가 먼저 만든다.
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;

-- A. app_user (MVP, 일반): 관제 화면·PWA 계정. 역할 3종
CREATE TABLE app_user (
    user_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    login_id text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('ADMIN', 'FOREMAN', 'VIEWER')),
    display_label text,
    created_at timestamptz NOT NULL,
    disabled_at timestamptz,
    PRIMARY KEY (user_id)
);

-- A. refresh_token (MVP, 일반): JWT 갱신 토큰
CREATE TABLE refresh_token (
    token_id uuid NOT NULL,
    user_id bigint NOT NULL,
    token_hash text NOT NULL UNIQUE,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    PRIMARY KEY (token_id)
);

-- A. device (MVP, 일반): 현장 노트북(확장: 워치) 기기와 토큰
CREATE TABLE device (
    device_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('LAPTOP', 'WATCH')),
    token_hash text NOT NULL,
    issued_at timestamptz NOT NULL,
    rotated_at timestamptz,
    revoked_at timestamptz,
    last_heartbeat_at timestamptz,
    PRIMARY KEY (device_id)
);

-- A. device_heartbeat (MVP, hypertable): 노트북 → EC2 1초 하트비트 기록. remote_state 판단 근거
CREATE TABLE device_heartbeat (
    received_at timestamptz NOT NULL,
    device_id text NOT NULL,
    sent_at timestamptz NOT NULL,
    heartbeat_seq bigint NOT NULL,
    tier0_control text NOT NULL CHECK (tier0_control IN ('CONNECTED', 'DISCONNECTED')),
    tier0_last_message_at timestamptz,
    tier0_video_cameras text[] NOT NULL,
    ec2_state text,
    site_display text NOT NULL CHECK (site_display IN ('NORMAL', 'CONTROL_HALTED', 'REMOTE_FAULT', 'AI_DELAY')),
    alarm jsonb,
    cameras jsonb,
    ec2_last_response_at timestamptz,
    PRIMARY KEY (received_at, device_id)
);

-- A. worker (MVP, 일반): 분석 워커와 토큰
CREATE TABLE worker (
    worker_id text NOT NULL,
    token_hash text NOT NULL,
    job_types text[] NOT NULL,
    issued_at timestamptz NOT NULL,
    revoked_at timestamptz,
    last_seen_at timestamptz,
    PRIMARY KEY (worker_id)
);

-- B. site (MVP, 일반): 현장과 World 좌표계 정의
CREATE TABLE site (
    site_id text NOT NULL,
    name text NOT NULL,
    world_origin jsonb,
    established_calibration_version text,
    current_floor_plan_id bigint,
    migrated_from jsonb,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id)
);

-- B. camera (MVP, 일반): 카메라 장비 정보
CREATE TABLE camera (
    camera_id text NOT NULL,
    site_id text NOT NULL,
    model text NOT NULL,
    hardware_version text,
    firmware_version text,
    width integer,
    height integer,
    nominal_fps real,
    installed_at timestamptz,
    removed_at timestamptz,
    PRIMARY KEY (camera_id)
);

-- B. zone (MVP, 버전): 위험 구역. 수정은 새 버전
CREATE TABLE zone (
    zone_id text NOT NULL,
    zone_version integer NOT NULL,
    site_id text NOT NULL,
    name text NOT NULL,
    zone_kind text NOT NULL,
    eval_type text NOT NULL CHECK (eval_type IN ('GROUND_FOOTPRINT', 'VOLUME_3D')),
    polygon geometry(Polygon, 0) NOT NULL CHECK (ST_SRID(polygon) = 0),
    z_min_m real,
    z_max_m real,
    severity text NOT NULL CHECK (severity IN ('CRITICAL', 'HIGH', 'CAUTION', 'INFO')),
    requirements jsonb,
    has_guardrail boolean,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    created_by bigint,
    PRIMARY KEY (zone_id, zone_version)
);

-- B. static_object (MVP, 버전): 등록 작업면·정적 객체(Operational Geometry)
CREATE TABLE static_object (
    object_id text NOT NULL,
    object_version integer NOT NULL,
    site_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('WORK_SURFACE', 'OBSTACLE')),
    obb jsonb NOT NULL,
    top_height_m real,
    name text,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    created_by bigint,
    PRIMARY KEY (object_id, object_version)
);

-- B. rule_version (MVP, 버전): Risk Engine 규칙 임계값 세트. 규칙 등록부에서 자동 생성한 Flyway 시드로 채운다
CREATE TABLE rule_version (
    rule_id text NOT NULL,
    rule_version integer NOT NULL,
    profile text NOT NULL CHECK (profile IN ('OPERATION', 'CLASSROOM_TEST')),
    thresholds jsonb NOT NULL,
    default_severity text CHECK (default_severity IN ('CRITICAL', 'HIGH', 'CAUTION', 'INFO')),
    legal_basis text,
    reads text[] NOT NULL,
    stage text,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    PRIMARY KEY (rule_id, rule_version)
);

-- B. config_bundle (MVP, 버전): 구성 묶음 발행과 현장 적용
CREATE TABLE config_bundle (
    config_version text NOT NULL,
    previous_config_version text,
    site_id text NOT NULL,
    issued_at timestamptz NOT NULL,
    issued_by bigint,
    bundle_uri text NOT NULL,
    bundle_hash text NOT NULL,
    components jsonb NOT NULL,
    calibration_version text NOT NULL,
    applied_at timestamptz,
    PRIMARY KEY (config_version)
);

-- B. config_bundle_item (MVP, 일반): 묶음에 들어간 구역·규칙·형상 버전 목록
CREATE TABLE config_bundle_item (
    config_version text NOT NULL,
    item_type text NOT NULL CHECK (item_type IN ('ZONE', 'RULE', 'STATIC_OBJECT')),
    item_id text NOT NULL,
    item_version integer NOT NULL,
    PRIMARY KEY (config_version, item_type, item_id)
);

-- B. config_ack (MVP, 일반): Tier 0 의 구성 묶음 적용·거부 응답
CREATE TABLE config_ack (
    ack_msg_id uuid NOT NULL,
    config_version text NOT NULL,
    gateway_id text NOT NULL,
    result text NOT NULL CHECK (result IN ('APPLIED', 'REJECTED')),
    bundle_hash text NOT NULL,
    applied_at timestamptz,
    reject_reason text,
    reject_detail text,
    active_config_version text,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (ack_msg_id)
);

-- B. tier0_endpoint (MVP, 일반): Tier 0 Pod 엔드포인트 등록 이력(최신 행을 노트북에 줌)
CREATE TABLE tier0_endpoint (
    endpoint_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    gateway_id text NOT NULL,
    public_ip inet NOT NULL,
    ports jsonb NOT NULL,
    tls_public_key_sha256 text NOT NULL,
    registered_at timestamptz NOT NULL,
    pod_id text,
    datacenter text,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (endpoint_id)
);

-- C. calibration_version (MVP, 버전): 보정 버전(상태·스케일 근거·검증 요약)
CREATE TABLE calibration_version (
    calibration_version text NOT NULL,
    site_id text NOT NULL,
    previous_calibration_version text,
    state text NOT NULL CHECK (state IN ('UNCALIBRATED', 'PROVISIONAL', 'CALIBRATED', 'DRIFTED')),
    provider_source text NOT NULL CHECK (provider_source IN ('G1_AUTO', 'DATASET_GT', 'SIM', 'MODEL_ESTIMATE')),
    provider_version text,
    reason text NOT NULL,
    effective_at timestamptz NOT NULL,
    calibration_ref text NOT NULL,
    calibration_hash text NOT NULL,
    scale_basis text,
    scale_cross_check_basis text[],
    validation jsonb,
    operating_reprojection jsonb,
    source_job_id text,
    confirmed_by bigint,
    confirmed_at timestamptz,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (calibration_version)
);

-- C. calibration_camera (MVP, 일반): 보정 버전의 카메라별 K·왜곡·R·t
CREATE TABLE calibration_camera (
    calibration_version text NOT NULL,
    camera_id text NOT NULL,
    image_size integer[2] NOT NULL,
    k_matrix jsonb NOT NULL,
    distortion_model text NOT NULL,
    distortion_coeffs double precision[] NOT NULL,
    r_matrix jsonb NOT NULL,
    t_vector double precision[3] NOT NULL,
    camera_height_m real,
    reprojection_median_px real,
    reprojection_p95_px real,
    PRIMARY KEY (calibration_version, camera_id)
);

-- C. calibration_run (M1, 일반): 자동 보정 실행 요청(걷기 보정)
CREATE TABLE calibration_run (
    run_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    site_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('WALKING', 'SOLVING', 'CANDIDATE', 'FAILED', 'CONFIRMED')),
    requested_by bigint,
    requested_at timestamptz NOT NULL,
    walk_started_at timestamptz,
    walk_ended_at timestamptz,
    obs2d_archive_uri text,
    job_id text,
    result_calibration_version text,
    failure_reason text,
    PRIMARY KEY (run_id)
);

-- C. calibration_verification (M1, 일반): 키를 아는 사람 3~5곳 검산 결과
CREATE TABLE calibration_verification (
    verification_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    calibration_version text NOT NULL,
    checkpoint_count integer NOT NULL,
    max_pairwise_position_disagreement_m real,
    max_head_height_error_m real,
    max_position_error_m real,
    floor_plane_disagreement_m real,
    floor_coverage_ratio real,
    passed boolean NOT NULL,
    detail jsonb,
    submitted_by bigint,
    submitted_at timestamptz NOT NULL,
    PRIMARY KEY (verification_id)
);

-- C. scale_reference (M1, 일반): 미리 치수를 잰 시연용 안전 장비 목록
CREATE TABLE scale_reference (
    scale_reference_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    site_id text,
    name text NOT NULL,
    kind text NOT NULL,
    height_m real NOT NULL,
    width_m real,
    depth_m real,
    measured_at date NOT NULL,
    created_by bigint,
    retired_at timestamptz,
    PRIMARY KEY (scale_reference_id)
);

-- C. calibration_scale_check (M1, 일반): 보정에서 실측 정물별로 잰 크기와 오차
CREATE TABLE calibration_scale_check (
    calibration_version text NOT NULL,
    scale_reference_id bigint NOT NULL,
    measured_height_m real NOT NULL,
    error_ratio real NOT NULL,
    used_for_scale boolean NOT NULL,
    mismatch_warning boolean NOT NULL,
    PRIMARY KEY (calibration_version, scale_reference_id)
);

-- C. coverage_guide (MVP, 일반): 커버리지 계산 결과와 카메라별 안내 문구
CREATE TABLE coverage_guide (
    guide_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    calibration_version text,
    camera_id text,
    message text NOT NULL,
    metrics jsonb,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (guide_id)
);

-- C. floor_plan (MVP, 일반): BEV 평면도(보조 화면)
CREATE TABLE floor_plan (
    floor_plan_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    site_id text NOT NULL,
    calibration_version text NOT NULL,
    scene_version text,
    image_key text,
    status text NOT NULL CHECK (status IN ('PENDING', 'READY', 'FAILED')),
    job_id text,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (floor_plan_id)
);

-- C. scene_version (MVP, 버전): 정적 3D Scene. 테이블은 MVP(버전 ID 참조), 생성은 M3
CREATE TABLE scene_version (
    scene_version text NOT NULL,
    site_id text NOT NULL,
    calibration_version text NOT NULL,
    state text NOT NULL CHECK (state IN ('CANDIDATE', 'VALIDATING', 'COMMITTED', 'REJECTED')),
    gltf_key text,
    quality jsonb,
    input_provenance jsonb,
    is_upper_bound boolean NOT NULL,
    job_id text,
    requested_by bigint,
    committed_at timestamptz,
    rejected_reason text,
    valid_from timestamptz,
    valid_to timestamptz,
    PRIMARY KEY (scene_version)
);

-- D. world_track_state (MVP, hypertable): World Track 상태 10 Hz(최소 리플레이 근거)
CREATE TABLE world_track_state (
    observation_at timestamptz NOT NULL,
    world_id text NOT NULL,
    msg_id uuid NOT NULL,
    gateway_id text NOT NULL,
    t_last_valid timestamptz NOT NULL,
    observation_state text NOT NULL,
    track_lifecycle text NOT NULL,
    identity_confidence text NOT NULL,
    identity_revision integer NOT NULL,
    split_from text,
    aliases text[],
    frame_age_ms integer,
    position geometry(Point, 0) NOT NULL CHECK (ST_SRID(position) = 0),
    position_xy_sigma real[2] NOT NULL,
    pelvis_xyz real[3],
    ground_anchor_xyz real[3],
    ground_anchor_sigma real[3],
    surface_source text,
    support_surface_id text,
    support_surface_validity text NOT NULL,
    contact_validity text NOT NULL,
    z_validity text NOT NULL,
    head_z real,
    head_z_sigma real,
    head_z_validity text,
    velocity real[3],
    cameras text[] NOT NULL,
    calibration_version text NOT NULL,
    scene_version text NOT NULL,
    config_version text,
    input_provenance jsonb,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (observation_at, world_id)
);

-- D. skeleton_3d (MVP, hypertable): 3D 골격 5~10 Hz(리플레이 확장·뷰어)
CREATE TABLE skeleton_3d (
    observation_at timestamptz NOT NULL,
    world_id text NOT NULL,
    joint_layout text NOT NULL,
    joints real[][] NOT NULL,
    visible boolean[] NOT NULL,
    validity text[] NOT NULL,
    joint_source text[] NOT NULL,
    PRIMARY KEY (observation_at, world_id)
);

-- D. system_health (MVP, hypertable): Tier 0 health 기록(카메라별 값은 JSONB)
CREATE TABLE system_health (
    reported_at timestamptz NOT NULL,
    gateway_id text NOT NULL,
    msg_id uuid NOT NULL,
    system_health text NOT NULL,
    t_latest_global timestamptz,
    pipeline_lag_ms integer,
    pipeline_lag_lower_bound_ms integer,
    capture_to_risk_ms jsonb,
    calibration_state text NOT NULL,
    calibration_version text,
    config_version text,
    config_sync text,
    gateway_link text,
    cameras jsonb NOT NULL,
    tier0_warnings jsonb,
    ec2_world_state_drops jsonb,
    gpu_util real,
    gpu_memory_used_ratio real,
    active_site_alerts jsonb,
    active_site_alerts_as_of timestamptz,
    laptop_clock_offset jsonb,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (reported_at, gateway_id)
);

-- D. system_metric (MVP, hypertable): 구간별 지연·FPS·큐·손실 지표(긴 형식)
CREATE TABLE system_metric (
    measured_at timestamptz NOT NULL,
    source_id text NOT NULL,
    metric text NOT NULL,
    camera_id text NOT NULL,
    value double precision NOT NULL,
    unit text,
    labels jsonb,
    PRIMARY KEY (measured_at, source_id, metric, camera_id)
);

-- D. observation_summary (M1, 일반): 2D 관측 카메라별 요약(원본은 Tier 0 저장 위치의 Parquet)
CREATE TABLE observation_summary (
    summary_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    gateway_id text NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    calibration_version text,
    archive_uri text,
    cameras jsonb NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (summary_id)
);

-- E. risk_event (MVP, 일반): LIVE·REPLAY 이벤트 현재 상태(전이 반영 결과). PLANNED·WHAT_IF 는 sim
CREATE TABLE risk_event (
    event_id uuid NOT NULL,
    source text NOT NULL CHECK (source IN ('LIVE', 'REPLAY')),
    gateway_id text,
    rule_id text NOT NULL,
    rule_version integer NOT NULL,
    lifecycle text NOT NULL CHECK (lifecycle IN ('RAISED', 'UPDATED', 'RESOLVED')),
    verification text NOT NULL,
    severity text NOT NULL,
    confidence text NOT NULL,
    needs_check boolean NOT NULL,
    confidence_reasons text[],
    world_ids text[] NOT NULL,
    zone_id text,
    metrics jsonb NOT NULL,
    rationale text,
    proposed_severity text,
    resume_count integer NOT NULL,
    event_sequence integer,
    config_version text,
    calibration_version text,
    scene_version text,
    raised_at timestamptz,
    updated_at timestamptz,
    resolved_at timestamptz,
    delivery text,
    related_event_id uuid,
    resolution jsonb,
    evidence_snapshot jsonb,
    retention_hold boolean NOT NULL,
    PRIMARY KEY (event_id),
    CONSTRAINT risk_event_event_source_key UNIQUE (event_id, source)
);

-- E. risk_event_update (MVP, 일반): Tier 0 전이 기록(추가만). (event_id, event_sequence) 로 중복 제거
CREATE TABLE risk_event_update (
    event_id uuid NOT NULL,
    event_sequence integer NOT NULL,
    msg_id uuid NOT NULL UNIQUE,
    gateway_id text NOT NULL,
    journal_offset bigint NOT NULL,
    transition text NOT NULL CHECK (transition IN ('RAISED', 'UPDATED', 'RESOLVED', 'ACKNOWLEDGED')),
    occurred_at timestamptz NOT NULL,
    observation_at timestamptz NOT NULL,
    evaluated_at timestamptz NOT NULL,
    rule_id text NOT NULL,
    rule_version integer NOT NULL,
    config_version text NOT NULL,
    calibration_version text NOT NULL,
    scene_version text NOT NULL,
    source text NOT NULL CHECK (source IN ('LIVE')),
    severity text NOT NULL,
    confidence text NOT NULL,
    verification text NOT NULL,
    needs_check boolean NOT NULL,
    confidence_reasons text[],
    world_ids text[] NOT NULL,
    zone_id text,
    metrics jsonb NOT NULL,
    evidence_ref uuid,
    resent boolean NOT NULL,
    resumed boolean NOT NULL,
    related_event_id uuid,
    ack_id uuid,
    close_id uuid,
    escalation_id uuid,
    delivery_mode text NOT NULL CHECK (delivery_mode IN ('REALTIME', 'DELAYED_ACTIVE', 'AUDIT_ONLY')),
    received_at timestamptz NOT NULL,
    PRIMARY KEY (event_id, event_sequence),
    CONSTRAINT risk_event_update_journal_key UNIQUE (gateway_id, journal_offset)
);

-- E. event_ack (MVP, 일반): 운영자 확인 기록(누가·어디서·어느 상태)
CREATE TABLE event_ack (
    ack_id uuid NOT NULL,
    event_id uuid NOT NULL,
    target_event_sequence integer,
    channel text NOT NULL CHECK (channel IN ('CONTROL_ROOM', 'FOREMAN_PWA', 'SITE_BUTTON')),
    actor_role text NOT NULL CHECK (actor_role IN ('ADMIN', 'FOREMAN', 'SITE')),
    actor_user_id bigint,
    actor_device_id text,
    acked_at timestamptz NOT NULL,
    journal_event_sequence integer,
    PRIMARY KEY (ack_id)
);

-- E. event_close (MVP, 일반): 관리자의 구역 대상 이벤트 종료 명령과 결과
CREATE TABLE event_close (
    close_id uuid NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    event_id uuid NOT NULL,
    actor_user_id bigint NOT NULL,
    requested_at timestamptz NOT NULL,
    reason text NOT NULL,
    result text CHECK (result IN ('ACCEPTED', 'DUPLICATE', 'REJECTED')),
    reject_reason text,
    event_sequence integer,
    decided_at timestamptz,
    PRIMARY KEY (close_id)
);

-- E. journal_cursor (MVP, 일반): Tier 0 인스턴스별 저널 수신 상태
CREATE TABLE journal_cursor (
    gateway_id text NOT NULL,
    acked_offset bigint NOT NULL,
    resend_from_offset bigint,
    sync_mode boolean NOT NULL,
    last_snapshot_at timestamptz,
    last_snapshot_offset bigint,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (gateway_id)
);

-- E. media_clip (MVP, 일반): 이벤트 클립 기록(카메라 하나 × 위험 구간 하나)
CREATE TABLE media_clip (
    clip_id uuid NOT NULL,
    event_id uuid NOT NULL,
    gateway_id text NOT NULL,
    event_sequence integer NOT NULL,
    resume_count integer NOT NULL,
    rule_id text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('CRITICAL', 'HIGH')),
    source text NOT NULL CHECK (source IN ('LIVE')),
    camera_id text NOT NULL,
    stream_epochs integer[] NOT NULL,
    anchor_capture_at timestamptz NOT NULL,
    requested_start_at timestamptz NOT NULL,
    requested_end_at timestamptz NOT NULL,
    clip_start_capture_at timestamptz NOT NULL,
    clip_end_capture_at timestamptz NOT NULL,
    frame_count integer NOT NULL,
    starts_with_keyframe boolean NOT NULL,
    truncated_start boolean NOT NULL,
    truncated_end boolean NOT NULL,
    has_gaps boolean NOT NULL,
    content_type text NOT NULL,
    container text NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 text NOT NULL,
    face_redaction text NOT NULL CHECK (face_redaction IN ('NONE', 'BLURRED_BEFORE_EXPORT')),
    redactor_version text,
    clip_created_at timestamptz NOT NULL,
    delete_after timestamptz NOT NULL,
    retention_hold boolean NOT NULL,
    object_key text NOT NULL,
    uploaded_at timestamptz NOT NULL,
    spooled boolean,
    upload_attempts integer,
    received_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (clip_id),
    CONSTRAINT media_clip_clip_span_key UNIQUE (event_id, resume_count, camera_id)
);

-- E. media_clip_retention (MVP, 일반): 보존 표시 이력(CRITICAL 자동, 관리자 붙이기·떼기)
CREATE TABLE media_clip_retention (
    retention_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    clip_id uuid NOT NULL,
    event_id uuid NOT NULL,
    gateway_id text,
    resume_count integer,
    severity text,
    retention_hold boolean NOT NULL,
    reason text NOT NULL CHECK (reason IN ('CRITICAL_AUTO', 'ADMIN_HOLD', 'ADMIN_RELEASE')),
    source text,
    actor_user_id bigint,
    issued_at timestamptz NOT NULL,
    PRIMARY KEY (retention_id)
);

-- E. clip_access_log (MVP, 일반): 클립 열람 기록
CREATE TABLE clip_access_log (
    access_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    clip_id uuid NOT NULL,
    user_id bigint NOT NULL,
    accessed_at timestamptz NOT NULL,
    signed_url_expires_at timestamptz NOT NULL,
    PRIMARY KEY (access_id)
);

-- E. upload_grant (MVP, 일반): 임시 업로드 주소 발급 기록(올라온 키 검증)
CREATE TABLE upload_grant (
    grant_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    object_key text NOT NULL UNIQUE,
    purpose text NOT NULL CHECK (purpose IN ('EVENT_FRAME', 'EVENT_CLIP', 'TICKET_PHOTO')),
    requested_by_device text,
    requested_by_user bigint,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    PRIMARY KEY (grant_id)
);

-- F. notification (M1, 일반): 실제 알림 발송 기록. LIVE 이벤트만
CREATE TABLE notification (
    notification_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    event_id uuid NOT NULL,
    source text NOT NULL CHECK (source IN ('LIVE')),
    event_sequence integer NOT NULL,
    channel text NOT NULL CHECK (channel IN ('WEB_PUSH', 'WATCH')),
    subscription_id bigint,
    target_user_id bigint,
    severity text NOT NULL CHECK (severity IN ('CRITICAL', 'HIGH')),
    summary text NOT NULL,
    delivery text NOT NULL CHECK (delivery IN ('REALTIME', 'DELAYED_ACTIVE')),
    observation_at timestamptz,
    sent_at timestamptz,
    receipt_at timestamptz,
    acked_at timestamptz,
    status text NOT NULL CHECK (status IN ('QUEUED', 'SENT', 'FAILED', 'EXPIRED')),
    error text,
    PRIMARY KEY (notification_id)
);

-- F. push_subscription (M1, 일반): Web Push 구독
CREATE TABLE push_subscription (
    subscription_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    user_id bigint NOT NULL,
    endpoint text NOT NULL UNIQUE,
    p256dh text NOT NULL,
    auth text NOT NULL,
    user_agent text,
    created_at timestamptz NOT NULL,
    revoked_at timestamptz,
    last_success_at timestamptz,
    PRIMARY KEY (subscription_id)
);

-- F. test_push (M1, 일반): 시험 푸시(RiskEvent 아님, notification 에 넣지 않음)
CREATE TABLE test_push (
    test_push_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    requested_by bigint NOT NULL,
    subscription_id bigint,
    sent_at timestamptz,
    receipt_at timestamptz,
    PRIMARY KEY (test_push_id)
);

-- F. push_receipt (M1, 일반): Service Worker 수신 확인 원본
CREATE TABLE push_receipt (
    receipt_id bigint GENERATED ALWAYS AS IDENTITY NOT NULL,
    notification_id bigint,
    test_push_id bigint,
    subscription_id bigint,
    received_at timestamptz NOT NULL,
    reported_at timestamptz NOT NULL,
    PRIMARY KEY (receipt_id)
);

-- G. analysis_job (MVP, 일반): I5 작업(재할당돼도 같은 job_id)
CREATE TABLE analysis_job (
    job_id text NOT NULL,
    job_type text NOT NULL,
    priority text NOT NULL CHECK (priority IN ('VLM_FAST', 'REPORT', 'BACKGROUND')),
    status text NOT NULL CHECK (status IN ('QUEUED', 'LEASED', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED')),
    attempt integer NOT NULL,
    max_attempts integer NOT NULL,
    timeout_ms integer NOT NULL,
    input jsonb NOT NULL,
    result jsonb,
    error jsonb,
    event_id uuid,
    requested_by bigint,
    created_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (job_id)
);

-- G. job_lease (MVP, 일반): 작업 대여(한 시점에 유효한 대여 하나)
CREATE TABLE job_lease (
    lease_id uuid NOT NULL,
    job_id text NOT NULL,
    worker_id text NOT NULL,
    attempt integer NOT NULL,
    leased_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    heartbeat_interval_ms integer NOT NULL,
    last_heartbeat_at timestamptz,
    progress real,
    status text NOT NULL CHECK (status IN ('ACTIVE', 'SUBMITTED', 'EXPIRED', 'RELEASED', 'REJECTED')),
    reject_reason text,
    consume_attempt boolean,
    submitted_at timestamptz,
    PRIMARY KEY (lease_id)
);

-- 외래 키(테이블을 모두 만든 뒤 건다. site ↔ floor_plan 처럼 서로 가리키는 쌍이 있다)
ALTER TABLE refresh_token ADD CONSTRAINT refresh_token_user_id_fkey FOREIGN KEY (user_id) REFERENCES app_user (user_id);
ALTER TABLE device_heartbeat ADD CONSTRAINT device_heartbeat_device_id_fkey FOREIGN KEY (device_id) REFERENCES device (device_id);
ALTER TABLE site ADD CONSTRAINT site_established_calibration_version_fkey FOREIGN KEY (established_calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE site ADD CONSTRAINT site_current_floor_plan_id_fkey FOREIGN KEY (current_floor_plan_id) REFERENCES floor_plan (floor_plan_id);
ALTER TABLE camera ADD CONSTRAINT camera_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE zone ADD CONSTRAINT zone_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE zone ADD CONSTRAINT zone_created_by_fkey FOREIGN KEY (created_by) REFERENCES app_user (user_id);
ALTER TABLE static_object ADD CONSTRAINT static_object_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE static_object ADD CONSTRAINT static_object_created_by_fkey FOREIGN KEY (created_by) REFERENCES app_user (user_id);
ALTER TABLE config_bundle ADD CONSTRAINT config_bundle_previous_config_version_fkey FOREIGN KEY (previous_config_version) REFERENCES config_bundle (config_version);
ALTER TABLE config_bundle ADD CONSTRAINT config_bundle_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE config_bundle ADD CONSTRAINT config_bundle_issued_by_fkey FOREIGN KEY (issued_by) REFERENCES app_user (user_id);
ALTER TABLE config_bundle ADD CONSTRAINT config_bundle_calibration_version_fkey FOREIGN KEY (calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE config_bundle_item ADD CONSTRAINT config_bundle_item_config_version_fkey FOREIGN KEY (config_version) REFERENCES config_bundle (config_version);
ALTER TABLE config_ack ADD CONSTRAINT config_ack_config_version_fkey FOREIGN KEY (config_version) REFERENCES config_bundle (config_version);
ALTER TABLE calibration_version ADD CONSTRAINT calibration_version_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE calibration_version ADD CONSTRAINT calibration_version_previous_calibration_version_fkey FOREIGN KEY (previous_calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE calibration_version ADD CONSTRAINT calibration_version_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES analysis_job (job_id);
ALTER TABLE calibration_version ADD CONSTRAINT calibration_version_confirmed_by_fkey FOREIGN KEY (confirmed_by) REFERENCES app_user (user_id);
ALTER TABLE calibration_camera ADD CONSTRAINT calibration_camera_calibration_version_fkey FOREIGN KEY (calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE calibration_camera ADD CONSTRAINT calibration_camera_camera_id_fkey FOREIGN KEY (camera_id) REFERENCES camera (camera_id);
ALTER TABLE calibration_run ADD CONSTRAINT calibration_run_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE calibration_run ADD CONSTRAINT calibration_run_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES app_user (user_id);
ALTER TABLE calibration_run ADD CONSTRAINT calibration_run_job_id_fkey FOREIGN KEY (job_id) REFERENCES analysis_job (job_id);
ALTER TABLE calibration_run ADD CONSTRAINT calibration_run_result_calibration_version_fkey FOREIGN KEY (result_calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE calibration_verification ADD CONSTRAINT calibration_verification_calibration_version_fkey FOREIGN KEY (calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE calibration_verification ADD CONSTRAINT calibration_verification_submitted_by_fkey FOREIGN KEY (submitted_by) REFERENCES app_user (user_id);
ALTER TABLE scale_reference ADD CONSTRAINT scale_reference_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE scale_reference ADD CONSTRAINT scale_reference_created_by_fkey FOREIGN KEY (created_by) REFERENCES app_user (user_id);
ALTER TABLE calibration_scale_check ADD CONSTRAINT calibration_scale_check_calibration_version_fkey FOREIGN KEY (calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE calibration_scale_check ADD CONSTRAINT calibration_scale_check_scale_reference_id_fkey FOREIGN KEY (scale_reference_id) REFERENCES scale_reference (scale_reference_id);
ALTER TABLE coverage_guide ADD CONSTRAINT coverage_guide_calibration_version_fkey FOREIGN KEY (calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE coverage_guide ADD CONSTRAINT coverage_guide_camera_id_fkey FOREIGN KEY (camera_id) REFERENCES camera (camera_id);
ALTER TABLE floor_plan ADD CONSTRAINT floor_plan_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE floor_plan ADD CONSTRAINT floor_plan_calibration_version_fkey FOREIGN KEY (calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE floor_plan ADD CONSTRAINT floor_plan_scene_version_fkey FOREIGN KEY (scene_version) REFERENCES scene_version (scene_version);
ALTER TABLE floor_plan ADD CONSTRAINT floor_plan_job_id_fkey FOREIGN KEY (job_id) REFERENCES analysis_job (job_id);
ALTER TABLE scene_version ADD CONSTRAINT scene_version_site_id_fkey FOREIGN KEY (site_id) REFERENCES site (site_id);
ALTER TABLE scene_version ADD CONSTRAINT scene_version_calibration_version_fkey FOREIGN KEY (calibration_version) REFERENCES calibration_version (calibration_version);
ALTER TABLE scene_version ADD CONSTRAINT scene_version_job_id_fkey FOREIGN KEY (job_id) REFERENCES analysis_job (job_id);
ALTER TABLE scene_version ADD CONSTRAINT scene_version_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES app_user (user_id);
ALTER TABLE risk_event ADD CONSTRAINT risk_event_related_event_id_fkey FOREIGN KEY (related_event_id) REFERENCES risk_event (event_id);
ALTER TABLE risk_event_update ADD CONSTRAINT risk_event_update_event_id_fkey FOREIGN KEY (event_id) REFERENCES risk_event (event_id);
ALTER TABLE risk_event_update ADD CONSTRAINT risk_event_update_ack_id_fkey FOREIGN KEY (ack_id) REFERENCES event_ack (ack_id);
ALTER TABLE risk_event_update ADD CONSTRAINT risk_event_update_close_id_fkey FOREIGN KEY (close_id) REFERENCES event_close (close_id);
ALTER TABLE event_ack ADD CONSTRAINT event_ack_event_id_fkey FOREIGN KEY (event_id) REFERENCES risk_event (event_id);
ALTER TABLE event_ack ADD CONSTRAINT event_ack_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES app_user (user_id);
ALTER TABLE event_ack ADD CONSTRAINT event_ack_actor_device_id_fkey FOREIGN KEY (actor_device_id) REFERENCES device (device_id);
ALTER TABLE event_close ADD CONSTRAINT event_close_event_id_fkey FOREIGN KEY (event_id) REFERENCES risk_event (event_id);
ALTER TABLE event_close ADD CONSTRAINT event_close_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES app_user (user_id);
ALTER TABLE media_clip ADD CONSTRAINT media_clip_event_id_fkey FOREIGN KEY (event_id) REFERENCES risk_event (event_id);
ALTER TABLE media_clip_retention ADD CONSTRAINT media_clip_retention_clip_id_fkey FOREIGN KEY (clip_id) REFERENCES media_clip (clip_id);
ALTER TABLE media_clip_retention ADD CONSTRAINT media_clip_retention_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES app_user (user_id);
ALTER TABLE clip_access_log ADD CONSTRAINT clip_access_log_clip_id_fkey FOREIGN KEY (clip_id) REFERENCES media_clip (clip_id);
ALTER TABLE clip_access_log ADD CONSTRAINT clip_access_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES app_user (user_id);
ALTER TABLE upload_grant ADD CONSTRAINT upload_grant_requested_by_device_fkey FOREIGN KEY (requested_by_device) REFERENCES device (device_id);
ALTER TABLE upload_grant ADD CONSTRAINT upload_grant_requested_by_user_fkey FOREIGN KEY (requested_by_user) REFERENCES app_user (user_id);
ALTER TABLE notification ADD CONSTRAINT notification_event_id_source_fkey FOREIGN KEY (event_id, source) REFERENCES risk_event (event_id, source);
ALTER TABLE notification ADD CONSTRAINT notification_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES push_subscription (subscription_id);
ALTER TABLE notification ADD CONSTRAINT notification_target_user_id_fkey FOREIGN KEY (target_user_id) REFERENCES app_user (user_id);
ALTER TABLE push_subscription ADD CONSTRAINT push_subscription_user_id_fkey FOREIGN KEY (user_id) REFERENCES app_user (user_id);
ALTER TABLE test_push ADD CONSTRAINT test_push_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES app_user (user_id);
ALTER TABLE test_push ADD CONSTRAINT test_push_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES push_subscription (subscription_id);
ALTER TABLE push_receipt ADD CONSTRAINT push_receipt_notification_id_fkey FOREIGN KEY (notification_id) REFERENCES notification (notification_id);
ALTER TABLE push_receipt ADD CONSTRAINT push_receipt_test_push_id_fkey FOREIGN KEY (test_push_id) REFERENCES test_push (test_push_id);
ALTER TABLE push_receipt ADD CONSTRAINT push_receipt_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES push_subscription (subscription_id);
ALTER TABLE analysis_job ADD CONSTRAINT analysis_job_event_id_fkey FOREIGN KEY (event_id) REFERENCES risk_event (event_id);
ALTER TABLE analysis_job ADD CONSTRAINT analysis_job_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES app_user (user_id);
ALTER TABLE job_lease ADD CONSTRAINT job_lease_job_id_fkey FOREIGN KEY (job_id) REFERENCES analysis_job (job_id);
ALTER TABLE job_lease ADD CONSTRAINT job_lease_worker_id_fkey FOREIGN KEY (worker_id) REFERENCES worker (worker_id);

-- 대상 테이블이 다음 단계라 FK 를 아직 걸지 않은 컬럼: risk_event_update.escalation_id → event_escalation

-- 시계열(hypertable). 청크 간격은 초기값이고 운영 초기 실측으로 다시 본다(11.4)
SELECT create_hypertable('world_track_state', by_range('observation_at', INTERVAL '1 day'));
SELECT create_hypertable('skeleton_3d', by_range('observation_at', INTERVAL '1 day'));
SELECT create_hypertable('system_health', by_range('reported_at', INTERVAL '7 days'));
SELECT create_hypertable('system_metric', by_range('measured_at', INTERVAL '7 days'));
SELECT create_hypertable('device_heartbeat', by_range('received_at', INTERVAL '7 days'));

-- 압축: 7 days 지난 청크(11.5, 16장 hypertable 보관·압축)
ALTER TABLE world_track_state SET (timescaledb.compress, timescaledb.compress_segmentby = 'world_id', timescaledb.compress_orderby = 'observation_at DESC');
SELECT add_compression_policy('world_track_state', INTERVAL '7 days');
ALTER TABLE skeleton_3d SET (timescaledb.compress, timescaledb.compress_segmentby = 'world_id', timescaledb.compress_orderby = 'observation_at DESC');
SELECT add_compression_policy('skeleton_3d', INTERVAL '7 days');
ALTER TABLE system_health SET (timescaledb.compress, timescaledb.compress_segmentby = 'gateway_id', timescaledb.compress_orderby = 'reported_at DESC');
SELECT add_compression_policy('system_health', INTERVAL '7 days');
ALTER TABLE system_metric SET (timescaledb.compress, timescaledb.compress_segmentby = 'source_id, metric, camera_id', timescaledb.compress_orderby = 'measured_at DESC');
SELECT add_compression_policy('system_metric', INTERVAL '7 days');
ALTER TABLE device_heartbeat SET (timescaledb.compress, timescaledb.compress_segmentby = 'device_id', timescaledb.compress_orderby = 'received_at DESC');
SELECT add_compression_policy('device_heartbeat', INTERVAL '7 days');

-- 상시 보관 정책: 운영 지표만 30일. 좌표·골격·health 는 상시 삭제하지 않고 발표 후 일괄 파기(11.5, 18.4).
-- 디스크 사용률이 70% 를 넘으면 skeleton_3d 에도 30일 정책을 거는 마이그레이션을 더한다.
SELECT add_retention_policy('system_metric', INTERVAL '30 days');
SELECT add_retention_policy('device_heartbeat', INTERVAL '30 days');

-- 인덱스(리플레이 질의 db-verification.md 5장, 작업 대여, 클립 삭제 작업)
CREATE INDEX world_track_state_world_id_observation_at_desc_idx ON world_track_state (world_id, observation_at DESC);
CREATE INDEX skeleton_3d_world_id_observation_at_desc_idx ON skeleton_3d (world_id, observation_at DESC);
CREATE INDEX risk_event_update_observation_at_idx ON risk_event_update (observation_at);
CREATE INDEX risk_event_lifecycle_severity_idx ON risk_event (lifecycle, severity);
CREATE INDEX notification_event_id_idx ON notification (event_id);
CREATE INDEX media_clip_delete_after_idx ON media_clip (delete_after);
CREATE INDEX zone_valid_from_valid_to_idx ON zone (valid_from, valid_to);
CREATE INDEX static_object_valid_from_valid_to_idx ON static_object (valid_from, valid_to);
CREATE INDEX rule_version_valid_from_valid_to_idx ON rule_version (valid_from, valid_to);
CREATE INDEX scene_version_valid_from_valid_to_idx ON scene_version (valid_from, valid_to);
CREATE INDEX analysis_job_status_priority_idx ON analysis_job (status, priority);
CREATE INDEX job_lease_job_id_idx ON job_lease (job_id);

-- 설명(설계 문서와 같은 문장)
COMMENT ON COLUMN app_user.login_id IS '로그인 ID';
COMMENT ON COLUMN app_user.role IS '관리자·반장·열람 (14장)';
COMMENT ON COLUMN app_user.display_label IS '화면 표시용 별칭. 위치·이벤트 기록에는 user_id 만 쓴다(11.4-5)';
COMMENT ON COLUMN app_user.disabled_at IS '비활성 시각(삭제하지 않음)';
COMMENT ON TABLE app_user IS '관제 화면·PWA 계정. 역할 3종 (MVP)';
COMMENT ON COLUMN refresh_token.token_hash IS '원문은 저장하지 않는다';
COMMENT ON TABLE refresh_token IS 'JWT 갱신 토큰 (MVP)';
COMMENT ON COLUMN device.device_id IS '예: laptop-01';
COMMENT ON COLUMN device.kind IS 'WATCH 는 확장(C.6)';
COMMENT ON COLUMN device.token_hash IS '기기마다 따로 발급';
COMMENT ON COLUMN device.rotated_at IS '주기 회전(14장)';
COMMENT ON COLUMN device.revoked_at IS '분실·교체 시 폐기';
COMMENT ON COLUMN device.last_heartbeat_at IS '최근 하트비트 수신 시각(조회 편의, 정본은 device_heartbeat)';
COMMENT ON TABLE device IS '현장 노트북(확장: 워치) 기기와 토큰 (MVP)';
COMMENT ON COLUMN device_heartbeat.received_at IS 'EC2 수신 시각, 시간 축';
COMMENT ON COLUMN device_heartbeat.sent_at IS '노트북 시계';
COMMENT ON COLUMN device_heartbeat.alarm IS '소리·점멸 중인 이벤트 목록';
COMMENT ON COLUMN device_heartbeat.cameras IS '카메라별 RTSP 상태·도착 간격';
COMMENT ON TABLE device_heartbeat IS '노트북 → EC2 1초 하트비트 기록. remote_state 판단 근거 (MVP)';
COMMENT ON COLUMN worker.worker_id IS '예: worker-01';
COMMENT ON COLUMN worker.job_types IS '마지막 대여 요청의 처리 가능 유형';
COMMENT ON TABLE worker IS '분석 워커와 토큰 (MVP)';
COMMENT ON COLUMN site.world_origin IS '원점·축 선택 근거(첫 CALIBRATED 의 바닥·벽, 4.1)';
COMMENT ON COLUMN site.established_calibration_version IS '좌표계를 정한 보정';
COMMENT ON COLUMN site.current_floor_plan_id IS '현재 BEV 평면도';
COMMENT ON COLUMN site.migrated_from IS '좌표계 이전 이력';
COMMENT ON TABLE site IS '현장과 World 좌표계 정의 (MVP)';
COMMENT ON COLUMN camera.camera_id IS '예: cam1';
COMMENT ON COLUMN camera.model IS '예: Tapo C110';
COMMENT ON COLUMN camera.removed_at IS '철거(R7·운영자 종료 근거)';
COMMENT ON TABLE camera IS '카메라 장비 정보 (MVP)';
COMMENT ON COLUMN zone.zone_kind IS '유형(개구부·출입 통제·장비 주차 등)';
COMMENT ON COLUMN zone.eval_type IS '8.2';
COMMENT ON COLUMN zone.polygon IS 'World XY(m), SRID 0 로컬 좌표. 저장·조회만(판정은 Risk Engine)';
COMMENT ON COLUMN zone.z_min_m IS 'VOLUME_3D 높이 범위';
COMMENT ON COLUMN zone.requirements IS '요구 조건(안전모 요구 등, R9 확장)';
COMMENT ON COLUMN zone.has_guardrail IS 'R3 난간 유무';
COMMENT ON COLUMN zone.valid_to IS 'null = 현재 유효';
COMMENT ON TABLE zone IS '위험 구역. 수정은 새 버전 (MVP)';
COMMENT ON COLUMN static_object.obb IS '보수적 OBB(중심·크기·yaw)';
COMMENT ON COLUMN static_object.top_height_m IS '상판 높이(작업면)';
COMMENT ON TABLE static_object IS '등록 작업면·정적 객체(Operational Geometry) (MVP)';
COMMENT ON COLUMN rule_version.rule_id IS 'R1~R10';
COMMENT ON COLUMN rule_version.rule_version IS '교실 시험 프로파일은 기본 + 1000(7.2)';
COMMENT ON COLUMN rule_version.thresholds IS '규칙 객체의 설정값(규칙 등록부에서 추출, ai `simai_ai.risk.rule_catalog`)';
COMMENT ON COLUMN rule_version.default_severity IS '규칙 모듈의 고정 심각도. 비면 규칙이 판정마다 정한다(R1 은 구역 심각도)';
COMMENT ON COLUMN rule_version.legal_basis IS '근거 조항(RuleSpec.legal_basis)';
COMMENT ON COLUMN rule_version.reads IS '규칙이 읽는 상태(RuleSpec.reads)';
COMMENT ON COLUMN rule_version.stage IS 'MVP·M2·확장';
COMMENT ON TABLE rule_version IS 'Risk Engine 규칙 임계값 세트. 규칙 등록부에서 자동 생성한 Flyway 시드로 채운다 (MVP)';
COMMENT ON COLUMN config_bundle.components IS '구성 요소별 version·sha256·uri';
COMMENT ON COLUMN config_bundle.applied_at IS '현장 적용 시각(APPLIED 받은 뒤)';
COMMENT ON TABLE config_bundle IS '구성 묶음 발행과 현장 적용 (MVP)';
COMMENT ON COLUMN config_bundle_item.item_id IS 'zone_id·rule_id·object_id';
COMMENT ON TABLE config_bundle_item IS '묶음에 들어간 구역·규칙·형상 버전 목록 (MVP)';
COMMENT ON TABLE config_ack IS 'Tier 0 의 구성 묶음 적용·거부 응답 (MVP)';
COMMENT ON COLUMN tier0_endpoint.ports IS 'role·camera_id·requested_port·public_port';
COMMENT ON COLUMN tier0_endpoint.tls_public_key_sha256 IS '공개키 고정 지문';
COMMENT ON TABLE tier0_endpoint IS 'Tier 0 Pod 엔드포인트 등록 이력(최신 행을 노트북에 줌) (MVP)';
COMMENT ON COLUMN calibration_version.calibration_version IS '예: cal-7';
COMMENT ON COLUMN calibration_version.reason IS 'INITIAL·RECALIBRATED·STATE_CHANGED·DRIFT_DETECTED·STREAM_CHANGED';
COMMENT ON COLUMN calibration_version.calibration_ref IS '보정 문서 주소';
COMMENT ON COLUMN calibration_version.validation IS '독립 검증 요약';
COMMENT ON COLUMN calibration_version.operating_reprojection IS '운영 중 카메라별 재투영 오차(최신값)';
COMMENT ON COLUMN calibration_version.source_job_id IS 'calibration_ba 작업';
COMMENT ON COLUMN calibration_version.confirmed_by IS 'POST …/confirmation';
COMMENT ON TABLE calibration_version IS '보정 버전(상태·스케일 근거·검증 요약) (MVP)';
COMMENT ON COLUMN calibration_camera.k_matrix IS '3×3';
COMMENT ON TABLE calibration_camera IS '보정 버전의 카메라별 K·왜곡·R·t (MVP)';
COMMENT ON COLUMN calibration_run.obs2d_archive_uri IS 'calibration_ba 입력';
COMMENT ON COLUMN calibration_run.result_calibration_version IS '보정 후보';
COMMENT ON TABLE calibration_run IS '자동 보정 실행 요청(걷기 보정) (M1)';
COMMENT ON COLUMN calibration_verification.max_position_error_m IS '실측 기준점이 있을 때만';
COMMENT ON COLUMN calibration_verification.passed IS 'CALIBRATED 기준 통과 여부';
COMMENT ON COLUMN calibration_verification.detail IS '검산점별 값';
COMMENT ON TABLE calibration_verification IS '키를 아는 사람 3~5곳 검산 결과 (M1)';
COMMENT ON COLUMN scale_reference.name IS '예: 라바콘 A';
COMMENT ON COLUMN scale_reference.kind IS '안전봉·라바콘·바리케이드 등';
COMMENT ON COLUMN scale_reference.measured_at IS '측정일';
COMMENT ON TABLE scale_reference IS '미리 치수를 잰 시연용 안전 장비 목록 (M1)';
COMMENT ON COLUMN calibration_scale_check.measured_height_m IS '2대 이상 삼각측량';
COMMENT ON TABLE calibration_scale_check IS '보정에서 실측 정물별로 잰 크기와 오차 (M1)';
COMMENT ON COLUMN coverage_guide.camera_id IS 'null 이면 전체(걷기 경로 안내)';
COMMENT ON COLUMN coverage_guide.message IS '예: 카메라 3번이 너무 아래를 봄';
COMMENT ON COLUMN coverage_guide.metrics IS '바닥 커버리지 비율 등';
COMMENT ON TABLE coverage_guide IS '커버리지 계산 결과와 카메라별 안내 문구 (MVP)';
COMMENT ON COLUMN floor_plan.scene_version IS '정적 3D 가 있으면';
COMMENT ON COLUMN floor_plan.image_key IS '객체 저장소 키';
COMMENT ON TABLE floor_plan IS 'BEV 평면도(보조 화면) (MVP)';
COMMENT ON COLUMN scene_version.scene_version IS '예: scene-3';
COMMENT ON COLUMN scene_version.calibration_version IS 'CALIBRATED 에서만 생성(4.5)';
COMMENT ON COLUMN scene_version.quality IS '품질 지표';
COMMENT ON COLUMN scene_version.is_upper_bound IS 'true 면 COMMITTED 불가';
COMMENT ON COLUMN scene_version.job_id IS 'static_reconstruct';
COMMENT ON COLUMN scene_version.requested_by IS 'POST /api/scenes/rebuild';
COMMENT ON COLUMN scene_version.valid_from IS 'COMMITTED 시작';
COMMENT ON TABLE scene_version IS '정적 3D Scene. 테이블은 MVP(버전 ID 참조), 생성은 M3 (MVP)';
COMMENT ON COLUMN world_track_state.observation_at IS '시간 축';
COMMENT ON COLUMN world_track_state.msg_id IS '이벤트 evidence_ref 가 가리키는 값(인덱스)';
COMMENT ON COLUMN world_track_state.ground_anchor_xyz IS 'Z=0 대체 금지';
COMMENT ON COLUMN world_track_state.calibration_version IS '논리 참조(hypertable 은 FK 를 걸지 않음)';
COMMENT ON TABLE world_track_state IS 'World Track 상태 10 Hz(최소 리플레이 근거) (MVP)';
COMMENT ON COLUMN skeleton_3d.joints IS 'null 관절은 NaN 이 아니라 null';
COMMENT ON COLUMN skeleton_3d.joint_source IS 'observed·inferred';
COMMENT ON TABLE skeleton_3d IS '3D 골격 5~10 Hz(리플레이 확장·뷰어) (MVP)';
COMMENT ON COLUMN system_health.reported_at IS '시간 축(Tier 0 시계)';
COMMENT ON COLUMN system_health.pipeline_lag_ms IS 'null = 모름';
COMMENT ON COLUMN system_health.capture_to_risk_ms IS 'p50·p95·p99';
COMMENT ON COLUMN system_health.cameras IS '카메라별 fps·상태·동기화·프레임 집계';
COMMENT ON TABLE system_health IS 'Tier 0 health 기록(카메라별 값은 JSONB) (MVP)';
COMMENT ON COLUMN system_metric.source_id IS 'tier0-01·laptop-01·ec2·worker-01';
COMMENT ON COLUMN system_metric.metric IS '예: capture_to_site_alert_ms, gateway_to_runpod_loss';
COMMENT ON COLUMN system_metric.camera_id IS '카메라 무관이면 빈 문자열';
COMMENT ON COLUMN system_metric.labels IS '백분위 등';
COMMENT ON TABLE system_metric IS '구간별 지연·FPS·큐·손실 지표(긴 형식) (MVP)';
COMMENT ON COLUMN observation_summary.archive_uri IS 'volume:// 원본 위치';
COMMENT ON COLUMN observation_summary.cameras IS '카메라별 처리 프레임·검출·트랙 수';
COMMENT ON TABLE observation_summary IS '2D 관측 카메라별 요약(원본은 Tier 0 저장 위치의 Parquet) (M1)';
COMMENT ON COLUMN risk_event.event_id IS 'UUIDv7';
COMMENT ON COLUMN risk_event.source IS 'notification 복합 FK 대상(11.4-3)';
COMMENT ON COLUMN risk_event.rule_version IS '(rule_id, rule_version) 논리 참조';
COMMENT ON COLUMN risk_event.severity IS '운영자 상향 반영 후 등급';
COMMENT ON COLUMN risk_event.world_ids IS '발생 당시 원본 ID';
COMMENT ON COLUMN risk_event.rationale IS '최신 VLM 근거(정본은 vlm_assessment)';
COMMENT ON COLUMN risk_event.event_sequence IS '반영한 마지막 전이';
COMMENT ON COLUMN risk_event.delivery IS 'REALTIME·DELAYED_ACTIVE·AUDIT_ONLY';
COMMENT ON COLUMN risk_event.resolution IS '운영자 종료(정본은 event_close)';
COMMENT ON COLUMN risk_event.evidence_snapshot IS '근거 스냅샷(11.3)';
COMMENT ON COLUMN risk_event.retention_hold IS '보존 표시(11.3)';
COMMENT ON TABLE risk_event IS 'LIVE·REPLAY 이벤트 현재 상태(전이 반영 결과). PLANNED·WHAT_IF 는 sim (MVP)';
COMMENT ON COLUMN risk_event_update.evidence_ref IS 'world_track_state.msg_id 논리 참조';
COMMENT ON COLUMN risk_event_update.ack_id IS 'ACKNOWLEDGED 일 때';
COMMENT ON COLUMN risk_event_update.close_id IS '운영자 종료일 때';
COMMENT ON COLUMN risk_event_update.escalation_id IS '운영자 상향일 때';
COMMENT ON COLUMN risk_event_update.delivery_mode IS '재동기화 중 받았는지(11.3 지연 전달·감사 전용)';
COMMENT ON TABLE risk_event_update IS 'Tier 0 전이 기록(추가만). (event_id, event_sequence) 로 중복 제거 (MVP)';
COMMENT ON COLUMN event_ack.target_event_sequence IS '확인한 사람이 본 상태';
COMMENT ON COLUMN event_ack.actor_user_id IS '관제 화면·PWA';
COMMENT ON COLUMN event_ack.actor_device_id IS '현장 확인 버튼';
COMMENT ON COLUMN event_ack.journal_event_sequence IS 'ACKNOWLEDGED 전이 sequence(저널 기록 뒤 채움)';
COMMENT ON TABLE event_ack IS '운영자 확인 기록(누가·어디서·어느 상태) (MVP)';
COMMENT ON COLUMN event_close.close_id IS 'EC2 가 발급하는 고유 ID. I1 `event_close` 의 멱등 키(Tier 0 는 같은 값을 DUPLICATE 로 답함)';
COMMENT ON COLUMN event_close.idempotency_key IS 'REST 요청이 보낸 멱등 키(`POST /api/events/{id}/close` 본문). 같은 키로 다시 오면 새 행을 만들지 않고 처음 결과를 돌려준다. `close_id` 와 따로 둔다';
COMMENT ON COLUMN event_close.result IS 'null = 응답 대기';
COMMENT ON TABLE event_close IS '관리자의 구역 대상 이벤트 종료 명령과 결과 (MVP)';
COMMENT ON COLUMN journal_cursor.acked_offset IS '빠짐없이 저장한 마지막 offset';
COMMENT ON COLUMN journal_cursor.resend_from_offset IS '빈 구간 재요청 시작 offset';
COMMENT ON COLUMN journal_cursor.sync_mode IS '재동기화 중(실시간 알림 보류)';
COMMENT ON TABLE journal_cursor IS 'Tier 0 인스턴스별 저널 수신 상태 (MVP)';
COMMENT ON COLUMN media_clip.resume_count IS '(event_id, resume_count, camera_id)';
COMMENT ON COLUMN media_clip.delete_after IS '14일';
COMMENT ON COLUMN media_clip.retention_hold IS '현재 보존 표시(이력은 media_clip_retention)';
COMMENT ON COLUMN media_clip.deleted_at IS '수명 주기 삭제 확인';
COMMENT ON TABLE media_clip IS '이벤트 클립 기록(카메라 하나 × 위험 구간 하나) (MVP)';
COMMENT ON COLUMN media_clip_retention.gateway_id IS 'Tier 0 기록일 때';
COMMENT ON COLUMN media_clip_retention.retention_hold IS '관리자는 false(떼기)도 가능';
COMMENT ON COLUMN media_clip_retention.actor_user_id IS '관리자일 때';
COMMENT ON TABLE media_clip_retention IS '보존 표시 이력(CRITICAL 자동, 관리자 붙이기·떼기) (MVP)';
COMMENT ON COLUMN clip_access_log.signed_url_expires_at IS '발급한 임시 주소 만료';
COMMENT ON TABLE clip_access_log IS '클립 열람 기록 (MVP)';
COMMENT ON COLUMN upload_grant.requested_by_device IS 'Tier 0 는 기기 토큰';
COMMENT ON COLUMN upload_grant.requested_by_user IS '반장(사진)';
COMMENT ON TABLE upload_grant IS '임시 업로드 주소 발급 기록(올라온 키 검증) (MVP)';
COMMENT ON COLUMN notification.event_id IS '복합 FK';
COMMENT ON COLUMN notification.source IS '11.4-3';
COMMENT ON COLUMN notification.event_sequence IS '알린 상태';
COMMENT ON COLUMN notification.channel IS 'WATCH 는 확장(C.6)';
COMMENT ON COLUMN notification.summary IS '규칙 근거 한 줄';
COMMENT ON COLUMN notification.observation_at IS '지연 측정 기준';
COMMENT ON COLUMN notification.receipt_at IS 'Service Worker 수신 확인';
COMMENT ON TABLE notification IS '실제 알림 발송 기록. LIVE 이벤트만 (M1)';
COMMENT ON COLUMN push_subscription.revoked_at IS '해지·만료';
COMMENT ON TABLE push_subscription IS 'Web Push 구독 (M1)';
COMMENT ON TABLE test_push IS '시험 푸시(RiskEvent 아님, notification 에 넣지 않음) (M1)';
COMMENT ON COLUMN push_receipt.notification_id IS '실제 알림';
COMMENT ON COLUMN push_receipt.test_push_id IS '시험 푸시. 둘 중 하나만(CK)';
COMMENT ON COLUMN push_receipt.received_at IS '기기 시각';
COMMENT ON COLUMN push_receipt.reported_at IS 'EC2 수신 시각';
COMMENT ON TABLE push_receipt IS 'Service Worker 수신 확인 원본 (M1)';
COMMENT ON COLUMN analysis_job.job_type IS 'vlm_rationale·tbm_report·static_reconstruct·calibration_ba·bev_floorplan·incident_summary·hmr_*';
COMMENT ON COLUMN analysis_job.attempt IS '반납은 세지 않음';
COMMENT ON COLUMN analysis_job.event_id IS '이벤트 관련 작업';
COMMENT ON TABLE analysis_job IS 'I5 작업(재할당돼도 같은 job_id) (MVP)';
COMMENT ON COLUMN job_lease.consume_attempt IS 'false = 반납';
COMMENT ON TABLE job_lease IS '작업 대여(한 시점에 유효한 대여 하나) (MVP)';
