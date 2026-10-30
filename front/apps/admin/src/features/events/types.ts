import type { RiskEventPayload } from '@simai/contracts';
import { z } from 'zod';

// 이벤트 REST 응답(명세 12.6 GET /api/events, POST /api/events/{id}/ack). 항목은 risk_event payload 와 같다.
// 계약 검증기는 봉투(envelope) 단위라 여기서는 화면이 쓰는 필드만 검사한다. 아래 타입 검사가 계약과 어긋나면 알려 준다.
// zod 객체는 모르는 칸을 버린다. REST 항목이 캐시에 들어가 STOMP 항목과 같이 쓰이므로, 서버가 보내는 칸은 다 적어 둔다.

const severity = z.enum(['CRITICAL', 'HIGH', 'CAUTION', 'INFO']);

const operatorAck = z.object({
  ack_id: z.string(),
  channel: z.string(),
  actor_role: z.string(),
  actor_id: z.string(),
  acked_at: z.number(),
});

// 운영자 종료(event_close)의 기록. 서버는 전부 보내지만, 칸 하나가 없다고 목록 전체를 버리지 않게 화면이 안 쓰는 칸은 선택으로 둔다.
const operatorResolution = z.object({
  kind: z.literal('OPERATOR_CLOSED'),
  close_id: z.string().optional(),
  channel: z.string().optional(),
  actor_role: z.string().optional(),
  actor_id: z.string().optional(),
  requested_at: z.number(),
  reason: z.string(),
});

const operatorEscalation = z.object({
  escalation_id: z.string().optional(),
  basis: z.string().optional(),
  assessment_job_id: z.string().optional(),
  target_severity: severity,
  channel: z.string().optional(),
  actor_role: z.string().optional(),
  actor_id: z.string().optional(),
  requested_at: z.number(),
  note: z.string().optional(),
});

export const eventItemSchema = z.object({
  event_id: z.string(),
  event_sequence: z.number().int().optional(),
  rule_id: z.string(),
  rule_version: z.number().int().optional(),
  source: z.enum(['LIVE', 'PLANNED', 'REPLAY', 'WHAT_IF']),
  lifecycle: z.enum(['RAISED', 'UPDATED', 'RESOLVED']),
  verification: z.enum(['VERIFIED', 'UNVERIFIABLE']),
  severity,
  confidence: z.enum(['HIGH', 'LOW']),
  needs_check: z.boolean(),
  confidence_reasons: z.array(z.string()).optional(),
  resume_count: z.number().int().optional(),
  world_ids: z.array(z.string()),
  zone_id: z.string().nullable(),
  metrics: z.record(z.string(), z.number()),
  rationale: z.string().nullable(),
  proposed_severity: severity.nullable(),
  gateway_id: z.string().optional(),
  config_version: z.string().optional(),
  calibration_version: z.string().optional(),
  scene_version: z.string().optional(),
  related_event_id: z.string().optional(),
  raised_at: z.number().optional(),
  updated_at: z.number().optional(),
  resolved_at: z.number().nullable().optional(),
  acks: z.array(operatorAck).optional(),
  delivery: z.enum(['REALTIME', 'DELAYED_ACTIVE', 'AUDIT_ONLY']).optional(),
  resolution: operatorResolution.optional(),
  escalations: z.array(operatorEscalation).optional(),
});

/** 화면이 쓰는 이벤트 값. STOMP 의 risk_event payload 도 그대로 넣을 수 있다. */
export type EventItem = z.infer<typeof eventItemSchema>;
export type Severity = EventItem['severity'];

// 계약(risk_event payload)이 바뀌어 EventItem 에 넣을 수 없게 되면 타입 검사가 여기서 실패한다.
export const contractFitsEventItem = (event: RiskEventPayload): EventItem => event;

/**
 * GET /api/events 응답(Spring Page). 다음 쪽이 있는지 보는 칸(totalElements·number·size)은 서버가 안 보내도 목록은 받는다.
 * 그러면 한 쪽이 꽉 찼는지로 다음 쪽을 판단한다(api.ts).
 */
export const eventsPageSchema = z.object({
  content: z.array(eventItemSchema),
  totalElements: z.number().int().optional(),
  number: z.number().int().optional(),
  size: z.number().int().optional(),
});

export type EventsPage = z.infer<typeof eventsPageSchema>;

/** 운영자 확인(ACK) 한 건. */
export type EventAck = NonNullable<EventItem['acks']>[number];
