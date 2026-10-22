import type { RiskEventPayload } from '@simai/contracts';
import { z } from 'zod';

// 이벤트 REST 응답(명세 12.6 GET /api/events). 서버 OpenAPI 가 나오기 전까지 가정이고, 항목은 risk_event payload 와 같다고 본다.
// 계약 검증기는 봉투(envelope) 단위라 여기서는 화면이 쓰는 필드만 검사한다. 아래 타입 검사가 계약과 어긋나면 알려 준다.

const severity = z.enum(['CRITICAL', 'HIGH', 'CAUTION', 'INFO']);

const operatorAck = z.object({
  ack_id: z.string(),
  channel: z.string(),
  actor_role: z.string(),
  actor_id: z.string(),
  acked_at: z.number(),
});

export const eventItemSchema = z.object({
  event_id: z.string(),
  event_sequence: z.number().int().optional(),
  rule_id: z.string(),
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
  raised_at: z.number().optional(),
  updated_at: z.number().optional(),
  resolved_at: z.number().nullable().optional(),
  acks: z.array(operatorAck).optional(),
  delivery: z.enum(['REALTIME', 'DELAYED_ACTIVE', 'AUDIT_ONLY']).optional(),
  resolution: z
    .object({ kind: z.literal('OPERATOR_CLOSED'), reason: z.string(), requested_at: z.number() })
    .optional(),
  escalations: z
    .array(z.object({ target_severity: severity, requested_at: z.number() }))
    .optional(),
});

/** 화면이 쓰는 이벤트 값. STOMP 의 risk_event payload 도 그대로 넣을 수 있다. */
export type EventItem = z.infer<typeof eventItemSchema>;
export type Severity = EventItem['severity'];

// 계약(risk_event payload)이 바뀌어 EventItem 에 넣을 수 없게 되면 타입 검사가 여기서 실패한다.
export const contractFitsEventItem = (event: RiskEventPayload): EventItem => event;

/** GET /api/events 응답(Spring Page 가정). 다른 칸은 쓰지 않는다. */
export const eventsPageSchema = z.object({ content: z.array(eventItemSchema) });
