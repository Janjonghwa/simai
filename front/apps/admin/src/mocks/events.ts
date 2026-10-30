import { EVENTS_TOPIC } from '@/lib/stomp';
import { problem, readLiveUser, unauthorized } from '@/mocks/problem';
import { nextMsgId } from '@/mocks/realtimeScenario';
import type { StompBroker } from '@/mocks/stompBroker';
import type { RiskEventMessage } from '@simai/contracts';
import { http, HttpResponse } from 'msw/http';

// 서버 이벤트 API(S15P31A101-121, -276) 대신 쓰는 mock. 시나리오 전이와 운영자 확인·상향을 한 저장소에 모은다.
// 순번(event_sequence)은 이 저장소가 매긴다. 확인(ACK)·운영자 종료·상향 승인도 전이라 순번을 올려 /topic/events 로 다시 보낸다.
// REST 모양은 실서버를 따른다: 목록은 Spring Page(최신순, 해소 포함, 열람 계정은 403), 오류는 ProblemDetail + code.

/** 해소된 이벤트를 목록에 남기는 시간(ms). */
const KEEP_RESOLVED_MS = 120_000;

/** GET /api/events 의 쪽 크기. 서버 기본 20, 최대 100. */
const DEFAULT_PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 100;
/** 쪽 번호·크기 질의로 받는 값: 음이 아닌 정수, 9자리까지. */
const PAGE_PARAM = /^\d{1,9}$/;

const CONTROL_ROOM = 'CONTROL_ROOM';
/** ACK 본문의 channel 로 받는 값(서버가 관제·반장 PWA 만 받는다). 없으면 CONTROL_ROOM. */
const ACK_CHANNELS = [CONTROL_ROOM, 'FOREMAN_PWA'] as const;
const DEFAULT_CLOSE_REASON = '운영자 종료';

type Payload = RiskEventMessage['payload'];
type AckActor = { id: string; role: 'ADMIN' | 'FOREMAN' };

const SEVERITY_ORDER: Record<Payload['severity'], number> = {
  CRITICAL: 3,
  HIGH: 2,
  CAUTION: 1,
  INFO: 0,
};

export interface EventsMock {
  /** 시나리오 전이를 저장소에 반영하고 보낼 메시지를 돌려준다. 앞선 확인·상향 기록은 이어 간다. */
  record(message: RiskEventMessage): RiskEventMessage;
  /** GET /api/events, POST /api/events/:id/ack, POST /api/events/:id/escalation */
  handlers: ReturnType<typeof http.get>[];
  /** 저장소의 이벤트 하나(클립 mock 이 읽는다). */
  find(eventId: string): Payload | undefined;
  /**
   * 관리자가 이벤트를 종료한 것으로 만든다(event_close 의 결과). 해소 전이에 resolution 을 싣고 /topic/events 로 보낸다.
   * 아직 화면 동작이 없어 시험과 개발 콘솔에서만 쓴다. 없거나 이미 해소됐으면 undefined.
   */
  close(eventId: string, actor: { id: string }, reason?: string): RiskEventMessage | undefined;
  /** 다음 ACK 하나를 토큰 만료(401)로 거절한다. 갱신 후 다시 보내기를 30분 기다리지 않고 본다(개발 콘솔 simaiMock). */
  expireAuth(): void;
}

// 서버는 비어 있는 선택 칸을 보내지 않는다(zone_id·rationale·proposed_severity 는 null 로 보낸다).
function toRest(payload: Payload): Record<string, unknown> {
  const present = Object.entries(payload).filter(
    ([key, value]) =>
      value !== undefined &&
      !(Array.isArray(value) && value.length === 0) &&
      !(key === 'resolved_at' && value === null),
  );
  return {
    zone_id: null,
    rationale: null,
    proposed_severity: null,
    ...Object.fromEntries(present),
  };
}

/** 최신순: 발생 시각 내림차순, 같으면 event_id 내림차순. */
const newestFirst = (a: Payload, b: Payload) =>
  (b.raised_at ?? 0) - (a.raised_at ?? 0) || b.event_id.localeCompare(a.event_id);

/** 쪽 질의 값. 없으면 기본값, 정수가 아니면 NaN. */
function readPageParam(params: URLSearchParams, name: string, fallback: number): number {
  const raw = params.get(name);
  if (raw === null) return fallback;
  return PAGE_PARAM.test(raw) ? Number(raw) : NaN;
}

/** 쪽 번호·크기 질의. 정수가 아니거나 범위 밖이면 undefined(서버는 400). */
function readPaging(params: URLSearchParams): { page: number; size: number } | undefined {
  const page = readPageParam(params, 'page', 0);
  const size = readPageParam(params, 'size', DEFAULT_PAGE_SIZE);
  if (Number.isNaN(page) || Number.isNaN(size) || size < 1 || size > MAX_PAGE_SIZE) {
    return undefined;
  }
  return { page, size };
}

/** 요청 본문 JSON. 읽을 수 없는 본문은 빈 객체로 본다. */
async function readJsonBody(request: Request): Promise<Record<string, unknown>> {
  const body: unknown = await request.json().catch(() => undefined);
  return typeof body === 'object' && body !== null ? Object.fromEntries(Object.entries(body)) : {};
}

/** 화면이 봤다는 순번이 서버가 모르는 값인가(정수가 아니거나 현재 순번보다 큼). 이전 순번은 받는다. */
const isUnknownSequence = (seen: unknown, current: number) =>
  seen !== undefined && (typeof seen !== 'number' || !Number.isInteger(seen) || seen > current);

export function createEventsMock({
  broker,
  now = Date.now,
}: {
  broker: StompBroker;
  now?: () => number;
}): EventsMock {
  const events = new Map<string, RiskEventMessage>();
  const decidedTargets = new Map<string, Payload['severity']>();
  const nextSequence = (eventId: string) => (events.get(eventId)?.payload.event_sequence ?? 0) + 1;
  let rejectNextAck = false;

  function prune(t: number) {
    for (const [id, { payload }] of events) {
      if (payload.lifecycle === 'RESOLVED' && (payload.updated_at ?? 0) < t - KEEP_RESOLVED_MS) {
        events.delete(id);
      }
    }
  }

  function record(message: RiskEventMessage): RiskEventMessage {
    const id = message.payload.event_id;
    const prev = events.get(id)?.payload;
    const acks = prev?.acks;
    const resumed =
      prev !== undefined && (message.payload.resume_count ?? 0) > (prev.resume_count ?? 0);
    const escalations = resumed
      ? message.payload.escalations
      : (prev?.escalations ?? message.payload.escalations);
    const severity =
      !resumed &&
      prev &&
      escalations?.length &&
      SEVERITY_ORDER[prev.severity] > SEVERITY_ORDER[message.payload.severity]
        ? prev.severity
        : message.payload.severity;
    const proposed =
      message.payload.proposed_severity &&
      SEVERITY_ORDER[message.payload.proposed_severity] > SEVERITY_ORDER[severity]
        ? message.payload.proposed_severity
        : null;
    const stored: RiskEventMessage = {
      ...message,
      payload: {
        ...message.payload,
        severity,
        proposed_severity: proposed,
        event_sequence: nextSequence(id),
        ...(acks ? { acks } : {}),
        ...(escalations ? { escalations } : {}),
      },
    };
    events.set(id, stored);
    prune(message.sent_at);
    return stored;
  }

  /** prev 에서 한 전이를 저장한다: 새 msg_id, 순번 +1, updated_at = t. change 는 msg_id 를 매긴 뒤에 만든다(id 번호 순서). */
  function advance(prev: RiskEventMessage, t: number, change: () => Partial<Payload>) {
    const eventId = prev.payload.event_id;
    const stored: RiskEventMessage = {
      ...prev,
      msg_id: nextMsgId(),
      sent_at: t,
      payload: {
        ...prev.payload,
        event_sequence: nextSequence(eventId),
        updated_at: t,
        ...change(),
      },
    };
    events.set(eventId, stored);
    return stored;
  }

  const acknowledge = (
    prev: RiskEventMessage,
    actor: AckActor,
    channel: (typeof ACK_CHANNELS)[number],
    t: number,
  ) =>
    advance(prev, t, () => ({
      acks: [
        ...(prev.payload.acks ?? []),
        {
          ack_id: nextMsgId(),
          channel,
          actor_role: actor.role,
          actor_id: actor.id,
          acked_at: t,
        },
      ],
    }));

  function list(request: Request) {
    // mock 로그인 토큰이나 실서버 토큰(혼합 모드)으로 누구인지 정한다. 목록은 반장·관리자만 본다(열람 계정은 403).
    const user = readLiveUser(request, now());
    if (!user) return unauthorized();
    if (user.role === 'VIEWER') {
      return problem(403, 'FORBIDDEN', '이벤트 목록을 볼 권한이 없습니다');
    }
    const paging = readPaging(new URL(request.url).searchParams);
    if (!paging) {
      return problem(400, 'INVALID_PAGE_REQUEST', '쪽 번호나 크기가 올바르지 않습니다');
    }
    const { page, size } = paging;
    const sorted = [...events.values()]
      .map((message) => message.payload)
      .sort(newestFirst)
      .map(toRest);
    return HttpResponse.json({
      content: sorted.slice(page * size, (page + 1) * size),
      totalElements: sorted.length,
      number: page,
      size,
    });
  }

  async function ack(eventId: string, request: Request) {
    // 만료된 실서버 토큰은 서버처럼 거절한다.
    const t = now();
    const user = readLiveUser(request, t);
    if (!user) return unauthorized();
    if (rejectNextAck) {
      rejectNextAck = false;
      return unauthorized();
    }
    if (user.role === 'VIEWER') return problem(403, 'FORBIDDEN', '확인 권한이 없습니다');

    const body = await readJsonBody(request);
    const channel = ACK_CHANNELS.find((c) => c === (body.channel ?? CONTROL_ROOM));
    if (!channel) return problem(400, 'INVALID_ACK_CHANNEL', '확인 경로가 올바르지 않습니다');
    const prev = events.get(eventId);
    if (!prev) return problem(404, 'EVENT_NOT_FOUND', '이벤트를 찾을 수 없습니다');
    if (prev.payload.lifecycle === 'RESOLVED') {
      return problem(409, 'EVENT_RESOLVED', '이미 해소된 이벤트입니다');
    }
    // 화면이 서버가 아직 모르는 순번을 봤다고 하면 거절한다.
    if (isUnknownSequence(body.event_sequence, prev.payload.event_sequence ?? 0)) {
      return problem(400, 'INVALID_EVENT_SEQUENCE', '이벤트 순번이 올바르지 않습니다');
    }

    const stored = acknowledge(prev, { id: user.id, role: user.role }, channel, t);
    broker.publish(EVENTS_TOPIC, stored);
    return HttpResponse.json(toRest(stored.payload));
  }

  async function escalate(eventId: string, request: Request) {
    const t = now();
    const user = readLiveUser(request, t);
    if (!user) return unauthorized();
    if (user.role !== 'ADMIN') {
      return problem(403, 'FORBIDDEN', '상향 의견을 처리할 권한이 없습니다');
    }

    const body = await readJsonBody(request);
    const rawAssessment = body.assessmentId ?? body.assessment_id;
    const assessmentId =
      typeof rawAssessment === 'string' && rawAssessment.trim() !== ''
        ? rawAssessment.trim()
        : typeof rawAssessment === 'number' && Number.isFinite(rawAssessment)
          ? rawAssessment
          : undefined;
    if (assessmentId === undefined) {
      return problem(400, 'INVALID_ASSESSMENT_ID', '평가 ID가 올바르지 않습니다');
    }

    const rawDecision = body.decision;
    const decision =
      rawDecision === 'APPROVED' || rawDecision === 'APPROVE'
        ? 'APPROVED'
        : rawDecision === 'REJECTED' || rawDecision === 'REJECT'
          ? 'REJECTED'
          : undefined;
    if (!decision) {
      return problem(400, 'INVALID_DECISION', '상향 결정 값이 올바르지 않습니다');
    }

    const prev = events.get(eventId);
    if (!prev) return problem(404, 'EVENT_NOT_FOUND', '이벤트를 찾을 수 없습니다');

    const note =
      typeof body.note === 'string' && body.note.trim() !== '' ? body.note.trim() : undefined;
    const assessmentKey = `${eventId}:${String(assessmentId)}`;
    const targetSeverity =
      prev.payload.proposed_severity ?? decidedTargets.get(assessmentKey) ?? prev.payload.severity;

    if (prev.payload.lifecycle === 'RESOLVED') {
      return HttpResponse.json({
        escalation_id: nextMsgId(),
        event_id: eventId,
        assessmentId,
        decision,
        target_severity: targetSeverity,
        actor_id: user.id,
        requested_at: t,
        decided_at: t,
        result: 'REJECTED',
        reject_reason: 'NOT_ACTIVE',
        ...(note ? { note } : {}),
      });
    }

    if (decision === 'REJECTED') {
      decidedTargets.set(assessmentKey, targetSeverity);
      events.set(eventId, {
        ...prev,
        payload: { ...prev.payload, proposed_severity: null },
      });
      return HttpResponse.json({
        event_id: eventId,
        assessmentId,
        decision: 'REJECTED',
        target_severity: targetSeverity,
        actor_id: user.id,
        requested_at: t,
        decided_at: t,
        ...(note ? { note } : {}),
      });
    }

    const existing = (prev.payload.escalations ?? []).find(
      (e) => e.assessment_job_id === String(assessmentId),
    );
    if (existing) {
      return HttpResponse.json({
        ...existing,
        event_id: eventId,
        assessmentId,
        decision: 'APPROVED',
        result: 'DUPLICATE',
        decided_at: t,
      });
    }

    if (
      targetSeverity === 'INFO' ||
      SEVERITY_ORDER[targetSeverity] <= SEVERITY_ORDER[prev.payload.severity]
    ) {
      return HttpResponse.json({
        escalation_id: nextMsgId(),
        event_id: eventId,
        assessmentId,
        decision: 'APPROVED',
        target_severity: targetSeverity,
        actor_id: user.id,
        requested_at: t,
        decided_at: t,
        result: 'REJECTED',
        reject_reason: 'NOT_UPWARD',
        ...(note ? { note } : {}),
      });
    }

    decidedTargets.set(assessmentKey, targetSeverity);
    const escalation = {
      escalation_id: nextMsgId(),
      basis: 'VLM_PROPOSAL' as const,
      assessment_job_id: String(assessmentId),
      target_severity: targetSeverity,
      channel: CONTROL_ROOM as 'CONTROL_ROOM',
      actor_role: 'ADMIN' as const,
      actor_id: user.id,
      requested_at: t,
      ...(note ? { note } : {}),
    };
    const stored = advance(prev, t, () => ({
      lifecycle: 'UPDATED',
      severity: targetSeverity,
      proposed_severity: null,
      escalations: [...(prev.payload.escalations ?? []), escalation],
    }));
    broker.publish(EVENTS_TOPIC, stored);
    return HttpResponse.json({
      ...escalation,
      event_id: eventId,
      assessmentId,
      decision: 'APPROVED',
      result: 'ACCEPTED',
      event_sequence: stored.payload.event_sequence,
      decided_at: t,
    });
  }

  function close(eventId: string, actor: { id: string }, reason = DEFAULT_CLOSE_REASON) {
    const prev = events.get(eventId);
    if (!prev || prev.payload.lifecycle === 'RESOLVED') return undefined;
    const t = now();
    const stored = advance(prev, t, () => ({
      lifecycle: 'RESOLVED',
      resolved_at: t,
      proposed_severity: null,
      resolution: {
        kind: 'OPERATOR_CLOSED',
        close_id: nextMsgId(),
        channel: CONTROL_ROOM,
        actor_role: 'ADMIN',
        actor_id: actor.id,
        requested_at: t,
        reason,
      },
    }));
    broker.publish(EVENTS_TOPIC, stored);
    return stored;
  }

  return {
    record,
    handlers: [
      http.get('*/api/events', ({ request }) => list(request)),
      http.post('*/api/events/:id/ack', ({ params, request }) => ack(String(params.id), request)),
      http.post('*/api/events/:id/escalation', ({ params, request }) =>
        escalate(String(params.id), request),
      ),
    ],
    find: (eventId) => events.get(eventId)?.payload,
    close,
    expireAuth: () => {
      rejectNextAck = true;
    },
  };
}
