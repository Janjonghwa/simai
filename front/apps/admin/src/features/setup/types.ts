import { z } from 'zod';

// 세팅 1단계 카메라 각도 헬퍼(GET /api/cameras/coverage-guides, S15P31A101-447, contracts/messages/coverage_guide.schema.json 1.0.0)
// 및 현장 세팅 세션·카메라 높이 기본값(GET /api/setup-sessions/camera-height-defaults, POST /api/setup-sessions, S15P31A101-452) 스키마.

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
  height_checks: z.unknown().nullable().optional(),
  height_warnings: z.array(z.string()).optional().default([]),
  object_cross_checks: z.unknown().nullable().optional(),
  calibration_version: z.string().nullable().optional().default(null),
  preview_scene_version: z.string().nullable().optional().default(null),
  scene_version: z.string().nullable().optional().default(null),
  config_version: z.string().nullable().optional().default(null),
  zone_drafts_ref: z.string().nullable().optional().default(null),
  failure_code: z.string().nullable().optional().default(null),
  failure_message: z.string().nullable().optional().default(null),
  requested_by: z.number().nullable().optional(),
  started_at_ms: z.number().optional(),
  ready_at_ms: z.number().nullable().optional(),
  completed_at_ms: z.number().nullable().optional(),
  updated_at_ms: z.number().optional(),
});
export type SetupSessionResponse = z.infer<typeof setupSessionResponseSchema>;

export interface CreateSetupSessionBody {
  work_unit_id: number;
  camera_heights_m: Record<string, number>;
}
