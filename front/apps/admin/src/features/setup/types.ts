import { z } from 'zod';

// 세팅 1단계 카메라 각도 헬퍼(GET /api/cameras/coverage-guides, S15P31A101-447, contracts/messages/coverage_guide.schema.json 1.0.0),
// 현장 세팅 세션·카메라 높이 기본값·재시도(GET /api/setup-sessions/camera-height-defaults, POST /api/setup-sessions,
// GET /api/setup-sessions/{id}, POST /api/setup-sessions/{id}/retry, S15P31A101-452),
// 및 VLM 구역 초안(GET /api/setup-sessions/{id}/zone-drafts, S15P31A101-455, contracts/messages/vlm_zone_draft.schema.json 1.0.0) 스키마.

export const cameraReceptionStatusSchema = z.enum(['OK', 'NO_VIDEO', 'STALE']);
export type CameraReceptionStatus = z.infer<typeof cameraReceptionStatusSchema>;

export const pairGradeSchema = z.enum(['ENOUGH', 'WEAK', 'LACK', 'UNKNOWN']);
export type PairGrade = z.infer<typeof pairGradeSchema>;

export const connectivitySchema = z.enum(['CONNECTED', 'DISCONNECTED', 'UNKNOWN']);
export type Connectivity = z.infer<typeof connectivitySchema>;

export const guideDirectionSchema = z.enum(['LEFT', 'RIGHT', 'UP', 'DOWN', 'NONE']);
export type GuideDirection = z.infer<typeof guideDirectionSchema>;

export const guideMagnitudeSchema = z.enum(['SMALL', 'MORE', 'NONE']);
export type GuideMagnitude = z.infer<typeof guideMagnitudeSchema>;

export const guideReasonCodeSchema = z.enum([
  'WEAK_PAIR_EDGE_CLUSTER',
  'NO_OVERLAP_VISIBLE',
  'OPTIONAL_IMPROVE',
]);
export type GuideReasonCode = z.infer<typeof guideReasonCodeSchema>;

const normalizedPointSchema = z.tuple([z.number().min(0).max(1), z.number().min(0).max(1)]);

export const coverageCameraItemSchema = z.object({
  camera_id: z.string().min(1),
  status: cameraReceptionStatusSchema,
  frame_ts_ms: z.number().nullable(),
});
export type CoverageCameraItem = z.infer<typeof coverageCameraItemSchema>;

export const coveragePairItemSchema = z.object({
  a: z.string().min(1),
  b: z.string().min(1),
  raw_matches: z.number().int().nonnegative().nullable(),
  inliers: z.number().int().nonnegative().nullable(),
  inlier_ratio: z.number().min(0).max(1).nullable(),
  grade: pairGradeSchema,
  centroid_a: normalizedPointSchema.nullable().optional().default(null),
  centroid_b: normalizedPointSchema.nullable().optional().default(null),
});
export type CoveragePairItem = z.infer<typeof coveragePairItemSchema>;

const pairTupleSchema = z.tuple([z.string().min(1), z.string().min(1)]);

export const coverageGuideItemSchema = z
  .object({
    camera_id: z.string().min(1),
    pair: pairTupleSchema.optional(),
    target_pair: pairTupleSchema.optional(),
    direction: guideDirectionSchema,
    magnitude: guideMagnitudeSchema,
    reason_code: guideReasonCodeSchema.optional(),
    reason: guideReasonCodeSchema.optional(),
  })
  .transform((item) => ({
    camera_id: item.camera_id,
    pair: item.pair ?? item.target_pair ?? ([item.camera_id, item.camera_id] as [string, string]),
    direction: item.direction,
    magnitude: item.magnitude,
    reason_code: item.reason_code ?? item.reason ?? 'WEAK_PAIR_EDGE_CLUSTER',
  }));
export type CoverageGuideItem = z.infer<typeof coverageGuideItemSchema>;

export const coverageParamsSchema = z.object({
  image_width_px: z.number().int(),
  max_keypoints: z.number().int(),
  ransac_threshold_px: z.number(),
  enough_min_inliers: z.number().int(),
  weak_min_inliers: z.number().int(),
  min_inlier_ratio: z.number(),
  intrinsics_source: z.enum(['STORED', 'DEFAULT']),
});

export const coverageGuidePayloadSchema = z.object({
  schema_version: z.string(),
  source_id: z.string().min(1),
  computed_at_ms: z.number(),
  valid_until_ms: z.number(),
  capture_skew_ms: z.number().nullable(),
  params: coverageParamsSchema.optional(),
  cameras: z.array(coverageCameraItemSchema).min(1),
  pairs: z.array(coveragePairItemSchema),
  connectivity: connectivitySchema,
  components: z.array(z.array(z.string().min(1))),
  guides: z.array(coverageGuideItemSchema),
});
export type CoverageGuidePayload = z.infer<typeof coverageGuidePayloadSchema>;

export const coverageRelayStatusSchema = z.enum(['EMPTY', 'OK', 'STALE']);
export type CoverageRelayStatus = z.infer<typeof coverageRelayStatusSchema>;

export const coverageGuidesResponseSchema = z.object({
  calibration_version: z.string().nullable().optional().default(null),
  guides: z.array(z.unknown()).optional().default([]),
  site_id: z.string().optional(),
  status: coverageRelayStatusSchema.optional().default('EMPTY'),
  stale: z.boolean().optional().default(false),
  active_demand: z.boolean().optional(),
  demand_ttl_ms: z.number().optional(),
  last_admin_query_at_ms: z.number().nullable().optional(),
  demand_until_ms: z.number().nullable().optional(),
  stale_after_ms: z.number().optional(),
  received_at_ms: z.number().nullable().optional().default(null),
  age_ms: z.number().nullable().optional().default(null),
  latest: coverageGuidePayloadSchema.nullable().optional().default(null),
});
export type CoverageGuidesResponse = z.infer<typeof coverageGuidesResponseSchema>;

export const cameraHeightDefaultsResponseSchema = z.object({
  site_id: z.string().min(1),
  height_tolerance_cm: z.number().positive(),
  camera_heights_m: z.record(z.string(), z.number().positive().nullable()),
  all_present: z.boolean(),
});
export type CameraHeightDefaultsResponse = z.infer<typeof cameraHeightDefaultsResponseSchema>;

export const setupSessionStageSchema = z.enum([
  'COLLECTING_BACKGROUND',
  'INITIAL_POSE_AND_WORLD',
  'JOINT_BA_AND_VERIFY',
  'PREVIEW_AND_VLM_DRAFT',
  'READY_FOR_ZONES',
  'BAKING_TEXTURE',
  'COMPLETED',
  'FAILED',
]);
export type SetupSessionStage = z.infer<typeof setupSessionStageSchema>;

export const heightCheckItemSchema = z.object({
  input_height_m: z.number(),
  restored_height_m: z.number().nullable().optional().default(null),
  difference_cm: z.number().nullable().optional().default(null),
  within_tolerance: z.boolean(),
  guidance: z.string().nullable().optional().default(null),
});
export type HeightCheckItem = z.infer<typeof heightCheckItemSchema>;

export const heightChecksSchema = z.record(z.string(), heightCheckItemSchema);
export type HeightChecksMap = z.infer<typeof heightChecksSchema>;

/**
 * 세팅이 왜 기다리는지(GET /api/setup-sessions/{id} 의 wait_reason, S15P31A101-583, docs/design/setup-wait-reason.md).
 * 서버가 이미 가진 상태(작업·대여·워커 대여 요청 시각·워커 반납 사유·Tier 0 health 지연·배경 수집률)로 고른다. 정상 진행이면 null.
 * 코드: NO_WORKER·WORKER_BUSY_OTHER_JOB·WAITING_FOR_WORKER·WORKER_PAUSED_TIER0·WORKER_NOT_POLLING·WORKER_STARTING·
 * BACKGROUND_INCOMPLETE. 서버가 뒤에 새 코드를 더해도 화면이 깨지지 않게 code 는 문자열로 받는다.
 */
export const setupWaitReasonSchema = z.object({
  code: z.string().min(1),
  since_ms: z.number(),
  waited_s: z.number().optional(),
  /** WORKER_PAUSED_TIER0 근거: 워커가 직접 반납(WORKER_RELEASE) 또는 Tier 0 health 지연 모름으로 추정(TIER0_HEALTH). */
  basis: z.string().nullable().optional().default(null),
  detail: z.string().nullable().optional().default(null),
  /** 최근 Tier 0 health 의 지연 최댓값(관측값, 참고). site_setup 은 지연이 높다는 것만으로는 막히지 않는다(S15P31A101-566). */
  tier0_lag_ms: z.number().nullable().optional().default(null),
  worker_last_seen_ms: z.number().nullable().optional().default(null),
  other_job_type: z.string().nullable().optional().default(null),
  previous_error_code: z.string().nullable().optional().default(null),
  cameras: z
    .array(z.object({ camera_id: z.string().min(1), collection_pct: z.number() }))
    .optional()
    .default([]),
});
export type SetupWaitReason = z.infer<typeof setupWaitReasonSchema>;

export const setupSessionResponseSchema = z.object({
  setup_session_id: z.string().min(1),
  site_id: z.string().min(1),
  work_unit_id: z.number().int().positive(),
  retry_of: z.string().nullable().optional().default(null),
  job_id: z.string().nullable().optional().default(null),
  status: setupSessionStageSchema,
  stage: setupSessionStageSchema.optional(),
  progress_pct: z.number().min(0).max(100),
  camera_heights_m: z.record(z.string(), z.number().positive()),
  height_tolerance_cm: z.number().positive().optional(),
  background_collection_pct: z.record(z.string(), z.number()).optional(),
  height_checks: heightChecksSchema.nullable().optional().default(null),
  height_warnings: z.array(z.string()).optional().default([]),
  object_cross_checks: z.unknown().nullable().optional(),
  calibration_version: z.string().nullable().optional().default(null),
  preview_scene_version: z.string().nullable().optional().default(null),
  scene_version: z.string().nullable().optional().default(null),
  config_version: z.string().nullable().optional().default(null),
  /** 그 세션 시작 뒤 그 현장의 첫 구역 발행(POST /api/zones/commit) 시각(ms). 없으면 null. */
  zones_committed_at: z.number().nullable().optional().default(null),
  zone_drafts_ref: z.string().nullable().optional().default(null),
  failure_code: z.string().nullable().optional().default(null),
  failure_message: z.string().nullable().optional().default(null),
  requested_by: z.number().nullable().optional(),
  started_at_ms: z.number().optional(),
  ready_at_ms: z.number().nullable().optional(),
  completed_at_ms: z.number().nullable().optional(),
  updated_at_ms: z.number().optional(),
  wait_reason: setupWaitReasonSchema.nullable().optional(),
});
export type SetupSessionResponse = z.infer<typeof setupSessionResponseSchema>;

export interface CreateSetupSessionBody {
  work_unit_id: number;
  camera_heights_m: Record<string, number>;
}

export interface RetrySetupSessionBody {
  camera_heights_m?: Record<string, number>;
}

// ---- VLM 구역 초안(GET /api/setup-sessions/{id}/zone-drafts, S15P31A101-455) ----

export const zoneDraftCandidateKindSchema = z.enum([
  'ELEVATED_WORK_SURFACE',
  'OPENING',
  'FLOOR_EDGE',
]);
export type ZoneDraftCandidateKind = z.infer<typeof zoneDraftCandidateKindSchema>;

export const zoneDraftEvalTypeSchema = z.enum(['GROUND_FOOTPRINT', 'VOLUME_3D']);
export type ZoneDraftEvalType = z.infer<typeof zoneDraftEvalTypeSchema>;

export const zoneDraftSeveritySchema = z.enum(['CRITICAL', 'HIGH']);
export type ZoneDraftSeverity = z.infer<typeof zoneDraftSeveritySchema>;

export const vlmOpinionStatusSchema = z.enum(['VLM', 'NO_OPINION']);
export type VlmOpinionStatus = z.infer<typeof vlmOpinionStatusSchema>;

export const zoneDraftObbSchema = z.object({
  center: z.tuple([z.number(), z.number(), z.number()]),
  size: z.tuple([z.number(), z.number(), z.number()]),
  yaw_rad: z.number(),
});
export type ZoneDraftObb = z.infer<typeof zoneDraftObbSchema>;

export const zoneDraftPolygonPointSchema = z.tuple([z.number(), z.number()]);

export const zoneDraftItemSchema = z
  .object({
    draft_id: z.string().optional(),
    draft_index: z.number().int().nonnegative().optional(),
    candidate_id: z.string().min(1),
    candidate_kind: zoneDraftCandidateKindSchema,
    zone_kind: z.string().min(1).optional().default('HAZARD_AREA'),
    eval_type: zoneDraftEvalTypeSchema.optional().default('GROUND_FOOTPRINT'),
    polygon_xy: z.array(zoneDraftPolygonPointSchema).min(3),
    z_min_m: z.number(),
    z_max_m: z.number(),
    top_height_m: z.number().nullable().optional().default(null),
    obb: zoneDraftObbSchema.nullable().optional().default(null),
    recommended_name: z.string().min(1),
    recommended_severity: zoneDraftSeveritySchema,
    rationale: z.string(),
    vlm_status: vlmOpinionStatusSchema.optional().default('VLM'),
    evidence_cameras: z.array(z.string()).optional().default([]),
    confidence: z.number().min(0).max(1).nullable().optional().default(null),
  })
  .transform((item) => ({
    ...item,
    no_opinion: item.vlm_status === 'NO_OPINION',
  }));
export type ZoneDraftItem = z.infer<typeof zoneDraftItemSchema>;
export type ZoneDraftItemInput = z.input<typeof zoneDraftItemSchema>;

export const zoneDraftRunStatusSchema = z.enum([
  'NOT_REQUESTED',
  'QUEUED',
  'SUCCEEDED',
  'NO_OPINION',
  'FAILED',
]);
export type ZoneDraftRunStatus = z.infer<typeof zoneDraftRunStatusSchema>;

export const setupZoneDraftsResponseSchema = z.object({
  draft_run_id: z.string().nullable().optional().default(null),
  setup_session_id: z.string().min(1),
  site_id: z.string().min(1),
  calibration_version: z.string().nullable().optional().default(null),
  preview_scene_version: z.string().nullable().optional().default(null),
  safety_use: z.literal(false).optional().default(false),
  job_id: z.string().nullable().optional().default(null),
  status: zoneDraftRunStatusSchema,
  model_version: z.string().nullable().optional().default(null),
  prompt_version: z.string().nullable().optional().default(null),
  latency_ms: z.number().nullable().optional().default(null),
  error_code: z.string().nullable().optional().default(null),
  error_message: z.string().nullable().optional().default(null),
  created_at_ms: z.number().nullable().optional().default(null),
  completed_at_ms: z.number().nullable().optional().default(null),
  drafts: z.array(zoneDraftItemSchema).optional().default([]),
});
export type SetupZoneDraftsResponse = z.infer<typeof setupZoneDraftsResponseSchema>;
export type SetupZoneDraftsResponseInput = z.input<typeof setupZoneDraftsResponseSchema>;
