import { z } from 'zod';

// GPU 켜기·끄기·GPU 설정 REST(S15P31A101-702, 서버 domain/gpu, 관리자만, 계약 밖).
// 상태는 G1 GET /api/gpu/sessions/current(-699)에 -702 가 더한 launch·power·stop_block 을 함께 읽는다.
// 다른 티켓이 더하는 필드(예: -701 gateway)는 모르는 채로 받는다(looseObject).

/** 점 하나의 서버 상태. STARTING = 켠 뒤 G1 등록 전, PREPARING = 등록했지만 준비 전 */
export const gpuPowerSchema = z.enum(['OFF', 'STARTING', 'PREPARING', 'CONNECTED', 'ERROR']);
export type GpuPower = z.infer<typeof gpuPowerSchema>;

const nullableNumber = z.number().nullable();
const nullableString = z.string().nullable();

export const gpuSessionSchema = z.looseObject({
  session_id: z.string(),
  state: z.enum(['ACTIVE', 'ENDED']),
  stage: z.string(),
  stage_reason_code: nullableString.optional(),
  provider: z.string(),
  instance_id: z.string(),
  gpu_type: z.string(),
  region: nullableString,
  price_per_hour_usd: nullableNumber,
  registered_at_ms: z.number(),
  elapsed_ms: z.number(),
  tier0_link: z.enum(['CONNECTED', 'DISCONNECTED']),
});
export type GpuSession = z.infer<typeof gpuSessionSchema>;

export const gpuLaunchSchema = z.looseObject({
  launch_id: z.string(),
  state: z.enum(['REQUESTED', 'RUNNING', 'FAILED', 'STOPPED']),
  provider: z.string(),
  gpu_type: z.string(),
  region: z.string(),
  cloud_type: z.string(),
  interruptible: z.boolean(),
  price_per_hour_usd: nullableNumber,
  requested_by: nullableString,
  requested_at_ms: z.number(),
  instance_id: nullableString,
  ended_at_ms: nullableNumber,
  end_reason: nullableString,
  failure_code: nullableString,
});
export type GpuLaunch = z.infer<typeof gpuLaunchSchema>;

export const gpuStatusSchema = z.looseObject({
  session: gpuSessionSchema.nullable(),
  server_time_ms: z.number(),
  launch: gpuLaunchSchema.nullable().optional(),
  power: gpuPowerSchema.optional(),
  stop_block: z.enum(['LIVE', 'SETUP']).nullable().optional(),
});
export type GpuStatus = z.infer<typeof gpuStatusSchema>;

export const gpuStockSchema = z.enum(['HIGH', 'MEDIUM', 'LOW', 'NONE']);
export type GpuStock = z.infer<typeof gpuStockSchema>;

export const gpuOffersSchema = z.object({
  provider: z.string(),
  cloud_type: z.string(),
  interruptible: z.boolean(),
  max_price_per_hour_usd: nullableNumber,
  gpu_types: z.array(
    z.object({ gpu_type: z.string(), name: z.string(), memory_gb: nullableNumber }),
  ),
  regions: z.array(z.object({ region: z.string(), name: z.string(), preferred: z.boolean() })),
  cells: z.array(
    z.object({
      gpu_type: z.string(),
      region: z.string(),
      stock: gpuStockSchema,
      price_per_hour_usd: nullableNumber,
      within_cap: z.boolean(),
    }),
  ),
  default: z.object({ gpu_type: z.string(), region: z.string() }).nullable(),
  fetched_at_ms: z.number(),
});
export type GpuOffers = z.infer<typeof gpuOffersSchema>;
export type GpuOfferCell = GpuOffers['cells'][number];

export const gpuSettingsSchema = z.object({
  version_id: nullableNumber,
  provider: z.enum(['RUNPOD', 'AWS']),
  preferred_gpu_types: z.array(z.string()),
  preferred_regions: z.array(z.string()),
  cloud_type: z.enum(['SECURE', 'COMMUNITY']),
  interruptible: z.boolean(),
  max_price_per_hour_usd: nullableNumber,
  auto_stop_idle_minutes: z.number(),
  runpod_template_id: nullableString,
  created_at_ms: nullableNumber,
});
export type GpuSettings = z.infer<typeof gpuSettingsSchema>;
export type GpuSettingsBody = Omit<GpuSettings, 'version_id' | 'created_at_ms'>;

export const gpuSettingsResponseSchema = z.object({
  settings: gpuSettingsSchema,
  providers: z.array(z.object({ id: z.string(), configured: z.boolean() })),
});
export type GpuSettingsResponse = z.infer<typeof gpuSettingsResponseSchema>;

/** 켜기 요청. 비우면 서버가 선호 순서 × 재고 × 가격 상한으로 고른다 */
export interface GpuLaunchBody {
  gpu_type?: string;
  region?: string;
}
