# contracts/ 에서 생성한 파일이다. 손으로 고치지 않는다.
# 다시 만들기(ai/ 에서): uv run python ../contracts/scripts/generate_python_models.py
# 스키마 이름 → 모델: Contracts.model_fields['<스키마 이름>'] (예: world_state → WorldState)

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    MISSING,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    constr,
)
from typing_extensions import TypeAliasType


class Shape(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    representation: Literal['MHR']
    frozen: StrictBool
    frames_used: Annotated[StrictInt, Field(ge=0)]


class ResolutionItem(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=1)]


class Ratios(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    OBSERVED: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    INTERPOLATED: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    UNOBSERVED: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class FaceRatios(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    OBSERVED: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    UNOBSERVED: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class AvatarFrameAvatarRestPath(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            max_length=512, pattern='^/api/avatars(/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)+$'
        ),
    ]


class SurfaceRatios(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    OBSERVED: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    INTERPOLATED: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    UNOBSERVED: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class BevFloorplan(RootModel[Any]):
    root: Any


class CoordinateFrame(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    frame: Literal['WORLD']
    unit: Literal['m']
    handedness: Literal['right']
    up_axis: Literal['+Z']


CamerasSkippedAdditionalProperty = TypeAliasType(
    "CamerasSkippedAdditionalProperty", Annotated[StrictStr, Field(min_length=1)]
)


class Validation(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    checkpoint_count: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    max_position_error_m: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    max_pairwise_position_disagreement_m: Annotated[
        StrictFloat | MISSING, Field(ge=0.0)
    ] = MISSING
    max_head_height_error_m: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    floor_plane_disagreement_m: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = (
        MISSING
    )
    floor_coverage_ratio: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = (
        MISSING
    )


class ImageSizeItem(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=1)]


class Distortion(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    model: Literal['OPENCV_PINHOLE', 'OPENCV_FISHEYE', 'NONE']
    coeffs: list[StrictFloat]


class ReprojectionErrorPx(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    median: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    p95: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING


class CalibrationScaleBasis(
    RootModel[Literal['MEASURED_OBJECT', 'MEASURED_CAMERA_HEIGHT', 'KNOWN_PERSON_HEIGHT', 'HUMAN_HEIGHT_STATS', 'STANDARD_OBJECT', 'MODEL_ESTIMATE', 'DATASET_GT']]
):
    root: Literal['MEASURED_OBJECT', 'MEASURED_CAMERA_HEIGHT', 'KNOWN_PERSON_HEIGHT', 'HUMAN_HEIGHT_STATS', 'STANDARD_OBJECT', 'MODEL_ESTIMATE', 'DATASET_GT']


class Outlier(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(max_length=160, min_length=1)]


class CalibrationScaleSource(
    RootModel[Literal['HEIGHT_ONLY', 'JOINT', 'PROP_ONLY', 'MODEL_ESTIMATE']]
):
    root: Literal['HEIGHT_ONLY', 'JOINT', 'PROP_ONLY', 'MODEL_ESTIMATE']


class ForwardXyzItem(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(ge=-1.0, le=1.0)]


class ReprojectionErrorPx1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    median: Annotated[StrictFloat, Field(ge=0.0)]
    p95: Annotated[StrictFloat, Field(ge=0.0)]


class CodecInfoBase64(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(min_length=4, pattern='^[A-Za-z0-9+/]+={0,2}$')]


class RuleVersion(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]


class CaptureSkewMs(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class CoverageGuideCoverageParams(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    image_width_px: Annotated[StrictInt, Field(ge=160, le=4096)]
    max_keypoints: Annotated[StrictInt, Field(ge=64, le=8192)]
    ransac_threshold_px: Annotated[StrictFloat, Field(gt=0.0, le=50.0)]
    enough_min_inliers: Annotated[StrictInt, Field(ge=1)]
    weak_min_inliers: Annotated[StrictInt, Field(ge=1)]
    min_inlier_ratio: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    intrinsics_source: Literal['STORED', 'DEFAULT']


class CoverageGuideNormalizedPointItem(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class CoverageGuideNormalizedPoint(RootModel[list[CoverageGuideNormalizedPointItem]]):
    root: Annotated[
        list[CoverageGuideNormalizedPointItem], Field(max_length=2, min_length=2)
    ]


class RawMatches(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class Inliers(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class InlierRatio(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class DefsAckChannel(RootModel[Literal['CONTROL_ROOM', 'FOREMAN_PWA', 'SITE_BUTTON']]):
    root: Literal['CONTROL_ROOM', 'FOREMAN_PWA', 'SITE_BUTTON']


class DefsAxisAngleItem(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(ge=-3.1416, le=3.1416)]


class DefsAxisAngle(RootModel[list[DefsAxisAngleItem]]):
    root: Annotated[list[DefsAxisAngleItem], Field(max_length=3, min_length=3)]


class DefsBboxXyxy(RootModel[list[StrictFloat]]):
    root: Annotated[list[StrictFloat], Field(max_length=4, min_length=4)]


class DefsCalibrationSource(
    RootModel[Literal['G1_AUTO', 'DATASET_GT', 'SIM', 'MODEL_ESTIMATE']]
):
    root: Literal['G1_AUTO', 'DATASET_GT', 'SIM', 'MODEL_ESTIMATE']


class DefsCalibrationState(
    RootModel[Literal['UNCALIBRATED', 'PROVISIONAL', 'CALIBRATED', 'DRIFTED']]
):
    root: Literal['UNCALIBRATED', 'PROVISIONAL', 'CALIBRATED', 'DRIFTED']


class DefsCameraId(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(pattern='^[A-Za-z0-9_-]+$')]


class DefsConfidenceReason(
    RootModel[Literal['Z_UNKNOWN', 'PREDICTED', 'PROVISIONAL_CALIB', 'SINGLE_VIEW', 'JOINT_UNCERTAIN', 'IDENTITY_UNCERTAIN', 'PPE_UNKNOWN', 'UNSPECIFIED']]
):
    root: Literal['Z_UNKNOWN', 'PREDICTED', 'PROVISIONAL_CALIB', 'SINGLE_VIEW', 'JOINT_UNCERTAIN', 'IDENTITY_UNCERTAIN', 'PPE_UNKNOWN', 'UNSPECIFIED']


class DefsConfidenceReasons(RootModel[list[DefsConfidenceReason]]):
    root: list[DefsConfidenceReason]


class DefsConfigSync(RootModel[Literal['CURRENT', 'PENDING', 'UNVERIFIED', 'STALE']]):
    root: Literal['CURRENT', 'PENDING', 'UNVERIFIED', 'STALE']


class DefsCoordinateFrame(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    frame: Literal['WORLD']
    unit: Literal['m']
    handedness: Literal['right']
    up_axis: Literal['+Z']
    site_id: StrictStr | MISSING = MISSING


class DefsDurationMs(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(ge=0.0)]


class DefsFrameKey(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    camera_id: DefsCameraId
    stream_epoch: Annotated[StrictInt, Field(ge=0)]
    frame_sequence: Annotated[StrictInt, Field(ge=0)]


class DefsFrameSource(RootModel[Literal['LIVE', 'FILE', 'SIM', 'DATASET']]):
    root: Literal['LIVE', 'FILE', 'SIM', 'DATASET']


class DefsIdentityConfidence(
    RootModel[Literal['HIGH', 'MEDIUM', 'LOW', 'IDENTITY_UNCERTAIN']]
):
    root: Literal['HIGH', 'MEDIUM', 'LOW', 'IDENTITY_UNCERTAIN']


class DefsInstanceId(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(pattern='^[a-z0-9][a-z0-9-]*$')]


class DefsJointLayout(RootModel[Literal['COCO17', 'HALPE26', 'MHR70', 'MHR_NATIVE']]):
    root: Literal['COCO17', 'HALPE26', 'MHR70', 'MHR_NATIVE']


class DefsJointSource(RootModel[Literal['observed', 'inferred']]):
    root: Literal['observed', 'inferred']


class DefsKeypoint2d1(RootModel[list[StrictFloat]]):
    root: Annotated[list[StrictFloat], Field(max_length=3, min_length=3)]


class DefsKeypoint2d(RootModel[DefsKeypoint2d1 | None]):
    root: DefsKeypoint2d1 | None


class DefsLinkState(RootModel[Literal['CONNECTED', 'DISCONNECTED']]):
    root: Literal['CONNECTED', 'DISCONNECTED']


class DefsMhrPose(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    root_rotation: DefsAxisAngle
    joint_rotations: Annotated[
        list[DefsAxisAngle | None], Field(max_length=1024, min_length=1)
    ]


class DefsNeedsCheckMatchesReasons(BaseModel):
    pass


class DefsObjectUri(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(max_length=1093, pattern='^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/\\S+$'),
    ]


class DefsObservationState(RootModel[Literal['OBSERVED', 'PREDICTED', 'UNKNOWN']]):
    root: Literal['OBSERVED', 'PREDICTED', 'UNKNOWN']


class DefsPercentilesMs(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    p50: Annotated[StrictFloat, Field(ge=0.0)]
    p95: Annotated[StrictFloat, Field(ge=0.0)]
    p99: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING


class DefsRiskConfidence(RootModel[Literal['HIGH', 'LOW']]):
    root: Literal['HIGH', 'LOW']


class DefsRiskLifecycle(RootModel[Literal['RAISED', 'UPDATED', 'RESOLVED']]):
    root: Literal['RAISED', 'UPDATED', 'RESOLVED']


class DefsRiskOrigin(RootModel[Literal['LIVE', 'PLANNED', 'REPLAY', 'WHAT_IF']]):
    root: Literal['LIVE', 'PLANNED', 'REPLAY', 'WHAT_IF']


class DefsRiskVerification(RootModel[Literal['VERIFIED', 'UNVERIFIABLE']]):
    root: Literal['VERIFIED', 'UNVERIFIABLE']


class DefsRuleMetrics(
    RootModel[dict[constr(pattern=r'^[a-z][a-z0-9_]*$', strict=True), StrictFloat]]
):
    root: dict[constr(pattern=r'^[a-z][a-z0-9_]*$', strict=True), StrictFloat]


class DefsSceneState(
    RootModel[Literal['CANDIDATE', 'VALIDATING', 'COMMITTED', 'REJECTED']]
):
    root: Literal['CANDIDATE', 'VALIDATING', 'COMMITTED', 'REJECTED']


class DefsSemver(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(pattern='^\\d+\\.\\d+\\.\\d+$')]


class DefsSeverity(RootModel[Literal['CRITICAL', 'HIGH', 'CAUTION', 'INFO']]):
    root: Literal['CRITICAL', 'HIGH', 'CAUTION', 'INFO']


class DefsSeverityUpwardOnly(BaseModel):
    pass


class DefsSha256(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(pattern='^sha256:[0-9a-f]{64}$')]


class DefsSurfaceSource(RootModel[Literal['OBSERVED', 'INTERPOLATED', 'UNOBSERVED']]):
    root: Literal['OBSERVED', 'INTERPOLATED', 'UNOBSERVED']


class DefsSystemHealth(RootModel[Literal['NOMINAL', 'DEGRADED', 'UNSAFE_UNKNOWN']]):
    root: Literal['NOMINAL', 'DEGRADED', 'UNSAFE_UNKNOWN']


class DefsTimestampMs(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class DefsTimestampSource(
    RootModel[Literal['CAMERA_STREAM', 'RECEIVE_ESTIMATED', 'FILE', 'SIM']]
):
    root: Literal['CAMERA_STREAM', 'RECEIVE_ESTIMATED', 'FILE', 'SIM']


class DefsTrackLifecycle(
    RootModel[Literal['TENTATIVE', 'CONFIRMED', 'OCCLUDED', 'LOST']]
):
    root: Literal['TENTATIVE', 'CONFIRMED', 'OCCLUDED', 'LOST']


class DefsTrackSource(
    RootModel[Literal['TIER0_LOG', 'TIER0_LIVE', 'DATASET_GT', 'SIM']]
):
    root: Literal['TIER0_LOG', 'TIER0_LIVE', 'DATASET_GT', 'SIM']


class DefsUri(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(min_length=1)]


class DefsUuid(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(pattern='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    ]


class DefsUuidV7(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(
            pattern='^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        ),
    ]


class DefsValidity(RootModel[Literal['VALID', 'UNCERTAIN', 'UNAVAILABLE']]):
    root: Literal['VALID', 'UNCERTAIN', 'UNAVAILABLE']


class DefsVec2(RootModel[list[StrictFloat]]):
    root: Annotated[list[StrictFloat], Field(max_length=2, min_length=2)]


class DefsVec3(RootModel[list[StrictFloat]]):
    root: Annotated[list[StrictFloat], Field(max_length=3, min_length=3)]


class DefsVersionId(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(min_length=1)]


class DefsWorldId(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(min_length=1)]


class DefsWorldXyz(RootModel[DefsVec3]):
    root: DefsVec3


class Envelope(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    schema_version: DefsSemver
    msg_type: Annotated[StrictStr, Field(min_length=1)]
    msg_id: DefsUuid
    sent_at: DefsTimestampMs
    source_id: Annotated[StrictStr, Field(min_length=1)]
    payload: dict[str, Any]


class Close(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    close_id: DefsUuid
    channel: Literal['CONTROL_ROOM']
    actor_role: Literal['ADMIN']
    actor_id: Annotated[StrictStr, Field(min_length=1)]
    requested_at: DefsTimestampMs
    reason: Annotated[StrictStr, Field(max_length=500, min_length=1)]


class Payload10(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    close: Close


class EventClose(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['event_close'], Field(min_length=1)]
    payload: Payload10


class Payload11(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    close_id: DefsUuid
    event_id: DefsUuidV7
    result: Literal['ACCEPTED', 'DUPLICATE', 'REJECTED']
    event_sequence: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    reject_reason: Literal['NOT_ACTIVE', 'TARGET_NOT_ZONE', 'SEVERITY_NOT_ALLOWED', 'CONDITION_PRESENT', 'JOURNAL_UNAVAILABLE'] | MISSING = (
        MISSING
    )
    decided_at: DefsTimestampMs


class EventCloseResult(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['event_close_result'], Field(min_length=1)]
    payload: Payload11


class Payload13(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    escalation_id: DefsUuid
    event_id: DefsUuidV7
    result: Literal['ACCEPTED', 'DUPLICATE', 'REJECTED']
    event_sequence: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    reject_reason: Literal['NOT_ACTIVE', 'NOT_UPWARD', 'JOURNAL_UNAVAILABLE'] | MISSING = (
        MISSING
    )
    decided_at: DefsTimestampMs


class EventEscalateResult(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['event_escalate_result'], Field(min_length=1)]
    payload: Payload13


class JournalOffset(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class EventStateSnapshotActiveEvent(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    lifecycle: Literal['RAISED', 'UPDATED']
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    severity: DefsSeverity
    confidence: DefsRiskConfidence
    verification: DefsRiskVerification
    needs_check: StrictBool
    world_ids: list[DefsWorldId]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    raised_at: DefsTimestampMs
    last_valid_observation_at: DefsTimestampMs | None
    acknowledged: StrictBool


class FrameHeaderTimebase(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(pattern='^1/[1-9][0-9]*$')]


class MotionTransition(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    at_ms: DefsTimestampMs
    kind: Literal['START', 'STOP']


class PeakFreqHz(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(ge=0.5, le=3.0)]


class BandMagnitude(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    rate_hz: Annotated[StrictFloat, Field(gt=0.0)]
    values: Annotated[list[StrictFloat], Field(max_length=512)]
    rms: Annotated[StrictFloat, Field(ge=0.0)]
    peak_freq_hz: PeakFreqHz | None


class Payload16(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    clock_basis: Literal['WATCH']
    clock_offset_ms: StrictInt | None
    window_start_ms: DefsTimestampMs
    window_end_ms: DefsTimestampMs
    sample_rate_hz: Annotated[StrictFloat, Field(gt=0.0)]
    step_times_ms: Annotated[list[DefsTimestampMs], Field(max_length=64)]
    motion_transitions: Annotated[
        list[MotionTransition] | MISSING, Field(max_length=16)
    ] = MISSING
    band_magnitude: BandMagnitude


class GaitFeatures(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['gait_features'], Field(min_length=1)]
    payload: Payload16


class HealthActiveSiteAlert(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    source: Literal['LIVE']
    acknowledged: StrictBool
    severity: Literal['CRITICAL', 'HIGH']
    summary: Annotated[StrictStr, Field(min_length=1)]
    rule_id: Annotated[StrictStr | MISSING, Field(min_length=1)] = MISSING
    zone_id: Annotated[StrictStr | None | MISSING, Field(min_length=1)] = MISSING
    sound_mode: Literal['REPEAT', 'ONCE', 'SILENT'] | MISSING = MISSING


class Frames1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    received: Annotated[StrictInt, Field(ge=0)]
    gateway_to_runpod_loss: Annotated[StrictInt, Field(ge=0)]
    skipped_by_policy: Annotated[StrictInt, Field(ge=0)]
    laptop_skipped: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    unattributed_gap: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    late_dropped: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    camera_to_gateway_gap_max_ms: DefsDurationMs | MISSING = MISSING


class HealthCameraHealth(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    id: DefsCameraId
    fps: Annotated[StrictFloat, Field(ge=0.0)]
    last_capture: DefsTimestampMs | None
    sync_offset_ms: StrictFloat | None
    sync_valid: StrictBool | None | MISSING = MISSING
    status: Literal['OK', 'NO_SIGNAL', 'WAITING_KEYFRAME', 'SYNC_INVALID']
    stream_epoch: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    timestamp_source: DefsTimestampSource | MISSING = MISSING
    timestamp_uncertainty_ms: DefsDurationMs | MISSING = MISSING
    frame_age_ms: DefsPercentilesMs | MISSING = MISSING
    queue_wait_ms: DefsPercentilesMs | MISSING = MISSING
    max_result_gap_ms: DefsDurationMs | MISSING = MISSING
    frames: Frames1 | MISSING = MISSING


class HealthClockOffsetEstimate(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    offset_ms: StrictFloat
    uncertainty_ms: DefsDurationMs
    round_trip_ms: DefsDurationMs
    sample_count: Annotated[StrictInt, Field(ge=1)]
    estimated_at: DefsTimestampMs


class HealthReSetupCamera(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    reason: Annotated[StrictStr, Field(min_length=1)]
    detected_at: DefsTimestampMs


class HealthTier0Warning(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    code: Literal['CHECKPOINT_STALLED', 'CHECKPOINT_ERROR', 'JOURNAL_STALLED', 'JOURNAL_FAILED', 'OUTBOX_BLOCKED', 'SNAPSHOT_BROKEN', 'LIVE_MARKER_WRITE_FAILED']
    since_ms: DefsTimestampMs


class HealthWorldStateDrops(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    superseded: Annotated[StrictInt, Field(ge=0)]
    disconnected: Annotated[StrictInt, Field(ge=0)]
    since_ms: DefsTimestampMs


class MeshGeometry(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    format: Literal['GLTF_SKINNED', 'GLB_SKINNED', 'MHR_NATIVE']
    uri: DefsUri
    sha256: DefsSha256
    vertex_count: Annotated[StrictInt, Field(ge=1)]
    face_count: Annotated[StrictInt, Field(ge=1)]
    axis_convention: Literal['WORLD_Z_UP', 'GLTF_Y_UP'] | MISSING = MISSING


class Shape1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    representation: Literal['MHR', 'SOMA_X', 'OTHER']
    params_uri: DefsUri
    frozen: StrictBool
    frames_used: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING


class Texture1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    uri: DefsUri
    sha256: DefsSha256
    resolution: Annotated[list[StrictInt] | MISSING, Field(max_length=2, min_length=2)] = (
        MISSING
    )


class SurfaceSource1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    mask_uri: DefsUri
    ratios: Ratios
    last_observed_map_uri: DefsUri | MISSING = MISSING


class ObservationSummary1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    frames_used: Annotated[StrictInt, Field(ge=0)]
    cameras_used: list[DefsCameraId]
    first_observed_at: DefsTimestampMs
    last_observed_at: DefsTimestampMs


class Root(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    source: Literal['TIER0']
    tier0_pelvis_xyz: DefsWorldXyz | None
    tier0_ground_anchor_xyz: DefsWorldXyz | None
    tier0_t: DefsTimestampMs
    hmr_root_offset_m: StrictFloat | None | MISSING = MISSING


class NativePose(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    representation: Literal['MHR', 'OTHER']
    format_version: StrictStr
    params: dict[str, Any]


class CanonicalMotion(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    joint_layout: DefsJointLayout
    joints: list[DefsWorldXyz | None]
    rotations: list[DefsVec3 | None] | MISSING = MISSING
    joint_source: list[DefsJointSource]


class PerCameraSource(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    local_track_id: StrictStr
    estimated_capture_time: DefsTimestampMs
    timestamp_source: DefsTimestampSource
    used_for: list[Literal['POSE', 'SHAPE', 'APPEARANCE']] | MISSING = MISSING


class Timing(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    hmr_latency_ms: DefsDurationMs
    lag_vs_tier0_ms: DefsDurationMs
    fallback: StrictBool


class Quality(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    leave_one_camera_out_reproj_px: StrictFloat | None | MISSING = MISSING
    silhouette_iou: StrictFloat | None | MISSING = MISSING
    foot_to_anchor_error_m: StrictFloat | None | MISSING = MISSING


class IncidentSummary(RootModel[Any]):
    root: Any


class Clip(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    clip_id: Annotated[StrictStr, Field(min_length=1)]
    camera_id: DefsCameraId
    object_key: Annotated[StrictStr, Field(min_length=1, pattern='^[^:?#]+$')]
    clip_uri: DefsObjectUri
    clip_start_capture_ms: DefsTimestampMs
    clip_end_capture_ms: DefsTimestampMs
    frame_count: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    container: Annotated[StrictStr | MISSING, Field(min_length=1)] = MISSING


class IncidentSummaryInput(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    window_start_ms: DefsTimestampMs | MISSING = MISSING
    window_end_ms: DefsTimestampMs | MISSING = MISSING
    anchor_capture_ms: DefsTimestampMs | MISSING = MISSING
    clips: Annotated[list[Clip], Field(min_length=1)]
    face_policy: Literal['BLURRED', 'EXCLUDED']
    structured_state: dict[str, Any]


class IncidentSummaryResult(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    summary: Annotated[StrictStr, Field(max_length=400, min_length=1)]
    evidence_timestamps_ms: Annotated[list[DefsTimestampMs], Field(min_length=1)]
    window_start_ms: DefsTimestampMs
    window_end_ms: DefsTimestampMs
    frames_used: Annotated[StrictInt, Field(ge=1)]
    model_version: Annotated[StrictStr, Field(min_length=1)]
    latency_ms: DefsDurationMs | MISSING = MISSING


class Error(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    code: Annotated[StrictStr, Field(min_length=1)]
    message: StrictStr
    retryable: StrictBool | MISSING = MISSING
    consume_attempt: StrictBool | MISSING = MISSING


class Payload21(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    job_id: Annotated[StrictStr, Field(min_length=1)]
    lease_id: DefsUuid
    status: Literal['ACTIVE', 'ACCEPTED', 'DUPLICATE', 'REJECTED']
    lease_expires_at: DefsTimestampMs | MISSING = MISSING
    heartbeat_interval_ms: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    reason: Literal['LEASE_EXPIRED', 'JOB_TIMED_OUT', 'JOB_CANCELLED', 'UNKNOWN_LEASE'] | MISSING = (
        MISSING
    )


class JobLeaseStatus(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['job_lease_status'], Field(min_length=1)]
    payload: Payload21


class JobRequestJobType(
    RootModel[Literal['vlm_rationale', 'tbm_report', 'static_reconstruct', 'calibration_ba', 'bev_floorplan', 'incident_summary', 'hmr_refine', 'hmr_appearance_update', 'scene_regenerate', 'site_setup', 'vlm_zone_draft']]
):
    root: Literal['vlm_rationale', 'tbm_report', 'static_reconstruct', 'calibration_ba', 'bev_floorplan', 'incident_summary', 'hmr_refine', 'hmr_appearance_update', 'scene_regenerate', 'site_setup', 'vlm_zone_draft']


class Payload23(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    gateway_id: DefsInstanceId
    acked_offset: Annotated[StrictInt, Field(ge=0)]
    resend_from_offset: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    sync_mode: StrictBool | MISSING = MISSING


class JournalAck(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['journal_ack'], Field(min_length=1)]
    payload: Payload23


class Tier0(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    control: DefsLinkState
    last_message_at: DefsTimestampMs | None
    video_cameras: list[DefsCameraId]


class Ec2(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    state: DefsLinkState
    last_response_at: DefsTimestampMs | None | MISSING = MISSING


class Alarm(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    sounding_event_ids: list[DefsUuidV7]
    displayed_event_ids: list[DefsUuidV7]


class Camera1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    rtsp: DefsLinkState
    stream_epoch: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    last_frame_sequence: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    camera_to_gateway_gap_max_ms: DefsDurationMs | MISSING = MISSING


class Payload24(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    heartbeat_seq: Annotated[StrictInt, Field(ge=0)]
    tier0: Tier0
    ec2: Ec2 | MISSING = MISSING
    site_display: Literal['NORMAL', 'CONTROL_HALTED', 'REMOTE_FAULT', 'AI_DELAY']
    alarm: Alarm | MISSING = MISSING
    cameras: list[Camera1] | MISSING = MISSING


class LaptopHeartbeat(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['laptop_heartbeat'], Field(min_length=1)]
    payload: Payload24


class Payload25(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    lifecycle: DefsRiskLifecycle
    source: Literal['LIVE']
    severity: Literal['CRITICAL', 'HIGH']
    confidence: DefsRiskConfidence
    needs_check: StrictBool
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    world_ids: list[DefsWorldId]
    summary: Annotated[StrictStr, Field(min_length=1)]
    observation_time: DefsTimestampMs
    evaluated_at: DefsTimestampMs
    sound_mode: Literal['REPEAT', 'ONCE', 'SILENT'] | MISSING = MISSING
    acknowledged: StrictBool


class LocalAlert(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['local_alert'], Field(min_length=1)]
    payload: Payload25


class StreamEpoch(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class Payload26(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
        regex_engine="python-re",
    )
    clip_id: DefsUuidV7
    event_id: DefsUuidV7
    gateway_id: DefsInstanceId
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    resume_count: Annotated[StrictInt, Field(ge=0)]
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    severity: Literal['CRITICAL', 'HIGH']
    source: Literal['LIVE']
    camera_id: DefsCameraId
    stream_epochs: Annotated[list[StreamEpoch], Field(min_length=1)]
    anchor_capture_ms: DefsTimestampMs
    requested_start_ms: DefsTimestampMs
    requested_end_ms: DefsTimestampMs
    clip_start_capture_ms: DefsTimestampMs
    clip_end_capture_ms: DefsTimestampMs
    frame_count: Annotated[StrictInt, Field(ge=1)]
    starts_with_keyframe: StrictBool
    truncated_start: StrictBool
    truncated_end: StrictBool
    has_gaps: StrictBool
    content_type: Annotated[StrictStr, Field(min_length=1)]
    container: Annotated[StrictStr, Field(min_length=1)]
    size_bytes: Annotated[StrictInt, Field(ge=1)]
    sha256: DefsSha256
    face_redaction: Literal['NONE', 'BLURRED_BEFORE_EXPORT']
    redactor_version: Annotated[StrictStr | None, Field(min_length=1)]
    created_at_ms: DefsTimestampMs
    delete_after_ms: DefsTimestampMs
    retention_hold: StrictBool
    object_key: Annotated[
        StrictStr,
        Field(
            max_length=1024,
            min_length=1,
            pattern='^(?![A-Za-z][A-Za-z0-9+.-]*:)(?!/)[^?#\\s]+$',
        ),
    ]
    uploaded_at_ms: DefsTimestampMs
    spooled: StrictBool | MISSING = MISSING
    upload_attempts: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING


class MediaClip(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['media_clip'], Field(min_length=1)]
    payload: Payload26


class Payload27(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    clip_id: DefsUuidV7
    event_id: DefsUuidV7
    gateway_id: DefsInstanceId
    resume_count: Annotated[StrictInt, Field(ge=0)]
    severity: Literal['CRITICAL', 'HIGH']
    retention_hold: Literal[True]
    reason: Literal['CRITICAL_AUTO']
    issued_at_ms: DefsTimestampMs
    source: Literal['LIVE']


class MediaClipRetention(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['media_clip_retention'], Field(min_length=1)]
    payload: Payload27


class ObservationBatchDetection(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    local_track_id: Annotated[StrictStr, Field(min_length=1)]
    bbox: DefsBboxXyxy
    score: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    keypoints_2d: list[DefsKeypoint2d | None]
    world_id: DefsWorldId | None | MISSING = MISSING


class ObservationSummaryCameraSummary(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    frames_processed: Annotated[StrictInt, Field(ge=0)]
    detections: Annotated[StrictInt, Field(ge=0)]
    max_detections_per_frame: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    local_tracks: Annotated[StrictInt, Field(ge=0)]
    linked_tracks: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    frame_age_ms: DefsPercentilesMs | MISSING = MISSING
    last_capture: DefsTimestampMs | None | MISSING = MISSING


class WorkId(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=1)]


class PpePolicy(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    format_version: Annotated[StrictStr, Field(pattern='^1\\.\\d+\\.\\d+$')]
    version: DefsVersionId
    work_id: WorkId | None
    effective_from_ms: DefsTimestampMs
    helmet: Literal['REQUIRED', 'NOT_REQUIRED']


class PpeStateEvidence(DefsFrameKey):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    stream_epoch: Annotated[StrictInt, Field(ge=0)]
    frame_sequence: Annotated[StrictInt, Field(ge=0)]
    capture_time_ms: DefsTimestampMs
    local_track_id: Annotated[StrictStr, Field(min_length=1)]
    candidate_state: Literal['WORN', 'NOT_WORN']
    confidence: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    region_quality: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class PpeStateItemState(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    state: Literal['WORN', 'NOT_WORN', 'UNKNOWN']
    reason: Annotated[StrictStr, Field(pattern='^[A-Z][A-Z0-9_]{1,63}$')]
    valid_cells: Annotated[StrictInt, Field(ge=0)]
    support_ratio: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    last_evidence_at_ms: DefsTimestampMs | None
    expires_at_ms: DefsTimestampMs | None
    evidence: Annotated[list[PpeStateEvidence], Field(max_length=16)]


class PpeStatePersonState(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    world_id: DefsWorldId
    identity_revision: Annotated[StrictInt, Field(ge=0)]
    capture_time_ms: DefsTimestampMs
    helmet: PpeStateItemState
    vest: PpeStateItemState


class Storage(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    uri: Annotated[StrictStr, Field(pattern='^(volume|s3)://\\S+$')]
    layout: Annotated[StrictStr, Field(min_length=1)]


class RecordedBy(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    tool: Annotated[StrictStr | MISSING, Field(min_length=1)] = MISSING
    version: StrictStr | MISSING = MISSING
    git_commit: Annotated[StrictStr | None | MISSING, Field(pattern='^[0-9a-f]{7,40}$')] = (
        MISSING
    )
    gateway_id: StrictStr | None | MISSING = MISSING


class RecordingManifestCameraToGatewayGap(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    threshold_ms: Annotated[StrictFloat | None, Field(ge=0.0)]
    count: Annotated[StrictInt, Field(ge=0)]
    max_ms: Annotated[StrictFloat | None, Field(ge=0.0)]
    arrival_interval_ms_p95: Annotated[StrictFloat | None, Field(ge=0.0)]


class RecordingManifestCaptureTimeModel(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    timestamp_source: DefsTimestampSource
    latency_ms: DefsDurationMs
    uncertainty_ms: DefsDurationMs
    uncertainty_no_pts_ms: DefsDurationMs
    window_ms: Annotated[StrictInt, Field(ge=1)]


class RecordingManifestConsentRequirement(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    consent: dict[str, Any] | None | MISSING = MISSING


class RecordingManifestConsentScope(
    RootModel[Literal['dataset_internal', 'runpod_ap_jp_1', 'runpod_us', 'aws_seoul', 'presentation', 'hmr_texture']]
):
    root: Literal['dataset_internal', 'runpod_ap_jp_1', 'runpod_us', 'aws_seoul', 'presentation', 'hmr_texture']


class RecordingManifestInputKind(RootModel[Literal['RTSP', 'FILE', 'SYNTHETIC']]):
    root: Literal['RTSP', 'FILE', 'SYNTHETIC']


class RecordingManifestInputKindsCoverSources(BaseModel):
    pass


class RecordingManifestRelativePath(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(pattern='^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$'),
    ]


class FrameSequenceRangeItem(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class FrameSequenceRange(RootModel[list[FrameSequenceRangeItem]]):
    root: Annotated[list[FrameSequenceRangeItem], Field(max_length=2, min_length=2)]


class Integrity(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    sequence_contiguous: Literal[True]
    first_is_keyframe: Literal[True]
    bytes_covered: Literal[True]


class RecordingManifestSettings(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    codec: Literal['H264'] | None
    profile: StrictStr | None
    width: Annotated[StrictInt | None, Field(ge=1)]
    height: Annotated[StrictInt | None, Field(ge=1)]
    nominal_fps: Annotated[StrictFloat | None, Field(gt=0.0)]
    firmware: StrictStr | None
    resolution_changed: StrictBool


StallsAdditionalProperty = TypeAliasType(
    "StallsAdditionalProperty", Annotated[StrictFloat, Field(ge=0.0)]
)


class DisconnectAfterItem(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class Faults(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    jitter_ms: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    stalls: (
        dict[constr(pattern=r'^[0-9]+$', strict=True), StallsAdditionalProperty] | MISSING
    ) = MISSING
    disconnect_after: list[DisconnectAfterItem] | MISSING = MISSING
    failed_reopens: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    seed: StrictInt | MISSING = MISSING


class RecordingManifestSource(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: RecordingManifestInputKind
    url: Annotated[StrictStr | MISSING, Field(pattern='^rtsp://[^\\s@/]+(/[^\\s@]*)?$')] = (
        MISSING
    )
    file_name: Annotated[StrictStr | MISSING, Field(min_length=1)] = MISSING
    realtime: StrictBool | MISSING = MISSING
    loop: StrictBool | MISSING = MISSING
    width: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    height: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    fps: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING
    faults: Faults | MISSING = MISSING


class RecordingManifestTestInputRule(RootModel[Any]):
    root: Any


class RecordingManifestTimeRangeMs(RootModel[list[DefsTimestampMs]]):
    root: Annotated[list[DefsTimestampMs], Field(max_length=2, min_length=2)]


class Payload31(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    source: Literal['LIVE']
    severity: Literal['CRITICAL', 'HIGH']
    needs_check: StrictBool
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    zone_name: StrictStr | MISSING = MISSING
    position_xy: DefsVec2 | MISSING = MISSING
    summary: Annotated[StrictStr, Field(max_length=120, min_length=1)]
    summary_source: Literal['RULE']
    delivery: Literal['REALTIME', 'DELAYED_ACTIVE']
    observation_time: DefsTimestampMs | MISSING = MISSING


class RiskAlert(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['risk_alert'], Field(min_length=1)]
    payload: Payload31


class Payload331(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    transition: Literal['UPDATED', 'ACKNOWLEDGED']
    resumed: Literal[False] | MISSING = MISSING


class Payload332(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    evidence_ref: StrictStr | MISSING = MISSING


class RiskPlanRequestRuleVersionRef(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]


class RiskPlanRequestWorkPosition(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    position_id: Annotated[StrictStr, Field(min_length=1)]
    position_xy: DefsVec2
    support_surface_id: Annotated[StrictStr, Field(min_length=1)]
    head_height_m: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING


class SizeXyItem(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(gt=0.0)]


class RiskPlanRequestWorkSurface(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    surface_id: Annotated[StrictStr, Field(min_length=1)]
    top_height_m: Annotated[StrictFloat, Field(gt=0.0)]
    center_xy: DefsVec2
    size_xy: Annotated[list[SizeXyItem], Field(max_length=2, min_length=2)]
    yaw_rad: Annotated[StrictFloat | MISSING, Field(ge=-3.1415927, le=3.1415927)] = (
        MISSING
    )
    top_height_sigma_m: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING


class RiskPlanRequestZone(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    zone_id: Annotated[StrictStr, Field(min_length=1)]
    zone_version: DefsVersionId
    judge_type: Literal['GROUND_FOOTPRINT', 'VOLUME_3D']
    polygon_xy: Annotated[list[DefsVec2], Field(min_length=3)]
    z_range: DefsVec2 | None
    severity: DefsSeverity
    zone_type: Annotated[StrictStr | None | MISSING, Field(min_length=1)] = MISSING
    valid_from: DefsTimestampMs | None | MISSING = MISSING
    valid_to: DefsTimestampMs | None | MISSING = MISSING


class RiskPlanResultError(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    code: Literal['INVALID_REQUEST', 'SOURCE_NOT_PLANNED', 'INVALID_LAYOUT', 'UNKNOWN_RULE', 'ENGINE_ERROR']
    message: Annotated[StrictStr, Field(max_length=2000, min_length=1)]
    retryable: StrictBool


class RiskPlanResultEvaluatedPosition(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    position_id: Annotated[StrictStr, Field(min_length=1)]
    position_xy: DefsVec2
    support_surface_id: Annotated[StrictStr, Field(min_length=1)]
    head_height_m: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING
    position_source: Literal['REQUESTED', 'GENERATED']


class PositionId(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(min_length=1)]


class ReasonCode(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(pattern='^[a-z][a-z0-9_]*$')]


class RiskPlanResultRiskItem1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    severity: DefsSeverity
    position_ids: Annotated[list[PositionId], Field(min_length=1)]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    metrics: DefsRuleMetrics
    reason_codes: list[ReasonCode]
    confidence_reasons: DefsConfidenceReasons


class RiskPlanResultRiskItem2(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    severity: DefsSeverity
    position_ids: list[PositionId]
    zone_id: Annotated[StrictStr, Field(min_length=1)]
    metrics: DefsRuleMetrics
    reason_codes: list[ReasonCode]
    confidence_reasons: DefsConfidenceReasons


class RiskPlanResultRiskItem(
    RootModel[RiskPlanResultRiskItem1 | RiskPlanResultRiskItem2]
):
    root: RiskPlanResultRiskItem1 | RiskPlanResultRiskItem2


class Config(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    path: StrictStr
    sha256: DefsSha256
    overrides: dict[str, Any] | MISSING = MISSING


class Gpu(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    model: StrictStr
    driver: StrictStr
    cuda: StrictStr
    provider: Literal['RUNPOD', 'VAST', 'OTHER', 'LOCAL']
    region: StrictStr | MISSING = MISSING
    datacenter: StrictStr | MISSING = MISSING
    shared_with_tier0: StrictBool | MISSING = MISSING


class Performance(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    stage_latency_ms: dict[str, DefsPercentilesMs] | MISSING = MISSING
    vram_peak_mb: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    throughput: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    failure_rate: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = MISSING
    storage_used_gb: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING


class Result(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: Literal['SCENE_OUTPUT', 'HUMAN_MESH_OUTPUT', 'REPORT', 'RENDER', 'LOG', 'OTHER']
    uri: DefsUri
    sha256: DefsSha256 | MISSING = MISSING


class Cost(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    gpu_hours: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    credits: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    notes: StrictStr | MISSING = MISSING


class Review(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    reviewer: StrictStr
    verdict: Literal['PASS', 'FAIL', 'CHANGES_REQUESTED']
    reviewed_at: DefsTimestampMs
    report_path: StrictStr
    reproduced: StrictBool | MISSING = MISSING


class SceneCompatibility(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    scene_version: DefsVersionId
    calibration_version: DefsVersionId | None
    verification: Literal['VERIFIED', 'DIMMED', 'MISMATCH']
    regeneration_required: StrictBool
    reason: Literal['SAME_CALIBRATION', 'SMALL_CHANGE', 'LARGE_CHANGE', 'DIFFERENT_WORLD', 'UNKNOWN']
    max_center_delta_m: Annotated[StrictFloat | None, Field(ge=0.0)]
    max_rotation_delta_rad: Annotated[
        StrictFloat | None, Field(ge=0.0, le=3.141592653589793)
    ]


class Method(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    reconstructor: Literal['MAPANYTHING_NATIVE', 'MAPANYTHING_EXTERNAL', 'MOGE3', 'VGGT', 'FLOOR_PLAN_PRIMITIVES', 'OTHER']
    reconstructor_detail: StrictStr | MISSING = MISSING
    uses_known_pose: StrictBool
    human_removal: Literal['EMPTY_ROOM', 'MASK_COMPOSITE', 'NONE'] | MISSING = MISSING
    postprocess: list[Literal['DEPTH_ALIGNMENT', 'CROSS_VIEW_ALIGNMENT', 'OUTLIER_REMOVAL', 'TSDF_FUSION', 'PLANE_CLEANUP', 'POISSON', 'TEXTURE', 'DECIMATION', 'WORLD_ALIGNMENT']] | MISSING = (
        MISSING
    )


class SurfaceValues(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    field_0: Annotated[Literal['UNOBSERVED'], Field(alias='0')]
    field_1: Annotated[Literal['INTERPOLATED'], Field(alias='1')]
    field_2: Annotated[Literal['OBSERVED'], Field(alias='2')]
    field_3: Annotated[Literal['ESTIMATED_PLANE'] | MISSING, Field(alias='3')] = MISSING
    field_4: Annotated[Literal['GENERATED'] | MISSING, Field(alias='4')] = MISSING


class Ratios2(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    UNOBSERVED: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = MISSING
    INTERPOLATED: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = MISSING
    OBSERVED: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = MISSING
    ESTIMATED_PLANE: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = MISSING
    GENERATED: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = MISSING


class Asset(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: Literal['GLTF_VISUAL', 'POINT_CLOUD', 'DEPTH_MAPS', 'MESH_RAW', 'PREVIEW_IMAGE', 'SURFACE_SOURCE_MAP']
    uri: DefsUri
    sha256: DefsSha256
    bytes: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    axis_convention: Literal['WORLD_Z_UP', 'GLTF_Y_UP'] | MISSING = MISSING
    triangle_count: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    surface_values: SurfaceValues | MISSING = MISSING
    ratios: Ratios2 | MISSING = MISSING


class WallDeviationM(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    median: Annotated[StrictFloat, Field(ge=0.0)]
    p95: Annotated[StrictFloat, Field(ge=0.0)]
    max: Annotated[StrictFloat, Field(ge=0.0)]


class Performance1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    stage_latency_ms: dict[str, DefsPercentilesMs] | MISSING = MISSING
    vram_peak_mb: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    gpu_model: StrictStr | MISSING = MISSING


class Payload36(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    site_id: Annotated[StrictStr, Field(min_length=1)]
    scene_version: DefsVersionId | None


class SceneReference(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['scene_reference'], Field(min_length=1)]
    payload: Payload36


class SceneRegenerate(RootModel[Any]):
    root: Any


class SceneRegenerateCameraRegenerateQuality(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    previous_fill_ratio: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    unrevealed_ratio: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class SceneRegenerateInput(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    request_id: Annotated[StrictInt, Field(ge=1)]
    requested_at_ms: DefsTimestampMs
    reason: Annotated[StrictStr | MISSING, Field(max_length=120)] = MISSING


class SceneRegenerateRegenerateQuality(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    previous_fill_ratio: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    unrevealed_ratio: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    cameras: dict[str, SceneRegenerateCameraRegenerateQuality] | MISSING = MISSING


class SceneRegenerateResult(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    bundle_id: Annotated[StrictStr, Field(pattern='^bundle-[0-9a-f]{32}$')]
    static_reconstruct_job_id: Annotated[StrictStr, Field(min_length=1)]
    quality: SceneRegenerateRegenerateQuality | MISSING = MISSING


class SiteSetup(RootModel[Any]):
    root: Any


class SiteSetupCameraFrameRef(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    uri: Annotated[StrictStr, Field(max_length=1024, pattern='^s3://[^\\s]+$')]
    sha256: Annotated[StrictStr, Field(pattern='^[0-9a-f]{64}$')]
    size_bytes: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    face_policy: Literal['BLURRED', 'EXCLUDED']


class SiteSetupHeightChecks1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    input_height_m: Annotated[StrictFloat, Field(gt=0.0)]
    restored_height_m: Annotated[StrictFloat, Field(gt=0.0)]
    difference_cm: StrictFloat
    within_tolerance: StrictBool


class SiteSetupHeightChecks(RootModel[dict[str, SiteSetupHeightChecks1]]):
    root: Annotated[dict[str, SiteSetupHeightChecks1], Field(min_length=1)]


class SiteSetupHoldoutStats(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    n: Annotated[StrictInt, Field(ge=0)]
    invalid: Annotated[StrictInt, Field(ge=0)]
    median: Annotated[StrictFloat | None, Field(ge=0.0)]
    p90: Annotated[StrictFloat | None, Field(ge=0.0)]
    p95: Annotated[StrictFloat | None, Field(ge=0.0)]


CameraHeightsMAdditionalProperty = TypeAliasType(
    "CameraHeightsMAdditionalProperty", Annotated[StrictFloat, Field(gt=0.0)]
)


class CameraId(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(max_length=160, min_length=1)]


class CalibrationTarget(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_ids: Annotated[list[CameraId], Field(min_length=2)]
    method: Literal['BACKGROUND_JOINT_BA_V2']
    world_frame: Literal['AUTO_FLOOR_Z_UP_METERS']


class SiteSetupInput(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    setup_session_id: DefsUuid
    work_unit_id: Annotated[StrictInt, Field(ge=1)]
    site_id: Annotated[StrictStr, Field(max_length=160, min_length=1)]
    camera_heights_m: Annotated[
        dict[constr(pattern=r'^[A-Za-z0-9_-]{1,64}$', strict=True), CameraHeightsMAdditionalProperty],
        Field(min_length=1),
    ]
    calibration_target: CalibrationTarget
    height_tolerance_cm: Annotated[StrictFloat, Field(gt=0.0)]


StageSecondsAdditionalProperty = TypeAliasType(
    "StageSecondsAdditionalProperty", Annotated[StrictFloat, Field(ge=0.0)]
)


class PairHoldout(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    before: SiteSetupHoldoutStats
    after: SiteSetupHoldoutStats


PairHoldoutMedianPxAdditionalProperty = TypeAliasType(
    "PairHoldoutMedianPxAdditionalProperty",
    Annotated[
        StrictFloat | None, Field(ge=0.0)
    ],
)


class SiteSetupObjectRef(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    uri: Annotated[StrictStr, Field(max_length=1024, pattern='^s3://[^\\s]+$')]
    sha256: Annotated[StrictStr, Field(pattern='^[0-9a-f]{64}$')]
    size_bytes: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING


BackgroundCollectionPctAdditionalProperty = TypeAliasType(
    "BackgroundCollectionPctAdditionalProperty",
    Annotated[StrictFloat, Field(ge=0.0, le=100.0)],
)


class ObjectCrossCheck(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: Literal['TRAFFIC_CONE', 'OPENING_SIGN']
    passed: StrictBool
    message: Annotated[StrictStr | MISSING, Field(max_length=500)] = MISSING
    scale: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING
    measured_m: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING
    expected_m: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING
    residual_cm: StrictFloat | MISSING = MISSING
    floor_z_m: StrictFloat | MISSING = MISSING
    cameras: Annotated[list[DefsCameraId] | MISSING, Field(min_length=2)] = MISSING


class Image(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    camera_id: DefsCameraId
    uri: DefsObjectUri
    kind: Literal['CLEAN_BACKGROUND', 'FRAME']


class Payload39(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    bundle_id: Annotated[StrictStr, Field(pattern='^bundle-[0-9a-f]{32}$')]
    job_id: Annotated[StrictStr, Field(min_length=1)]
    created: StrictBool
    job_status: Literal['QUEUED', 'LEASED', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED']
    job_error_code: StrictStr | None | MISSING = MISSING


class StaticReconstructSubmitResult(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[
        Literal['static_reconstruct_submit_result'], Field(min_length=1)
    ]
    payload: Payload39


class TbmReport(RootModel[Any]):
    root: Any


class TbmReportClauseId(RootModel[StrictStr]):
    root: Annotated[
        StrictStr,
        Field(pattern='^산업안전보건기준규칙 제[1-9][0-9]*조(의[1-9][0-9]*)?(#(0|[1-9][0-9]*))?$'),
    ]


class Plan(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    layout_id: Annotated[StrictStr, Field(min_length=1)]
    layout_version: Annotated[StrictStr, Field(min_length=1)]
    evaluation_time: DefsTimestampMs
    rules_version: Annotated[StrictStr, Field(min_length=1)]
    items: Annotated[list[RiskPlanResultRiskItem], Field(max_length=200)]
    positions: Annotated[list[RiskPlanResultEvaluatedPosition], Field(max_length=200)]
    no_evaluated_positions: StrictBool


class Event(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    event_id: DefsUuidV7
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    severity: DefsSeverity
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    raised_at: DefsTimestampMs
    resolved_at: DefsTimestampMs | None
    resolution: Literal['OBSERVED', 'OPERATOR_CLOSED'] | None
    action_status: Literal['REQUESTED', 'CONFIRMED', 'IN_PROGRESS', 'DONE'] | None


class Clause(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    clause_id: TbmReportClauseId
    text: Annotated[StrictStr, Field(max_length=4000, min_length=1)]


class Options(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    language: Literal['ko'] | MISSING = MISSING
    max_chars: Annotated[StrictInt | MISSING, Field(ge=500, le=20000)] = MISSING


class TbmReportInput(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    report_kind: Literal['TBM', 'DAILY']
    report_id: Annotated[StrictStr, Field(max_length=64, min_length=1)]
    site_id: Annotated[StrictStr, Field(max_length=64, min_length=1)]
    report_date: Annotated[StrictStr, Field(pattern='^[0-9]{4}-[0-9]{2}-[0-9]{2}$')]
    plan: Plan | MISSING = MISSING
    events: Annotated[list[Event] | MISSING, Field(max_length=500)] = MISSING
    clauses: Annotated[list[Clause], Field(max_length=50, min_length=1)]
    options: Options | MISSING = MISSING


class Paragraph(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    text: Annotated[StrictStr, Field(min_length=1)]
    citations: list[TbmReportClauseId]


class Section(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: Literal['SUMMARY', 'RISK_ITEM', 'EVENT_REVIEW', 'RECOMMENDATION']
    title: Annotated[StrictStr, Field(max_length=200, min_length=1)]
    item_index: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    paragraphs: Annotated[list[Paragraph], Field(min_length=1)]


class RiskItem(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    item_index: Annotated[StrictInt, Field(ge=0)]
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    severity: DefsSeverity
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    position_ids: list[PositionId]


class Generator(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    model: Annotated[StrictStr, Field(min_length=1)]
    prompt_version: Annotated[StrictStr, Field(min_length=1)]
    attempts: Annotated[StrictInt, Field(ge=1, le=2)]
    input_tokens: Annotated[StrictInt | None | MISSING, Field(ge=0)] = MISSING
    output_tokens: Annotated[StrictInt | None | MISSING, Field(ge=0)] = MISSING


class TbmReportResult(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    report_kind: Literal['TBM', 'DAILY']
    report_id: Annotated[StrictStr, Field(max_length=64, min_length=1)]
    sections: Annotated[list[Section], Field(min_length=1)]
    risk_items: list[RiskItem]
    citations: list[TbmReportClauseId]
    generator: Generator
    created_at: DefsTimestampMs


class Tier0EndpointPortMapping(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    role: Literal['CONTROL', 'VIDEO']
    camera_id: DefsCameraId | MISSING = MISSING
    requested_port: Annotated[StrictInt, Field(ge=70000)]
    public_port: Annotated[StrictInt, Field(ge=1, le=65535)]


class Payload41(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
        regex_engine="python-re",
    )
    segment_id: DefsUuidV7
    gateway_id: DefsInstanceId
    camera_id: DefsCameraId
    stream_epochs: Annotated[list[StreamEpoch], Field(min_length=1)]
    segment_start_capture_ms: DefsTimestampMs
    segment_end_capture_ms: DefsTimestampMs
    frame_count: Annotated[StrictInt, Field(ge=1)]
    starts_with_keyframe: StrictBool
    has_gaps: StrictBool
    content_type: Annotated[StrictStr, Field(min_length=1)]
    container: Annotated[StrictStr, Field(min_length=1)]
    size_bytes: Annotated[StrictInt, Field(ge=1)]
    sha256: DefsSha256
    face_redaction: Literal['NONE']
    created_at_ms: DefsTimestampMs
    delete_after_ms: DefsTimestampMs
    object_key: Annotated[
        StrictStr,
        Field(
            max_length=1024,
            min_length=1,
            pattern='^(?![A-Za-z][A-Za-z0-9+.-]*:)(?!/)[^?#\\s]+$',
        ),
    ]
    uploaded_at_ms: DefsTimestampMs
    upload_attempts: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING


class VideoSegment(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['video_segment'], Field(min_length=1)]
    payload: Payload41


class Laptop(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    link: DefsLinkState
    last_heartbeat_at: DefsTimestampMs | None


class DataAgeMs(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0)]


class Config1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    published_version: DefsVersionId | None
    applied_version: DefsVersionId | None
    sync: DefsConfigSync


class Scene(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    committed_version: DefsVersionId | None
    live_version: DefsVersionId | None
    compatibility: SceneCompatibility | MISSING = MISSING
    mismatch: StrictBool


class ReSetupItem(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    reason: Annotated[StrictStr, Field(min_length=1)]
    detected_at: DefsTimestampMs


class BatteryPct(RootModel[StrictInt]):
    root: Annotated[StrictInt, Field(ge=0, le=100)]


class Item(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    device_id: DefsInstanceId
    link: Literal['CONNECTED', 'LOST']
    last_heartbeat_at: DefsTimestampMs | None
    battery_pct: BatteryPct | None
    transport: Literal['WIFI', 'PHONE_BLUETOOTH', 'CELLULAR', 'UNKNOWN'] | None


class Watches(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    registered: Annotated[StrictInt, Field(ge=0)]
    connected: Annotated[StrictInt, Field(ge=0)]
    items: list[Item]


class Payload42(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    as_of: DefsTimestampMs
    system_health: DefsSystemHealth
    remote_state: Literal['NORMAL', 'REMOTE_LINK_LOST', 'CONTROL_HALTED']
    tier0_link: DefsLinkState
    laptop: Laptop
    data_age_ms: DataAgeMs | None
    calibration_state: DefsCalibrationState
    config: Config1
    scene: Scene
    tier0_health: dict[str, Any] | None
    re_setup: list[ReSetupItem] | MISSING = MISSING
    watches: Watches | MISSING = MISSING


class ViewerHealth(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['viewer_health'], Field(min_length=1)]
    payload: Payload42


class VlmRationale(RootModel[Any]):
    root: Any


class ClauseId(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(min_length=1)]


class VlmRationaleResult(DefsSeverityUpwardOnly):
    model_config = ConfigDict(
        extra='forbid',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    severity: DefsSeverity
    rationale: Annotated[StrictStr, Field(min_length=1)]
    proposed_severity: DefsSeverity | None
    clause_ids: list[ClauseId] | MISSING = MISSING
    model_version: Annotated[StrictStr, Field(min_length=1)]
    latency_ms: DefsDurationMs | MISSING = MISSING


class VlmZoneDraft(RootModel[Any]):
    root: Any


class VlmZoneDraftCandidateKind(
    RootModel[Literal['ELEVATED_WORK_SURFACE', 'OPENING', 'FLOOR_EDGE']]
):
    root: Literal['ELEVATED_WORK_SURFACE', 'OPENING', 'FLOOR_EDGE']


class TopHeightM(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(gt=0.0)]


class Confidence(RootModel[StrictFloat]):
    root: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]


class VlmZoneDraftDraftSeverity(RootModel[Literal['CRITICAL', 'HIGH']]):
    root: Literal['CRITICAL', 'HIGH']


class VlmZoneDraftEvalType(RootModel[Literal['GROUND_FOOTPRINT', 'VOLUME_3D']]):
    root: Literal['GROUND_FOOTPRINT', 'VOLUME_3D']


class VlmZoneDraftObb3d(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    center: DefsVec3
    size: DefsVec3
    yaw_rad: StrictFloat


class ModelVersion(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(min_length=1)]


class VlmZoneDraftVlmOpinionStatus(RootModel[Literal['VLM', 'NO_OPINION']]):
    root: Literal['VLM', 'NO_OPINION']


class Payload43(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    alert_msg_id: DefsUuid
    kind: Literal['VIBRATED', 'CONFIRMED']
    acked_at: DefsTimestampMs
    received_at: DefsTimestampMs | MISSING = MISSING


class WatchAck(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['watch_ack'], Field(min_length=1)]
    payload: Payload43


class Payload44(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    severity: Literal['CRITICAL', 'HIGH', 'CAUTION']
    vibration_pattern: Literal['DANGER_LONG', 'CAUTION_DOUBLE', 'CAUTION_WEAK'] | MISSING = (
        MISSING
    )
    direction: Literal['FRONT', 'BACK', 'LEFT', 'RIGHT'] | MISSING = MISSING
    word: Annotated[
        StrictStr, Field(max_length=8, min_length=1, pattern='^\\S(.*\\S)?$')
    ]
    ack_within_ms: Annotated[StrictInt, Field(ge=1, le=600000)]
    needs_check: StrictBool


class WatchAlert(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['watch_alert'], Field(min_length=1)]
    payload: Payload44


class WorldId(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(max_length=64, min_length=1)]


class Payload45(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    state: Literal['BOUND', 'UNBOUND']
    method: Literal['MOTION_MATCH', 'WAVE', 'MANUAL'] | None
    world_id: WorldId | None
    unbind_reason: Annotated[StrictStr | MISSING, Field(pattern='^[A-Z][A-Z_]{0,31}$')] = (
        MISSING
    )
    sent_at: DefsTimestampMs


class WatchBindingNotice(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['watch_binding_notice'], Field(min_length=1)]
    payload: Payload45


class WorldId1(RootModel[StrictStr]):
    root: Annotated[StrictStr, Field(min_length=1)]


class Payload46(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    origin: Literal['EVENT', 'MANUAL'] | MISSING = MISSING
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    alert_msg_id: DefsUuid
    device_id: DefsInstanceId
    world_id: WorldId1 | None
    severity: Literal['CRITICAL', 'HIGH', 'CAUTION']
    reason: Literal['NO_ACK', 'SEND_FAILED']
    sent_at: DefsTimestampMs
    escalated_at: DefsTimestampMs


class WatchEscalation(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['watch_escalation'], Field(min_length=1)]
    payload: Payload46


class Echo(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    seq: Annotated[StrictInt, Field(ge=0)]
    received_at: DefsTimestampMs


class Watch(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    battery_pct: Annotated[StrictInt, Field(ge=0, le=100)]
    transport: Literal['WIFI', 'PHONE_BLUETOOTH', 'CELLULAR', 'UNKNOWN']
    app_version: Annotated[StrictStr | MISSING, Field(min_length=1)] = MISSING


class Payload47(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    heartbeat_seq: Annotated[StrictInt, Field(ge=0)]
    echo: Echo | MISSING = MISSING
    interval_ms: Annotated[StrictInt | MISSING, Field(ge=1000, le=60000)] = MISSING
    watch: Watch | MISSING = MISSING


class WatchHeartbeat(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['watch_heartbeat'], Field(min_length=1)]
    payload: Payload47


class WorldStateHeadZUncertain1x(RootModel[Any]):
    root: Any


class WorldStateHeadZUncertain2x(RootModel[Any]):
    root: Any


class WorldStateSkeleton(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    joint_layout: DefsJointLayout
    joints: list[DefsWorldXyz | None]
    visible: list[StrictBool]
    validity: list[DefsValidity]
    joint_source: list[DefsJointSource]


class WorldStateWorldTrack(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    world_id: DefsWorldId
    t_last_valid: DefsTimestampMs
    observation_state: DefsObservationState
    track_lifecycle: DefsTrackLifecycle
    identity_confidence: DefsIdentityConfidence
    identity_revision: Annotated[StrictInt, Field(ge=0)]
    split_from: DefsWorldId | MISSING = MISSING
    aliases: Annotated[list[DefsWorldId] | MISSING, Field(min_length=1)] = MISSING
    frame_age_ms: DefsDurationMs | MISSING = MISSING
    position_xy: DefsVec2
    position_xy_sigma: DefsVec2
    pelvis_xyz: DefsWorldXyz | None
    ground_anchor_xyz: DefsWorldXyz | None
    ground_anchor_sigma: DefsVec3 | None | MISSING = MISSING
    surface_source: Literal['observed', 'inferred'] | MISSING = MISSING
    support_surface_id: StrictStr | None
    support_surface_validity: Literal['VALID', 'UNCERTAIN', 'UNKNOWN']
    contact_validity: DefsValidity
    z_validity: DefsValidity
    head_z: StrictFloat | None
    head_z_sigma: StrictFloat | None | MISSING = MISSING
    head_z_validity: DefsValidity | MISSING = MISSING
    velocity: DefsVec3 | MISSING = MISSING
    cameras: list[DefsCameraId]
    skeleton: WorldStateSkeleton | MISSING = MISSING


class FrameRef(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    stream_epoch: Annotated[StrictInt, Field(ge=0)]
    frame_sequence: Annotated[StrictInt, Field(ge=0)]


class FrameRef1(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    file_uri: DefsUri
    frame_index: Annotated[StrictInt, Field(ge=0)]


class WorldTracksCameraObservation(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    local_track_id: StrictStr
    bbox: DefsBboxXyxy
    keypoints_2d: list[DefsKeypoint2d | None] | MISSING = MISSING
    joint_layout: DefsJointLayout | MISSING = MISSING
    frame_ref: FrameRef | FrameRef1
    estimated_capture_time: DefsTimestampMs
    timestamp_source: DefsTimestampSource
    timestamp_uncertainty_ms: DefsDurationMs | MISSING = MISSING


class Mesh(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    format: Literal['GLB_SKINNED', 'GLTF_SKINNED']
    object_uri: DefsObjectUri
    sha256: DefsSha256
    size_bytes: Annotated[StrictInt, Field(ge=1)]
    vertex_count: Annotated[StrictInt, Field(ge=1)]
    face_count: Annotated[StrictInt, Field(ge=1)]
    axis_convention: Literal['GLTF_Y_UP']


class Skeleton(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    rig: Literal['MHR']
    joint_count: Annotated[StrictInt, Field(ge=1, le=1024)]
    joint_parents_sha256: DefsSha256
    pelvis_joint_index: Annotated[StrictInt, Field(ge=0)]
    rig_source: Annotated[StrictStr, Field(max_length=256, min_length=1)]


class Texture(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    object_uri: DefsObjectUri
    sha256: DefsSha256
    resolution: Annotated[list[ResolutionItem], Field(max_length=2, min_length=2)]


class SurfaceSource(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    mask_uri: DefsObjectUri | None
    ratios: Ratios
    face_ratios: FaceRatios


class AvatarFrameViewerAsset(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    stream_epoch: DefsTimestampMs
    identity_revision: Annotated[StrictInt, Field(ge=0)]
    mesh_url: AvatarFrameAvatarRestPath
    mesh_sha256: DefsSha256
    mesh_format: Literal['GLB_SKINNED', 'GLTF_SKINNED']
    joint_count: Annotated[StrictInt, Field(ge=1, le=1024)]
    joint_parents_sha256: DefsSha256
    pelvis_joint_index: Annotated[StrictInt, Field(ge=0)]
    texture_url: AvatarFrameAvatarRestPath | None
    surface_source_url: AvatarFrameAvatarRestPath | None
    surface_ratios: SurfaceRatios
    face_ratios: FaceRatios
    face_policy: Literal['OBSERVED', 'BLURRED', 'EXCLUDED']


class AvatarPoseBatchPersonPose(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    world_id: DefsWorldId
    identity_revision: Annotated[StrictInt, Field(ge=0)]
    capture_time_ms: DefsTimestampMs
    appearance_version: Annotated[StrictInt, Field(ge=0)]
    joint_source: Literal['inferred']
    pose: DefsMhrPose


class WorldBounds(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    min_xy: DefsVec2
    max_xy: DefsVec2


class RoomOutline(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    source: Literal['inferred']
    basis: Literal['FLOOR_MASK', 'CAMERA_FOOTPRINT']
    method: Literal['CONVEX_HULL_OF_COVERAGE']
    accuracy: Literal['NOT_CLAIMED']
    polygon_xy: Annotated[list[DefsVec2], Field(min_length=3)]
    openings: None


class CalibrationScalePropResidual(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: Literal['TRAFFIC_CONE', 'OPENING_SIGN']
    dimension: Annotated[StrictStr, Field(max_length=64, min_length=1)]
    expected_m: Annotated[StrictFloat, Field(ge=0.0)]
    measured_m: StrictFloat
    residual_cm: StrictFloat
    cameras: Annotated[list[DefsCameraId] | MISSING, Field(min_length=2)] = MISSING


class CalibrationScaleResiduals(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_heights_cm: (
        dict[constr(pattern=r'^[A-Za-z0-9_-]{1,64}$', strict=True), StrictFloat] | MISSING
    ) = MISSING
    props: list[CalibrationScalePropResidual] | MISSING = MISSING


class CalibrationStatusCalibratedCameraPose(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    position_xyz: DefsWorldXyz
    forward_xyz: Annotated[list[ForwardXyzItem], Field(max_length=3, min_length=3)]
    hfov_rad: Annotated[StrictFloat, Field(gt=0.0, lt=3.141592653589793)]


class CalibrationStatusCalibrationReprojectionItem(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    reprojection_error_px: ReprojectionErrorPx1


class Camera(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    reprojection_error_px: ReprojectionErrorPx1


class CctvRemoteControl(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: Literal['demand']
    active: StrictBool
    sent_at_ms: DefsTimestampMs


class Payload5(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    exchange_id: DefsUuid
    t1_ms: DefsTimestampMs


class ClockSyncRequest(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['clock_sync_request'], Field(min_length=1)]
    payload: Payload5


class Payload6(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    exchange_id: DefsUuid
    t1_ms: DefsTimestampMs
    t2_ms: DefsTimestampMs
    t3_ms: DefsTimestampMs


class ClockSyncResponse(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['clock_sync_response'], Field(min_length=1)]
    payload: Payload6


class CodecInfo(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    stream_epoch: Annotated[StrictInt, Field(ge=0)]
    codec: Literal['H264']
    profile: StrictStr | MISSING = MISSING
    sps: CodecInfoBase64
    pps: CodecInfoBase64
    pts_timebase: FrameHeaderTimebase
    width: Annotated[StrictInt, Field(ge=1)]
    height: Annotated[StrictInt, Field(ge=1)]
    nominal_fps: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING


class Payload7(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    config_version: DefsVersionId
    bundle_hash: DefsSha256
    result: Literal['APPLIED', 'REJECTED']
    applied_at: DefsTimestampMs | MISSING = MISSING
    reject_reason: Literal['SCHEMA_INVALID', 'HASH_MISMATCH', 'REFERENCE_INVALID', 'DOWNLOAD_FAILED'] | MISSING = (
        MISSING
    )
    reject_detail: StrictStr | MISSING = MISSING
    active_config_version: DefsVersionId | None


class ConfigAck(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['config_ack'], Field(min_length=1)]
    payload: Payload7


class ConfigBundleComponent(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    version: DefsVersionId
    sha256: DefsSha256
    uri: DefsUri


class Component(RootModel[list[DefsCameraId]]):
    root: Annotated[list[DefsCameraId], Field(max_length=4, min_length=1)]


class CoverageGuideCameraGuideItem(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    pair: Annotated[list[DefsCameraId], Field(max_length=2, min_length=2)]
    direction: Literal['LEFT', 'RIGHT', 'UP', 'DOWN', 'NONE']
    magnitude: Literal['SMALL', 'MORE', 'NONE']
    reason_code: Literal['WEAK_PAIR_EDGE_CLUSTER', 'NO_OVERLAP_VISIBLE', 'OPTIONAL_IMPROVE']


class CoverageGuideCameraStatusItem(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    status: Literal['OK', 'NO_VIDEO', 'STALE']
    frame_ts_ms: DefsTimestampMs | None


class CoverageGuidePairMatchItem(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    a: DefsCameraId
    b: DefsCameraId
    raw_matches: RawMatches | None
    inliers: Inliers | None
    inlier_ratio: InlierRatio | None
    grade: Literal['ENOUGH', 'WEAK', 'LACK', 'UNKNOWN']
    centroid_a: CoverageGuideNormalizedPoint | None
    centroid_b: CoverageGuideNormalizedPoint | None


class DefsCaptureTime(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    estimated_capture_time_ms: DefsTimestampMs
    timestamp_source: DefsTimestampSource
    timestamp_uncertainty_ms: DefsDurationMs


class Frames(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    source: DefsFrameSource
    dataset_id: StrictStr | MISSING = MISSING
    dataset_hash: DefsSha256 | MISSING = MISSING
    camera_ids: list[DefsCameraId] | MISSING = MISSING


class Calibration1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    source: DefsCalibrationSource
    calibration_version: DefsVersionId


class Tracks(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    source: DefsTrackSource
    provider_version: StrictStr


class DefsInputProvenance(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    frames: Frames
    calibration: Calibration1 | MISSING = MISSING
    tracks: Tracks | MISSING = MISSING
    is_upper_bound: StrictBool


class DefsMat3(RootModel[list[DefsVec3]]):
    root: Annotated[list[DefsVec3], Field(max_length=3, min_length=3)]


class DefsOperatorAck(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    ack_id: DefsUuid
    channel: DefsAckChannel
    actor_role: Literal['ADMIN', 'FOREMAN', 'SITE']
    actor_id: Annotated[StrictStr, Field(min_length=1)]
    acked_at: DefsTimestampMs


class DefsOperatorEscalation(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    escalation_id: DefsUuid
    basis: Literal['VLM_PROPOSAL']
    assessment_job_id: Annotated[StrictStr, Field(min_length=1)]
    target_severity: Literal['CRITICAL', 'HIGH', 'CAUTION']
    channel: Literal['CONTROL_ROOM']
    actor_role: Literal['ADMIN']
    actor_id: Annotated[StrictStr, Field(min_length=1)]
    requested_at: DefsTimestampMs
    note: Annotated[StrictStr | MISSING, Field(max_length=500, min_length=1)] = MISSING


class DefsOperatorResolution(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: Literal['OPERATOR_CLOSED']
    close_id: DefsUuid
    channel: Literal['CONTROL_ROOM']
    actor_role: Literal['ADMIN']
    actor_id: Annotated[StrictStr, Field(min_length=1)]
    requested_at: DefsTimestampMs
    reason: Annotated[StrictStr, Field(max_length=500, min_length=1)]


class DefsPodLocalFrame(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    path: Annotated[
        StrictStr,
        Field(
            max_length=200,
            pattern='^event-frames/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[A-Za-z0-9_-]{1,96}\\.jpg$',
        ),
    ]
    sha256: DefsSha256
    size_bytes: Annotated[StrictInt, Field(ge=1, le=16777216)]


class DefsReproducibility(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    run_id: Annotated[StrictStr, Field(min_length=1)]
    model_version: Annotated[StrictStr, Field(min_length=1)]
    weights_hash: DefsSha256 | MISSING = MISSING
    input_hash: DefsSha256


class Ack(DefsOperatorAck):
    model_config = ConfigDict(
        extra='allow',
    )
    channel: Literal['CONTROL_ROOM', 'FOREMAN_PWA', 'SITE_BUTTON', 'CONTROL_ROOM', 'FOREMAN_PWA']


class Payload9(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    ack: Ack


class EventAck(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['event_ack'], Field(min_length=1)]
    payload: Payload9


class Payload12(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    escalation: DefsOperatorEscalation


class EventEscalate(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['event_escalate'], Field(min_length=1)]
    payload: Payload12


class Frame1(DefsCaptureTime):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    frame_uri: DefsObjectUri
    estimated_capture_time_ms: DefsTimestampMs
    timestamp_source: DefsTimestampSource
    timestamp_uncertainty_ms: DefsDurationMs
    local_copy: DefsPodLocalFrame | MISSING = MISSING


class Payload14(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    frames: Annotated[list[Frame1], Field(min_length=1)]


class EventFrameReady(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['event_frame_ready'], Field(min_length=1)]
    payload: Payload14


class Payload15(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    gateway_id: DefsInstanceId
    snapshot_at: DefsTimestampMs
    journal_offset: JournalOffset | None
    config_version: DefsVersionId
    active_events: list[EventStateSnapshotActiveEvent]


class EventStateSnapshot(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['event_state_snapshot'], Field(min_length=1)]
    payload: Payload15


class FrameHeader(DefsFrameKey, DefsCaptureTime):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    stream_epoch: Annotated[StrictInt, Field(ge=0)]
    frame_sequence: Annotated[StrictInt, Field(ge=0)]
    estimated_capture_time_ms: DefsTimestampMs
    timestamp_source: DefsTimestampSource
    timestamp_uncertainty_ms: DefsDurationMs
    source_pts: StrictInt | None
    pts_timebase: FrameHeaderTimebase
    keyframe: StrictBool
    received_at_ms: DefsTimestampMs | MISSING = MISSING


class Payload17(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    system_health: DefsSystemHealth
    t_latest_global: DefsTimestampMs | None | MISSING = MISSING
    cameras: list[HealthCameraHealth]
    pipeline_lag_ms: DefsDurationMs | None
    pipeline_lag_lower_bound_ms: DefsDurationMs | MISSING = MISSING
    capture_to_risk_ms: DefsPercentilesMs | MISSING = MISSING
    calibration_state: DefsCalibrationState
    calibration_version: DefsVersionId | MISSING = MISSING
    config_version: DefsVersionId | MISSING = MISSING
    config_sync: DefsConfigSync | MISSING = MISSING
    gateway_link: DefsLinkState | MISSING = MISSING
    ec2_world_state_drops: HealthWorldStateDrops | MISSING = MISSING
    tier0_warnings: list[HealthTier0Warning] | MISSING = MISSING
    re_setup: list[HealthReSetupCamera] | MISSING = MISSING
    gpu_util: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = MISSING
    gpu_memory_used_ratio: Annotated[StrictFloat | MISSING, Field(ge=0.0, le=1.0)] = (
        MISSING
    )
    active_site_alerts: list[HealthActiveSiteAlert] | MISSING = MISSING
    active_site_alerts_as_of: DefsTimestampMs | MISSING = MISSING
    laptop_clock_offset: HealthClockOffsetEstimate | None | MISSING = MISSING


class Health(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['health'], Field(min_length=1)]
    payload: Payload17


class HumanMeshOutputCommonFields(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    world_id: DefsWorldId
    experiment_id: StrictStr | MISSING = MISSING
    calibration_version: DefsVersionId
    scene_version: DefsVersionId | MISSING = MISSING
    coordinate_frame: DefsCoordinateFrame
    input_provenance: DefsInputProvenance
    reproducibility: DefsReproducibility


class HumanMeshOutputPoseFrame(HumanMeshOutputCommonFields):
    model_config = ConfigDict(
        extra='allow',
    )
    record_type: Literal['pose_frame']
    t: DefsTimestampMs
    appearance_version: Annotated[StrictInt, Field(ge=0)]
    root: Root
    native_pose: NativePose
    canonical_motion: CanonicalMotion
    observation_state: DefsObservationState
    per_camera_sources: list[PerCameraSource]
    timing: Timing
    quality: Quality | MISSING = MISSING


class Payload18(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    job_id: Annotated[StrictStr, Field(min_length=1)]
    lease_id: DefsUuid
    job_type: JobRequestJobType
    status: Literal['SUCCEEDED', 'FAILED', 'TIMED_OUT']
    started_at: DefsTimestampMs | None | MISSING = MISSING
    finished_at: DefsTimestampMs
    result: dict[str, Any] | MISSING = MISSING
    error: Error | MISSING = MISSING


class JobCallback(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['job_callback'], Field(min_length=1)]
    payload: Payload18


class Payload20(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    job_types: Annotated[list[JobRequestJobType], Field(min_length=1)]
    wait_ms: Annotated[StrictInt, Field(ge=0, le=30000)]


class JobLeaseRequest(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['job_lease_request'], Field(min_length=1)]
    payload: Payload20


class Payload22(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    job_id: Annotated[StrictStr, Field(min_length=1)]
    lease_id: DefsUuid
    attempt: Annotated[StrictInt, Field(ge=1)]
    max_attempts: Annotated[StrictInt, Field(ge=1)]
    job_type: JobRequestJobType
    priority: Literal['VLM_FAST', 'REPORT', 'BACKGROUND']
    timeout_ms: Annotated[StrictInt, Field(ge=1)]
    leased_at: DefsTimestampMs
    lease_expires_at: DefsTimestampMs
    heartbeat_interval_ms: Annotated[StrictInt, Field(ge=1)]
    deadline_at: DefsTimestampMs | MISSING = MISSING
    input: dict[str, Any]


class JobRequest(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['job_request'], Field(min_length=1)]
    payload: Payload22


class ObservationBatchFrameObservation(DefsFrameKey, DefsCaptureTime):
    model_config = ConfigDict(
        extra='allow',
    )
    detections: list[ObservationBatchDetection]


class Payload29(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    window_start_ms: DefsTimestampMs
    window_end_ms: DefsTimestampMs
    calibration_version: DefsVersionId | MISSING = MISSING
    cameras: list[ObservationSummaryCameraSummary]
    archive_uri: DefsUri | MISSING = MISSING


class ObservationSummary(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['observation_summary'], Field(min_length=1)]
    payload: Payload29


class Payload30(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    stream_epoch: DefsTimestampMs
    state_sequence: Annotated[StrictInt, Field(ge=0)]
    model_version: Annotated[StrictStr, Field(max_length=256, min_length=1)]
    checkpoint_sha256: DefsSha256
    policy_version: Annotated[StrictStr, Field(max_length=128, min_length=1)]
    input_mode: Literal['FULL_FRAME']
    input_provenance: DefsInputProvenance
    people: Annotated[list[PpeStatePersonState], Field(max_length=64, min_length=0)]


class PpeState(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['ppe_state'], Field(min_length=1)]
    payload: Payload30


class RecordingManifestConsent(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    contains_people: StrictBool
    purpose: Literal['dev_test', 'dataset'] | MISSING = MISSING
    consent_ref: Annotated[StrictStr | MISSING, Field(min_length=1)] = MISSING
    scope: Annotated[list[RecordingManifestConsentScope] | MISSING, Field(min_length=1)] = (
        MISSING
    )
    retention_until: Annotated[
        StrictStr, Field(pattern='^\\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\\d|3[01])$')
    ]


class RecordingManifestFileEntry(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    path: RecordingManifestRelativePath
    bytes: Annotated[StrictInt, Field(ge=0)]
    sha256: DefsSha256


class RecordingManifestFrameLine(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    header: FrameHeader
    offset: Annotated[StrictInt, Field(ge=0)]
    size: Annotated[StrictInt, Field(ge=1)]


class RecordingManifestSegment(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    stream_epoch: Annotated[StrictInt, Field(ge=0)]
    video: RecordingManifestFileEntry
    frames: RecordingManifestFileEntry
    codec_info: RecordingManifestFileEntry
    frame_count: Annotated[StrictInt, Field(ge=0)]
    keyframe_count: Annotated[StrictInt, Field(ge=0)]
    frame_sequence_range: FrameSequenceRange | None
    capture_time_range_ms: RecordingManifestTimeRangeMs | None
    received_time_range_ms: RecordingManifestTimeRangeMs | None
    timestamp_sources: list[DefsTimestampSource]
    timestamp_uncertainty_ms_max: Annotated[StrictFloat | None, Field(ge=0.0)]
    keyframe_interval_s_p50: Annotated[StrictFloat | None, Field(ge=0.0)]
    camera_to_gateway_gap: RecordingManifestCameraToGatewayGap
    integrity: Integrity


class Payload32(DefsSeverityUpwardOnly, DefsNeedsCheckMatchesReasons):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    source: DefsRiskOrigin
    lifecycle: DefsRiskLifecycle
    verification: DefsRiskVerification
    severity: DefsSeverity
    confidence: DefsRiskConfidence
    needs_check: StrictBool
    confidence_reasons: DefsConfidenceReasons | MISSING = MISSING
    resume_count: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    world_ids: list[DefsWorldId]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    metrics: DefsRuleMetrics
    rationale: Annotated[StrictStr | None, Field(min_length=1)]
    proposed_severity: DefsSeverity | None
    gateway_id: DefsInstanceId | MISSING = MISSING
    config_version: DefsVersionId | MISSING = MISSING
    calibration_version: DefsVersionId | MISSING = MISSING
    scene_version: DefsVersionId | MISSING = MISSING
    raised_at: DefsTimestampMs | MISSING = MISSING
    updated_at: DefsTimestampMs | MISSING = MISSING
    resolved_at: DefsTimestampMs | None | MISSING = MISSING
    acks: list[DefsOperatorAck] | MISSING = MISSING
    delivery: Literal['REALTIME', 'DELAYED_ACTIVE', 'AUDIT_ONLY'] | MISSING = MISSING
    related_event_id: DefsUuidV7 | MISSING = MISSING
    resolution: DefsOperatorResolution | MISSING = MISSING
    escalations: list[DefsOperatorEscalation] | MISSING = MISSING


class RiskEvent(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['risk_event'], Field(min_length=1)]
    payload: Payload32


class Payload333(DefsNeedsCheckMatchesReasons):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    journal_offset: Annotated[StrictInt, Field(ge=0)]
    transition: Literal['RAISED', 'UPDATED', 'RESOLVED', 'ACKNOWLEDGED']
    occurred_at: DefsTimestampMs
    gateway_id: DefsInstanceId
    observation_time: DefsTimestampMs
    evaluated_at: DefsTimestampMs
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    config_version: DefsVersionId
    calibration_version: DefsVersionId
    scene_version: DefsVersionId
    source: Literal['LIVE']
    severity: DefsSeverity
    confidence: DefsRiskConfidence
    verification: DefsRiskVerification
    needs_check: StrictBool
    confidence_reasons: DefsConfidenceReasons | MISSING = MISSING
    world_ids: list[DefsWorldId]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    metrics: DefsRuleMetrics
    evidence_ref: DefsUuid | None
    resent: StrictBool
    resumed: StrictBool | MISSING = MISSING
    related_event_id: DefsUuidV7 | MISSING = MISSING
    ack: DefsOperatorAck | MISSING = MISSING
    resolution: DefsOperatorResolution | MISSING = MISSING
    escalation: DefsOperatorEscalation | MISSING = MISSING


class Payload334(Payload331, Payload333):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    journal_offset: Annotated[StrictInt, Field(ge=0)]
    transition: Literal['RAISED', 'UPDATED', 'RESOLVED', 'ACKNOWLEDGED']
    occurred_at: DefsTimestampMs
    gateway_id: DefsInstanceId
    observation_time: DefsTimestampMs
    evaluated_at: DefsTimestampMs
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    config_version: DefsVersionId
    calibration_version: DefsVersionId
    scene_version: DefsVersionId
    source: Literal['LIVE']
    severity: DefsSeverity
    confidence: DefsRiskConfidence
    verification: DefsRiskVerification
    needs_check: StrictBool
    confidence_reasons: DefsConfidenceReasons | MISSING = MISSING
    world_ids: list[DefsWorldId]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    metrics: DefsRuleMetrics
    evidence_ref: DefsUuid | None
    resent: StrictBool
    resumed: StrictBool | MISSING = MISSING
    related_event_id: DefsUuidV7 | MISSING = MISSING
    ack: DefsOperatorAck | MISSING = MISSING
    resolution: DefsOperatorResolution | MISSING = MISSING
    escalation: DefsOperatorEscalation | MISSING = MISSING


class Payload335(Payload332, Payload333):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    journal_offset: Annotated[StrictInt, Field(ge=0)]
    transition: Literal['RAISED', 'UPDATED', 'RESOLVED', 'ACKNOWLEDGED']
    occurred_at: DefsTimestampMs
    gateway_id: DefsInstanceId
    observation_time: DefsTimestampMs
    evaluated_at: DefsTimestampMs
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    config_version: DefsVersionId
    calibration_version: DefsVersionId
    scene_version: DefsVersionId
    source: Literal['LIVE']
    severity: DefsSeverity
    confidence: DefsRiskConfidence
    verification: DefsRiskVerification
    needs_check: StrictBool
    confidence_reasons: DefsConfidenceReasons | MISSING = MISSING
    world_ids: list[DefsWorldId]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    metrics: DefsRuleMetrics
    evidence_ref: DefsUuid | None
    resent: StrictBool
    resumed: StrictBool | MISSING = MISSING
    related_event_id: DefsUuidV7 | MISSING = MISSING
    ack: DefsOperatorAck | MISSING = MISSING
    resolution: DefsOperatorResolution | MISSING = MISSING
    escalation: DefsOperatorEscalation | MISSING = MISSING


class Payload336(Payload331, Payload333):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    journal_offset: Annotated[StrictInt, Field(ge=0)]
    transition: Literal['RAISED', 'UPDATED', 'RESOLVED', 'ACKNOWLEDGED']
    occurred_at: DefsTimestampMs
    gateway_id: DefsInstanceId
    observation_time: DefsTimestampMs
    evaluated_at: DefsTimestampMs
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    config_version: DefsVersionId
    calibration_version: DefsVersionId
    scene_version: DefsVersionId
    source: Literal['LIVE']
    severity: DefsSeverity
    confidence: DefsRiskConfidence
    verification: DefsRiskVerification
    needs_check: StrictBool
    confidence_reasons: DefsConfidenceReasons | MISSING = MISSING
    world_ids: list[DefsWorldId]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    metrics: DefsRuleMetrics
    evidence_ref: DefsUuid | None
    resent: StrictBool
    resumed: StrictBool | MISSING = MISSING
    related_event_id: DefsUuidV7 | MISSING = MISSING
    ack: DefsOperatorAck | MISSING = MISSING
    resolution: DefsOperatorResolution | MISSING = MISSING
    escalation: DefsOperatorEscalation | MISSING = MISSING


class Payload337(Payload332, Payload333):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    journal_offset: Annotated[StrictInt, Field(ge=0)]
    transition: Literal['RAISED', 'UPDATED', 'RESOLVED', 'ACKNOWLEDGED']
    occurred_at: DefsTimestampMs
    gateway_id: DefsInstanceId
    observation_time: DefsTimestampMs
    evaluated_at: DefsTimestampMs
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    config_version: DefsVersionId
    calibration_version: DefsVersionId
    scene_version: DefsVersionId
    source: Literal['LIVE']
    severity: DefsSeverity
    confidence: DefsRiskConfidence
    verification: DefsRiskVerification
    needs_check: StrictBool
    confidence_reasons: DefsConfidenceReasons | MISSING = MISSING
    world_ids: list[DefsWorldId]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    metrics: DefsRuleMetrics
    evidence_ref: DefsUuid | None
    resent: StrictBool
    resumed: StrictBool | MISSING = MISSING
    related_event_id: DefsUuidV7 | MISSING = MISSING
    ack: DefsOperatorAck | MISSING = MISSING
    resolution: DefsOperatorResolution | MISSING = MISSING
    escalation: DefsOperatorEscalation | MISSING = MISSING


class Payload33(RootModel[Payload334 | Payload335 | Payload336 | Payload337]):
    root: Payload334 | Payload335 | Payload336 | Payload337


class RiskEventTransition(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['risk_event_transition'], Field(min_length=1)]
    payload: Payload33


class RiskPlanRequestLayout(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    layout_id: Annotated[StrictStr, Field(min_length=1)]
    layout_version: DefsVersionId
    coordinate_frame: DefsCoordinateFrame
    zones: list[RiskPlanRequestZone]
    work_surfaces: list[RiskPlanRequestWorkSurface]
    work_positions: list[RiskPlanRequestWorkPosition] | MISSING = MISSING


class RiskPlanRequestRuleSet(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    rules_version: DefsVersionId
    rule_versions: Annotated[list[RiskPlanRequestRuleVersionRef], Field(min_length=1)]


class Payload35(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    request_id: DefsUuid | None
    source: Literal['PLANNED']
    status: Literal['OK', 'ERROR']
    computed_at: DefsTimestampMs
    engine_version: Annotated[StrictStr, Field(min_length=1)]
    evaluation_time: DefsTimestampMs | MISSING = MISSING
    layout_id: Annotated[StrictStr | MISSING, Field(min_length=1)] = MISSING
    layout_version: DefsVersionId | MISSING = MISSING
    rules: RiskPlanRequestRuleSet | MISSING = MISSING
    items: list[RiskPlanResultRiskItem] | MISSING = MISSING
    error: RiskPlanResultError | MISSING = MISSING
    positions: list[RiskPlanResultEvaluatedPosition] | MISSING = MISSING
    no_evaluated_positions: StrictBool | MISSING = MISSING


class RiskPlanResult(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['risk_plan_result'], Field(min_length=1)]
    source_id: Annotated[Literal['risk-engine-01'], Field(min_length=1)]
    payload: Payload35


class RunRecord(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    schema_version: DefsSemver
    run_id: Annotated[StrictStr, Field(pattern='^[A-Za-z0-9._-]+$')]
    experiment_id: Literal['EXP-01', 'EXP-02', 'INT-01', 'VAL-01']
    e_gate: Literal['E-GATE-0', 'E-GATE-1', 'E-GATE-2', 'E-GATE-3', 'E-GATE-4'] | MISSING = (
        MISSING
    )
    jira_key: StrictStr
    owner: StrictStr
    git_commit: Annotated[StrictStr, Field(pattern='^[0-9a-f]{7,40}$')]
    branch: StrictStr
    dirty_worktree: StrictBool | MISSING = MISSING
    env_hash: StrictStr
    model_version: StrictStr
    weights_hash: DefsSha256 | MISSING = MISSING
    config: Config
    input_provenance: DefsInputProvenance
    gpu: Gpu
    started_at: DefsTimestampMs
    finished_at: DefsTimestampMs | MISSING = MISSING
    status: Literal['SUCCEEDED', 'FAILED', 'ABORTED']
    failure_reason: StrictStr | MISSING = MISSING
    experiment_state: Literal['PLAN', 'BASELINE', 'ITERATE', 'BENCHMARK', 'REVIEW', 'ADOPT', 'HOLD', 'REJECT']
    metrics: dict[str, StrictFloat | StrictStr | StrictBool | None]
    performance: Performance | MISSING = MISSING
    results: list[Result]
    cost: Cost | MISSING = MISSING
    review: Review | None | MISSING = MISSING
    notes: StrictStr | MISSING = MISSING


class Quality1(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    wall_deviation_m: WallDeviationM | None
    desk_top_height_error_m: StrictFloat | None | MISSING = MISSING
    desk_position_error_m: StrictFloat | None | MISSING = MISSING
    coobserved_point_disagreement_median_m: StrictFloat | None | MISSING = MISSING
    scale_factor_vs_floor_plan: StrictFloat | None | MISSING = MISSING
    observed_surface_ratio: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    previous_fill_ratio: Annotated[StrictFloat | None | MISSING, Field(ge=0.0, le=1.0)] = (
        MISSING
    )
    unrevealed_ratio: Annotated[StrictFloat | None | MISSING, Field(ge=0.0, le=1.0)] = (
        MISSING
    )
    camera_background_quality: dict[str, SceneRegenerateCameraRegenerateQuality] | MISSING = (
        MISSING
    )
    failure_cases: list[StrictStr] | MISSING = MISSING
    notes: StrictStr | MISSING = MISSING


class SceneOutput(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    schema_version: DefsSemver
    scene_version: DefsVersionId
    state: DefsSceneState
    geometry_role: Literal['VISUAL']
    created_at: DefsTimestampMs
    experiment_id: StrictStr | MISSING = MISSING
    calibration_version: DefsVersionId
    coordinate_frame: DefsCoordinateFrame
    input_provenance: DefsInputProvenance
    reproducibility: DefsReproducibility
    method: Method
    assets: Annotated[list[Asset], Field(min_length=1)]
    quality: Quality1
    performance: Performance1 | MISSING = MISSING


class Ack1(DefsOperatorAck):
    model_config = ConfigDict(
        extra='allow',
    )
    channel: Literal['SITE_BUTTON']
    actor_id: Annotated[StrictStr, Field(min_length=1)]


class Payload37(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    ack: Ack1


class SiteAck(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['site_ack'], Field(min_length=1)]
    payload: Payload37


class SiteSetupArtifacts(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    calibration_document: SiteSetupObjectRef | MISSING = MISSING
    preview_scene: SiteSetupObjectRef | MISSING = MISSING
    scene: SiteSetupObjectRef | MISSING = MISSING
    camera_frames: Annotated[
        list[SiteSetupCameraFrameRef] | MISSING, Field(max_length=8)
    ] = MISSING


class Input(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    calibration_version: DefsVersionId
    calibration_ref: DefsObjectUri
    calibration_hash: DefsSha256
    images: Annotated[list[Image], Field(max_length=16, min_length=2)]
    input_provenance: DefsInputProvenance
    clean_background_bundle_id: Annotated[
        StrictStr | MISSING, Field(pattern='^bundle-[0-9a-f]{32}$')
    ] = MISSING
    regenerate_quality: SceneRegenerateRegenerateQuality | MISSING = MISSING


class Payload38(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    bundle_id: Annotated[StrictStr, Field(pattern='^bundle-[0-9a-f]{32}$')]
    input: Input
    request_id: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING


class StaticReconstructSubmit(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['static_reconstruct_submit'], Field(min_length=1)]
    payload: Payload38


class Payload40(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    gateway_id: DefsInstanceId
    public_ip: Annotated[
        StrictStr,
        Field(
            pattern='^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$'
        ),
    ]
    ports: Annotated[list[Tier0EndpointPortMapping], Field(min_length=2)]
    tls_public_key_sha256: DefsSha256
    registered_at: DefsTimestampMs
    pod_id: Annotated[StrictStr | MISSING, Field(pattern='^[A-Za-z0-9-]+$')] = MISSING
    datacenter: Annotated[StrictStr | MISSING, Field(min_length=1)] = MISSING


class Tier0Endpoint(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['tier0_endpoint'], Field(min_length=1)]
    payload: Payload40


class Frame2(DefsCaptureTime):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    frame_uri: DefsObjectUri
    estimated_capture_time_ms: DefsTimestampMs
    timestamp_source: DefsTimestampSource
    timestamp_uncertainty_ms: DefsDurationMs
    local_copy: DefsPodLocalFrame | MISSING = MISSING
    face_policy: Literal['BLURRED', 'EXCLUDED']


class VlmRationaleInput(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt, Field(ge=1)]
    rule_id: Annotated[StrictStr, Field(min_length=1)]
    rule_version: Annotated[StrictInt, Field(ge=1)]
    severity: DefsSeverity
    confidence: DefsRiskConfidence
    needs_check: StrictBool
    world_ids: list[DefsWorldId]
    zone_id: Annotated[StrictStr | None, Field(min_length=1)]
    metrics: DefsRuleMetrics
    observation_time: DefsTimestampMs
    frames: Annotated[list[Frame2], Field(min_length=1)]
    structured_state: dict[str, Any]
    world_state_ref: DefsUuid | MISSING = MISSING


class VlmZoneDraftDraftItem(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    candidate_id: Annotated[StrictStr, Field(max_length=128, min_length=1)]
    candidate_kind: VlmZoneDraftCandidateKind
    zone_kind: Annotated[StrictStr, Field(max_length=64, min_length=1)]
    eval_type: VlmZoneDraftEvalType
    polygon_xy: Annotated[list[DefsVec2], Field(max_length=256, min_length=3)]
    z_min_m: StrictFloat
    z_max_m: StrictFloat
    top_height_m: TopHeightM | None | MISSING = MISSING
    obb: VlmZoneDraftObb3d | None | MISSING = MISSING
    recommended_name: Annotated[StrictStr, Field(max_length=160, min_length=1)]
    recommended_severity: VlmZoneDraftDraftSeverity
    rationale: Annotated[StrictStr, Field(max_length=500, min_length=1)]
    vlm_status: VlmZoneDraftVlmOpinionStatus
    evidence_cameras: list[DefsCameraId]
    confidence: Confidence | None | MISSING = MISSING


class VlmZoneDraftFrameInput(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    frame_uri: DefsObjectUri
    local_copy: DefsPodLocalFrame | MISSING = MISSING
    face_policy: Literal['BLURRED', 'EXCLUDED']
    estimated_capture_time_ms: DefsTimestampMs | MISSING = MISSING
    timestamp_source: DefsTimestampSource | MISSING = MISSING
    timestamp_uncertainty_ms: DefsDurationMs | MISSING = MISSING


class VlmZoneDraftGeometryCandidate(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    candidate_id: Annotated[StrictStr, Field(max_length=128, min_length=1)]
    candidate_kind: VlmZoneDraftCandidateKind
    polygon_xy: Annotated[list[DefsVec2], Field(max_length=256, min_length=3)]
    z_min_m: StrictFloat
    z_max_m: StrictFloat
    top_height_m: TopHeightM | None | MISSING = MISSING
    obb: VlmZoneDraftObb3d | None | MISSING = MISSING
    area_m2: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    point_count: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING


class VlmZoneDraftInput(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    site_id: DefsVersionId
    setup_session_id: DefsUuid
    calibration_version: DefsVersionId
    calibration_ref: DefsObjectUri | MISSING = MISSING
    calibration_hash: DefsSha256 | MISSING = MISSING
    preview_scene_version: DefsVersionId
    preview_scene_uri: DefsObjectUri | MISSING = MISSING
    pointcloud_uri: DefsObjectUri | MISSING = MISSING
    geometry_candidates: Annotated[
        list[VlmZoneDraftGeometryCandidate] | MISSING, Field(max_length=64)
    ] = MISSING
    frames: Annotated[list[VlmZoneDraftFrameInput] | MISSING, Field(max_length=8)] = (
        MISSING
    )


class VlmZoneDraftResult(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    setup_session_id: DefsUuid
    site_id: DefsVersionId
    calibration_version: DefsVersionId
    preview_scene_version: DefsVersionId
    safety_use: Literal[False]
    status: Literal['SUCCEEDED', 'NO_OPINION']
    model_version: ModelVersion | None
    prompt_version: DefsVersionId
    latency_ms: DefsDurationMs
    drafts: Annotated[list[VlmZoneDraftDraftItem], Field(max_length=64)]


class Payload48(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    t_latest_global: DefsTimestampMs
    calibration_version: DefsVersionId
    scene_version: DefsVersionId
    config_version: DefsVersionId | MISSING = MISSING
    input_provenance: DefsInputProvenance | MISSING = MISSING
    tracks: list[WorldStateWorldTrack]


class WorldState(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['world_state'], Field(min_length=1)]
    payload: Payload48


class Track(WorldStateWorldTrack):
    per_camera: list[WorldTracksCameraObservation]


class WorldTracks(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    schema_version: DefsSemver
    t: DefsTimestampMs
    provider_source: DefsTrackSource
    provider_version: Annotated[StrictStr, Field(min_length=1)]
    calibration_version: DefsVersionId
    scene_version: DefsVersionId | MISSING = MISSING
    coordinate_frame: DefsCoordinateFrame
    tracks: list[Track]


class Payload(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    event_id: DefsUuidV7
    event_sequence: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING
    ack: DefsOperatorAck


class AlarmStop(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['alarm_stop'], Field(min_length=1)]
    payload: Payload


class Payload1(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    world_id: DefsWorldId
    stream_epoch: DefsTimestampMs
    identity_revision: Annotated[StrictInt, Field(ge=0)]
    appearance_version: Annotated[StrictInt, Field(ge=0)]
    previous_appearance_version: Annotated[StrictInt | None, Field(ge=0)]
    created_at_ms: DefsTimestampMs
    calibration_version: DefsVersionId
    model_version: Annotated[StrictStr, Field(max_length=256, min_length=1)]
    input_provenance: DefsInputProvenance
    mesh: Mesh
    skeleton: Skeleton
    shape: Shape
    texture: Texture | None
    surface_source: SurfaceSource
    face_policy: Literal['OBSERVED', 'BLURRED', 'EXCLUDED']


class AvatarAsset(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['avatar_asset'], Field(min_length=1)]
    payload: Payload1


class AvatarFrameAvatar(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    world_id: DefsWorldId
    identity_revision: Annotated[StrictInt, Field(ge=0)]
    capture_time_ms: DefsTimestampMs
    received_at_ms: DefsTimestampMs
    calibration_version: DefsVersionId
    appearance_version: Annotated[StrictInt, Field(ge=0)]
    joint_source: Literal['inferred']
    pose: DefsMhrPose
    asset: AvatarFrameViewerAsset | None


class Payload3(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    stream_epoch: DefsTimestampMs
    batch_sequence: Annotated[StrictInt, Field(ge=0)]
    calibration_version: DefsVersionId
    scene_version: DefsVersionId | MISSING = MISSING
    model_version: Annotated[StrictStr, Field(max_length=256, min_length=1)]
    input_provenance: DefsInputProvenance
    dropped_poses: Annotated[StrictInt, Field(ge=0)]
    poses: Annotated[
        list[AvatarPoseBatchPersonPose], Field(max_length=64, min_length=1)
    ]


class AvatarPoseBatch(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['avatar_pose_batch'], Field(min_length=1)]
    payload: Payload3


class Frame(DefsCaptureTime):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    frame_uri: DefsObjectUri
    estimated_capture_time_ms: DefsTimestampMs
    timestamp_source: DefsTimestampSource
    timestamp_uncertainty_ms: DefsDurationMs


class BevFloorplanInput(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    calibration_version: DefsVersionId
    calibration_ref: DefsObjectUri
    calibration_hash: DefsSha256
    frame_source: DefsFrameSource
    frames: Annotated[list[Frame], Field(min_length=1)]
    scene_version: DefsVersionId | MISSING = MISSING


class Raster(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    width: Annotated[StrictInt, Field(ge=1)]
    height: Annotated[StrictInt, Field(ge=1)]
    meters_per_pixel: Annotated[StrictFloat, Field(gt=0.0)]
    origin_xy: DefsVec2
    origin: Literal['TOP_LEFT_PIXEL_OUTER_CORNER']
    col_axis: Literal['+X']
    row_axis: Literal['-Y']
    pixel_to_world: DefsMat3
    world_bounds: WorldBounds


class BevFloorplanResult(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    image_uri: DefsObjectUri
    image_sha256: DefsSha256
    content_type: Literal['image/png']
    metadata_uri: DefsObjectUri
    calibration_version: DefsVersionId
    scene_version: DefsVersionId | None
    created_at: DefsTimestampMs
    purpose: Literal['OVERVIEW_DISPLAY_ONLY']
    safety_use: Literal[False]
    coordinate_frame: CoordinateFrame
    raster: Raster
    cameras_used: Annotated[list[DefsCameraId], Field(min_length=1)]
    cameras_skipped: dict[DefsCameraId, CamerasSkippedAdditionalProperty]
    coverage_ratio: Annotated[StrictFloat, Field(ge=0.0, le=1.0)]
    room_outline: RoomOutline | None
    input_provenance: DefsInputProvenance


class CalibrationCameraCalibration(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    image_size: Annotated[list[ImageSizeItem], Field(max_length=2, min_length=2)]
    K: DefsMat3
    distortion: Distortion
    R: DefsMat3
    t: DefsVec3
    camera_height_m: StrictFloat | MISSING = MISSING
    reprojection_error_px: ReprojectionErrorPx | MISSING = MISSING


class CalibrationScaleEstimation(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    scale_source: CalibrationScaleSource
    applied_scale: Annotated[StrictFloat, Field(gt=0.0)]
    height_only_scale: Annotated[StrictFloat, Field(gt=0.0)]
    prop_only_scale: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING
    joint_scale: Annotated[StrictFloat | MISSING, Field(gt=0.0)] = MISSING
    floor_offset_m: StrictFloat | MISSING = MISSING
    disagreement_ratio: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    disagreement_tolerance: Annotated[StrictFloat | MISSING, Field(gt=0.0, le=1.0)] = (
        MISSING
    )
    disagreement_warning: StrictBool | MISSING = MISSING
    warning_message: Annotated[StrictStr | MISSING, Field(max_length=500)] = MISSING
    outliers: list[Outlier] | MISSING = MISSING
    residuals: CalibrationScaleResiduals | MISSING = MISSING


class CalibrationStatus(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    calibration_state: DefsCalibrationState
    calibration_version: DefsVersionId | None
    previous_calibration_version: DefsVersionId | None
    provider_source: DefsCalibrationSource | None
    reason: Literal['INITIAL', 'RECALIBRATED', 'STATE_CHANGED', 'DRIFT_DETECTED', 'STREAM_CHANGED'] | None
    effective_at: DefsTimestampMs | None
    updated_at: DefsTimestampMs | None
    cameras: list[CalibrationStatusCalibrationReprojectionItem]
    document_uploaded: StrictBool
    latest_uploaded_version: DefsVersionId | None
    camera_poses: Annotated[
        list[CalibrationStatusCalibratedCameraPose], Field(max_length=16)
    ]


class CameraPose(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    R: DefsMat3
    t: DefsVec3


class Payload4(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    world_frame_id: DefsVersionId | MISSING = MISSING
    camera_poses: Annotated[list[CameraPose] | MISSING, Field(min_length=1)] = MISSING
    calibration_version: DefsVersionId
    previous_calibration_version: DefsVersionId | None
    calibration_state: DefsCalibrationState
    provider_source: DefsCalibrationSource
    reason: Literal['INITIAL', 'RECALIBRATED', 'STATE_CHANGED', 'DRIFT_DETECTED', 'STREAM_CHANGED']
    effective_at: DefsTimestampMs
    calibration_ref: DefsUri
    calibration_hash: DefsSha256
    cameras: list[Camera] | MISSING = MISSING


class CalibrationUpdate(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['calibration_update'], Field(min_length=1)]
    payload: Payload4


class CctvPreview(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    kind: Literal['hello', 'codec', 'status']
    protocol: Literal['simai-cctv-preview/1']
    cameras: Annotated[list[DefsCameraId] | MISSING, Field(max_length=16, min_length=1)] = (
        MISSING
    )
    codec_info: CodecInfo | MISSING = MISSING
    codec_string: Annotated[StrictStr | MISSING, Field(pattern='^avc1\\.[0-9A-F]{6}$')] = (
        MISSING
    )
    state: Literal['live', 'waiting_source', 'viewer_limit', 'monthly_quota'] | MISSING = (
        MISSING
    )
    monthly_sent_bytes: Annotated[StrictInt | MISSING, Field(ge=0)] = MISSING
    monthly_quota_bytes: Annotated[StrictInt | MISSING, Field(ge=1)] = MISSING


class Rules(ConfigBundleComponent):
    model_config = ConfigDict(
        extra='allow',
    )
    rule_versions: Annotated[list[RuleVersion], Field(min_length=1)]


class Components(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    zones: ConfigBundleComponent
    rules: Rules
    calibration: ConfigBundleComponent
    operational_geometry: ConfigBundleComponent
    ppe_policy: ConfigBundleComponent | MISSING = MISSING


class Payload8(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    config_version: DefsVersionId
    previous_config_version: DefsVersionId | None | MISSING = MISSING
    issued_at: DefsTimestampMs
    bundle_uri: DefsUri
    bundle_hash: DefsSha256
    components: Components


class ConfigBundle(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['config_bundle'], Field(min_length=1)]
    payload: Payload8


class CoverageGuide(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    schema_version: Literal['1.0.0']
    source_id: DefsInstanceId
    computed_at_ms: DefsTimestampMs
    valid_until_ms: DefsTimestampMs
    capture_skew_ms: CaptureSkewMs | None
    params: CoverageGuideCoverageParams
    cameras: Annotated[
        list[CoverageGuideCameraStatusItem], Field(max_length=4, min_length=4)
    ]
    pairs: Annotated[
        list[CoverageGuidePairMatchItem], Field(max_length=6, min_length=6)
    ]
    connectivity: Literal['CONNECTED', 'DISCONNECTED', 'UNKNOWN']
    components: Annotated[list[Component], Field(max_length=4, min_length=1)]
    guides: Annotated[list[CoverageGuideCameraGuideItem], Field(max_length=12)]


class HumanMeshOutput2(HumanMeshOutputPoseFrame):
    model_config = ConfigDict(
        extra='allow',
    )
    schema_version: DefsSemver
    record_type: Literal['pose_frame']


class HumanMeshOutputAvatarAsset(HumanMeshOutputCommonFields):
    model_config = ConfigDict(
        extra='allow',
    )
    record_type: Literal['avatar_asset']
    appearance_version: Annotated[StrictInt, Field(ge=0)]
    previous_appearance_version: StrictInt | None | MISSING = MISSING
    created_at: DefsTimestampMs
    mesh_geometry: MeshGeometry
    shape: Shape1
    texture: Texture1 | None | MISSING = MISSING
    surface_source: SurfaceSource1
    face_policy: Literal['EXCLUDED', 'BLURRED']
    observation_summary: ObservationSummary1
    consent_ref: StrictStr | MISSING = MISSING


class Payload28(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    calibration_version: DefsVersionId
    joint_layout: DefsJointLayout
    frames: Annotated[list[ObservationBatchFrameObservation], Field(min_length=1)]


class ObservationBatch(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['observation_batch'], Field(min_length=1)]
    payload: Payload28


class RecordingManifestCamera(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    camera_id: DefsCameraId
    source: RecordingManifestSource
    settings: RecordingManifestSettings
    timestamp_source: DefsTimestampSource
    capture_time_model: RecordingManifestCaptureTimeModel
    frame_count: Annotated[StrictInt, Field(ge=0)]
    capture_time_range_ms: RecordingManifestTimeRangeMs | None
    reconnects: Annotated[StrictInt, Field(ge=0)]
    open_failures: Annotated[StrictInt, Field(ge=0)]
    dropped_before_keyframe: Annotated[StrictInt, Field(ge=0)]
    errors: list[StrictStr]
    events: RecordingManifestFileEntry | None
    segments: list[RecordingManifestSegment]


class Payload34(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    request_id: DefsUuid
    source: Literal['PLANNED']
    evaluation_time: DefsTimestampMs
    base_config_version: DefsVersionId | None
    rules: RiskPlanRequestRuleSet
    layout: RiskPlanRequestLayout


class RiskPlanRequest(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['risk_plan_request'], Field(min_length=1)]
    source_id: Annotated[Literal['ec2'], Field(min_length=1)]
    payload: Payload34


class JointBa(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    anchor_camera_id: Annotated[StrictStr, Field(max_length=160, min_length=1)]
    converged: StrictBool
    stage_seconds: Annotated[StrictFloat, Field(ge=0.0)]
    valid_points: Annotated[StrictInt, Field(ge=0)]
    holdout_worsening_tolerance_px: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = (
        MISSING
    )
    pair_holdout: dict[str, PairHoldout] | MISSING = MISSING
    pair_holdout_median_px: (
        dict[constr(pattern=r'^[A-Za-z0-9_-]{1,64}-[A-Za-z0-9_-]{1,64}$', strict=True), PairHoldoutMedianPxAdditionalProperty] | MISSING
    ) = MISSING
    scale_source: CalibrationScaleSource | MISSING = MISSING
    scale_estimation: CalibrationScaleEstimation | MISSING = MISSING


class SiteSetupMetrics(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    stage_seconds: dict[Literal['COLLECTING_BACKGROUND', 'INITIAL_POSE_AND_WORLD', 'JOINT_BA_AND_VERIFY', 'PREVIEW_AND_VLM_DRAFT', 'READY_FOR_ZONES', 'BAKING_TEXTURE'], StageSecondsAdditionalProperty] | MISSING = (
        MISSING
    )
    total_seconds: Annotated[StrictFloat | MISSING, Field(ge=0.0)] = MISSING
    joint_ba: JointBa | MISSING = MISSING


class SiteSetupProgress(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    setup_session_id: DefsUuid
    status: Literal['COLLECTING_BACKGROUND', 'INITIAL_POSE_AND_WORLD', 'JOINT_BA_AND_VERIFY', 'PREVIEW_AND_VLM_DRAFT', 'READY_FOR_ZONES', 'BAKING_TEXTURE', 'COMPLETED']
    progress_pct: Annotated[StrictFloat, Field(ge=0.0, le=100.0)]
    background_collection_pct: dict[str, BackgroundCollectionPctAdditionalProperty] | MISSING = (
        MISSING
    )
    calibration_version: Annotated[
        StrictStr | MISSING, Field(max_length=160, min_length=1)
    ] = MISSING
    preview_scene_version: Annotated[
        StrictStr | MISSING, Field(max_length=160, min_length=1)
    ] = MISSING
    scene_version: Annotated[StrictStr | MISSING, Field(max_length=160, min_length=1)] = (
        MISSING
    )
    config_version: Annotated[StrictStr | MISSING, Field(max_length=160, min_length=1)] = (
        MISSING
    )
    zone_drafts_ref: Annotated[StrictStr | MISSING, Field(max_length=160, min_length=1)] = (
        MISSING
    )
    height_checks: SiteSetupHeightChecks | MISSING = MISSING
    scale_source: CalibrationScaleSource | MISSING = MISSING
    scale_estimation: CalibrationScaleEstimation | MISSING = MISSING
    object_cross_checks: list[ObjectCrossCheck] | MISSING = MISSING
    metrics: SiteSetupMetrics | MISSING = MISSING
    artifacts: SiteSetupArtifacts | MISSING = MISSING


class SiteSetupResult(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    setup_session_id: DefsUuid
    status: Literal['COMPLETED']
    progress_pct: Literal[100]
    background_collection_pct: dict[str, BackgroundCollectionPctAdditionalProperty] | MISSING = (
        MISSING
    )
    calibration_version: Annotated[StrictStr, Field(max_length=160, min_length=1)]
    preview_scene_version: Annotated[StrictStr, Field(max_length=160, min_length=1)]
    scene_version: Annotated[StrictStr, Field(max_length=160, min_length=1)]
    config_version: Annotated[StrictStr | MISSING, Field(max_length=160, min_length=1)] = (
        MISSING
    )
    zone_drafts_ref: Annotated[StrictStr | MISSING, Field(max_length=160, min_length=1)] = (
        MISSING
    )
    height_checks: SiteSetupHeightChecks
    scale_source: CalibrationScaleSource | MISSING = MISSING
    scale_estimation: CalibrationScaleEstimation | MISSING = MISSING
    object_cross_checks: list[ObjectCrossCheck] | MISSING = MISSING
    metrics: SiteSetupMetrics | MISSING = MISSING
    artifacts: SiteSetupArtifacts | MISSING = MISSING


class Payload2(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    input_provenance: DefsInputProvenance
    avatars: Annotated[list[AvatarFrameAvatar], Field(max_length=64, min_length=1)]


class AvatarFrame(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['avatar_frame'], Field(min_length=1)]
    payload: Payload2


class Calibration(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    schema_version: DefsSemver
    calibration_version: DefsVersionId
    provider_source: DefsCalibrationSource
    provider_version: StrictStr | MISSING = MISSING
    state: DefsCalibrationState
    created_at: DefsTimestampMs
    coordinate_frame: DefsCoordinateFrame
    scale_basis: CalibrationScaleBasis
    scale_source: CalibrationScaleSource | MISSING = MISSING
    scale_estimation: CalibrationScaleEstimation | MISSING = MISSING
    world_axes_source: Literal['WALLS', 'WALL_AND_FLOOR', 'MANUAL_FLOOR_POINTS', 'CAMERA_LAYOUT'] | MISSING = (
        MISSING
    )
    scale_cross_check_basis: Annotated[
        list[CalibrationScaleBasis] | MISSING, Field(min_length=1)
    ] = MISSING
    extrinsic_convention: Literal['OPENCV_WORLD_TO_CAMERA']
    cameras: Annotated[list[CalibrationCameraCalibration], Field(min_length=1)]
    validation: Validation | MISSING = MISSING
    content_hash: DefsSha256 | MISSING = MISSING
    previous_calibration_version: DefsVersionId | MISSING = MISSING
    setup_session_id: DefsUuid | MISSING = MISSING


class HumanMeshOutput1(HumanMeshOutputAvatarAsset):
    model_config = ConfigDict(
        extra='allow',
    )
    schema_version: DefsSemver
    record_type: Literal['avatar_asset']


class HumanMeshOutput(RootModel[HumanMeshOutput1 | HumanMeshOutput2]):
    root: HumanMeshOutput1 | HumanMeshOutput2


class Payload19(BaseModel):
    model_config = ConfigDict(
        extra='allow',
    )
    job_id: Annotated[StrictStr, Field(min_length=1)]
    lease_id: DefsUuid
    progress: Annotated[StrictFloat | None | MISSING, Field(ge=0.0, le=1.0)] = MISSING
    site_setup: SiteSetupProgress | MISSING = MISSING


class JobHeartbeat(Envelope):
    model_config = ConfigDict(
        extra='allow',
    )
    msg_type: Annotated[Literal['job_heartbeat'], Field(min_length=1)]
    payload: Payload19


class RecordingManifest(
    RecordingManifestConsentRequirement,
    RecordingManifestInputKindsCoverSources,
):
    model_config = ConfigDict(
        extra='forbid',
    )
    manifest_version: Annotated[StrictStr, Field(pattern='^1\\.\\d+\\.\\d+$')]
    session_id: Annotated[StrictStr, Field(pattern='^[a-z0-9][a-z0-9-]*$')]
    scenario: Annotated[StrictStr, Field(min_length=1)]
    description: StrictStr | MISSING = MISSING
    notes: StrictStr | MISSING = MISSING
    input_kinds: Annotated[list[RecordingManifestInputKind], Field(min_length=1)]
    is_test_input: StrictBool
    started_at_ms: DefsTimestampMs
    ended_at_ms: DefsTimestampMs | None
    requested_duration_s: Annotated[StrictFloat | None | MISSING, Field(gt=0.0)] = (
        MISSING
    )
    common_capture_window_ms: RecordingManifestTimeRangeMs | None
    calibration_version: DefsVersionId | None
    consent: dict[str, Any] | None
    storage: Storage
    recorded_by: RecordedBy
    deleted_at_ms: DefsTimestampMs | MISSING = MISSING
    cameras: Annotated[list[RecordingManifestCamera], Field(min_length=1)]


class Contracts(BaseModel):
    model_config = ConfigDict(
        extra='forbid',
    )
    alarm_stop: AlarmStop | MISSING = MISSING
    avatar_asset: AvatarAsset | MISSING = MISSING
    avatar_frame: AvatarFrame | MISSING = MISSING
    avatar_pose_batch: AvatarPoseBatch | MISSING = MISSING
    bev_floorplan: BevFloorplan | MISSING = MISSING
    calibration: Calibration | MISSING = MISSING
    calibration_status: CalibrationStatus | MISSING = MISSING
    calibration_update: CalibrationUpdate | MISSING = MISSING
    cctv_preview: CctvPreview | MISSING = MISSING
    cctv_remote_control: CctvRemoteControl | MISSING = MISSING
    clock_sync_request: ClockSyncRequest | MISSING = MISSING
    clock_sync_response: ClockSyncResponse | MISSING = MISSING
    codec_info: CodecInfo | MISSING = MISSING
    config_ack: ConfigAck | MISSING = MISSING
    config_bundle: ConfigBundle | MISSING = MISSING
    coverage_guide: CoverageGuide | MISSING = MISSING
    envelope: Envelope | MISSING = MISSING
    event_ack: EventAck | MISSING = MISSING
    event_close: EventClose | MISSING = MISSING
    event_close_result: EventCloseResult | MISSING = MISSING
    event_escalate: EventEscalate | MISSING = MISSING
    event_escalate_result: EventEscalateResult | MISSING = MISSING
    event_frame_ready: EventFrameReady | MISSING = MISSING
    event_state_snapshot: EventStateSnapshot | MISSING = MISSING
    frame_header: FrameHeader | MISSING = MISSING
    gait_features: GaitFeatures | MISSING = MISSING
    health: Health | MISSING = MISSING
    human_mesh_output: HumanMeshOutput | MISSING = MISSING
    incident_summary: IncidentSummary | MISSING = MISSING
    job_callback: JobCallback | MISSING = MISSING
    job_heartbeat: JobHeartbeat | MISSING = MISSING
    job_lease_request: JobLeaseRequest | MISSING = MISSING
    job_lease_status: JobLeaseStatus | MISSING = MISSING
    job_request: JobRequest | MISSING = MISSING
    journal_ack: JournalAck | MISSING = MISSING
    laptop_heartbeat: LaptopHeartbeat | MISSING = MISSING
    local_alert: LocalAlert | MISSING = MISSING
    media_clip: MediaClip | MISSING = MISSING
    media_clip_retention: MediaClipRetention | MISSING = MISSING
    observation_batch: ObservationBatch | MISSING = MISSING
    observation_summary: ObservationSummary | MISSING = MISSING
    ppe_policy: PpePolicy | MISSING = MISSING
    ppe_state: PpeState | MISSING = MISSING
    recording_manifest: RecordingManifest | MISSING = MISSING
    risk_alert: RiskAlert | MISSING = MISSING
    risk_event: RiskEvent | MISSING = MISSING
    risk_event_transition: RiskEventTransition | MISSING = MISSING
    risk_plan_request: RiskPlanRequest | MISSING = MISSING
    risk_plan_result: RiskPlanResult | MISSING = MISSING
    run_record: RunRecord | MISSING = MISSING
    scene_compatibility: SceneCompatibility | MISSING = MISSING
    scene_output: SceneOutput | MISSING = MISSING
    scene_reference: SceneReference | MISSING = MISSING
    scene_regenerate: SceneRegenerate | MISSING = MISSING
    site_ack: SiteAck | MISSING = MISSING
    site_setup: SiteSetup | MISSING = MISSING
    static_reconstruct_submit: StaticReconstructSubmit | MISSING = MISSING
    static_reconstruct_submit_result: StaticReconstructSubmitResult | MISSING = MISSING
    tbm_report: TbmReport | MISSING = MISSING
    tier0_endpoint: Tier0Endpoint | MISSING = MISSING
    video_segment: VideoSegment | MISSING = MISSING
    viewer_health: ViewerHealth | MISSING = MISSING
    vlm_rationale: VlmRationale | MISSING = MISSING
    vlm_zone_draft: VlmZoneDraft | MISSING = MISSING
    watch_ack: WatchAck | MISSING = MISSING
    watch_alert: WatchAlert | MISSING = MISSING
    watch_binding_notice: WatchBindingNotice | MISSING = MISSING
    watch_escalation: WatchEscalation | MISSING = MISSING
    watch_heartbeat: WatchHeartbeat | MISSING = MISSING
    world_state: WorldState | MISSING = MISSING
    world_tracks: WorldTracks | MISSING = MISSING
