import { readMockUser } from '@/mocks/mockUser';
import { testPatternVideo } from '@/mocks/testPatternVideo';
import type { RiskEventPayload } from '@simai/contracts';
import { http, HttpResponse } from 'msw/http';

// 서버 클립 API(BE 곽나혁) 대신 쓰는 mock. 형식은 features/clips/types.ts 의 가정을 따르고, 클립 칸은 계약 media_clip 과 같다.
// 시나리오의 LIVE CRITICAL·HIGH 이벤트마다 카메라 3대의 클립을 만든다. 클립은 위험 구간(앞뒤 20초)이 끝나고 올라간 뒤에야
// 목록에 생긴다. 영상은 브라우저에서 녹화한 사람이 없는 시험 패턴(WebM)이다. 화면은 영상 형식에 기대지 않는다.

const PRE_MS = 20_000;
const POST_MS = 20_000;
/** 위험 구간이 끝난 뒤 올라가기까지(ms). 카메라마다 조금씩 늦다. */
const UPLOAD_MS = 4_000;
const CAMERAS = ['cam1', 'cam2', 'cam3'] as const;
/** 재생 주소 만료(ms). 짧게 두어 다시 받기를 볼 수 있다. */
const URL_TTL_MS = 60_000;
/** 보관 기간(명세 11.5, 14일). */
const KEEP_MS = 14 * 24 * 60 * 60 * 1000;

type Camera = (typeof CAMERAS)[number];

const problem = (status: number, detail: string, code: string) =>
  HttpResponse.json({ status, detail, code }, { status });

export interface ClipsMock {
  /** GET /api/events/:id/clips, GET /api/clips/:id, PUT /api/clips/:id/retention */
  handlers: (ReturnType<typeof http.get> | ReturnType<typeof http.put>)[];
}

export function createClipsMock({
  findEvent,
  now = Date.now,
  video = testPatternVideo,
}: {
  /** 이벤트 mock 저장소의 이벤트(events.find). */
  findEvent: (eventId: string) => RiskEventPayload | undefined;
  now?: () => number;
  /** 재생할 영상 주소. 기본은 브라우저에서 만든 시험 패턴이다. */
  video?: () => Promise<string>;
}): ClipsMock {
  /** 위험 구간(재개 횟수)마다 기준 시각. 처음 발생은 발생 시각, 재개는 그것을 처음 본 갱신 시각이다. */
  const anchors = new Map<string, number[]>();
  /** 관리자가 바꾼 보존 표시. */
  const held = new Map<string, boolean>();
  const ids = new Map<string, string>();
  const index = new Map<string, { eventId: string; resume: number; camera: Camera }>();
  let issued = 0;

  const clipIdOf = (eventId: string, resume: number, camera: Camera) => {
    const key = `${eventId}/${resume}/${camera}`;
    let id = ids.get(key);
    if (!id) {
      id = `0192a6f1-7c1e-7d3a-9f2d-${(++issued).toString(16).padStart(12, '0')}`;
      ids.set(key, id);
      index.set(id, { eventId, resume, camera });
    }
    return id;
  };

  function anchorsOf(event: RiskEventPayload): number[] {
    const list = anchors.get(event.event_id) ?? [event.raised_at ?? now()];
    while (list.length <= (event.resume_count ?? 0)) list.push(event.updated_at ?? now());
    anchors.set(event.event_id, list);
    return list;
  }

  function clipOf(event: RiskEventPayload, resume: number, camera: Camera, t: number) {
    const anchor = anchorsOf(event)[resume];
    if (anchor === undefined) return undefined;
    // cam2 의 첫 구간은 링 버퍼에 앞부분이 없어 늦게 시작하고, cam3 는 구간 안에 프레임 빈칸이 있다.
    const truncatedStart = camera === 'cam2' && resume === 0;
    const start = anchor - PRE_MS + (truncatedStart ? 6_000 : 0);
    const end = anchor + POST_MS;
    const uploaded = end + UPLOAD_MS + CAMERAS.indexOf(camera) * 1_500;
    if (t < uploaded) return undefined;
    const clipId = clipIdOf(event.event_id, resume, camera);
    const created = end + 500;
    return {
      clip_id: clipId,
      event_id: event.event_id,
      gateway_id: 'tier0-01',
      event_sequence: resume === 0 ? 1 : (event.event_sequence ?? 1),
      resume_count: resume,
      rule_id: event.rule_id,
      severity: event.severity === 'CRITICAL' ? 'CRITICAL' : 'HIGH',
      source: 'LIVE',
      camera_id: camera,
      stream_epochs: [1],
      anchor_capture_ms: anchor,
      requested_start_ms: anchor - PRE_MS,
      requested_end_ms: end,
      clip_start_capture_ms: start,
      clip_end_capture_ms: end,
      frame_count: Math.round(((end - start) / 1000) * 15),
      starts_with_keyframe: true,
      truncated_start: truncatedStart,
      truncated_end: false,
      has_gaps: camera === 'cam3',
      content_type: 'video/h264',
      container: 'h264-annexb',
      size_bytes: 1_200_000,
      sha256: clipId.replaceAll('-', '').padEnd(64, '0'),
      face_redaction: 'NONE',
      redactor_version: null,
      created_at_ms: created,
      delete_after_ms: created + KEEP_MS,
      // CRITICAL 이벤트에 연결된 클립은 자동 보존이다(명세 11.5).
      retention_hold: event.severity === 'CRITICAL' || held.get(clipId) === true,
      object_key: `clips/${event.event_id}/${resume}/${camera}.h264`,
      uploaded_at_ms: uploaded,
      deleted_at_ms: null,
    };
  }

  function clipsOf(event: RiskEventPayload, t: number) {
    if (event.source !== 'LIVE' || (event.severity !== 'CRITICAL' && event.severity !== 'HIGH')) {
      return [];
    }
    return anchorsOf(event).flatMap((_, resume) =>
      CAMERAS.flatMap((camera) => clipOf(event, resume, camera, t) ?? []),
    );
  }

  function current(clipId: string) {
    const at = index.get(clipId);
    const event = at && findEvent(at.eventId);
    const clip = at && event ? clipOf(event, at.resume, at.camera, now()) : undefined;
    return event && clip ? { event, clip } : undefined;
  }

  // 명세 12.6: 클립은 관리자만 본다. 만료된 실서버 토큰(혼합 모드)은 서버처럼 거절한다.
  function denied(request: Request) {
    const user = readMockUser(request.headers.get('Authorization'));
    if (!user || (user.expiresAt !== undefined && user.expiresAt <= now())) {
      return problem(401, '로그인이 필요합니다', 'UNAUTHORIZED');
    }
    if (user.role !== 'ADMIN') return problem(403, '권한이 없습니다', 'FORBIDDEN');
    return undefined;
  }

  const handlers = [
    http.get('*/api/events/:id/clips', ({ params, request }) => {
      const refused = denied(request);
      if (refused) return refused;
      const event = findEvent(String(params.id));
      if (!event) return problem(404, '이벤트가 없습니다', 'NOT_FOUND');
      return HttpResponse.json({
        contract: 'DEMO_UNCONFIRMED',
        event_id: event.event_id,
        clips: clipsOf(event, now()),
      });
    }),
    http.get('*/api/clips/:id', async ({ params, request }) => {
      const refused = denied(request);
      if (refused) return refused;
      const found = current(String(params.id));
      if (!found) return problem(404, '클립이 없습니다', 'NOT_FOUND');
      const url = await video().catch(() => undefined);
      if (!url) return problem(500, '시험 영상을 만들지 못했습니다', 'MOCK_VIDEO_FAILED');
      return HttpResponse.json({
        contract: 'DEMO_UNCONFIRMED',
        clip_id: found.clip.clip_id,
        url,
        expires_at_ms: now() + URL_TTL_MS,
        content_type: 'video/webm',
      });
    }),
    http.put('*/api/clips/:id/retention', async ({ params, request }) => {
      const refused = denied(request);
      if (refused) return refused;
      const body = (await request.json().catch(() => undefined)) as
        { retention_hold?: unknown } | undefined;
      if (typeof body?.retention_hold !== 'boolean') {
        return problem(400, 'retention_hold 가 필요합니다', 'BAD_REQUEST');
      }
      const clipId = String(params.id);
      const found = current(clipId);
      if (!found) return problem(404, '클립이 없습니다', 'NOT_FOUND');
      if (found.event.severity === 'CRITICAL' && !body.retention_hold) {
        return problem(409, 'CRITICAL 이벤트 클립은 자동 보존입니다', 'AUTO_RETAINED');
      }
      held.set(clipId, body.retention_hold);
      return HttpResponse.json(current(clipId)?.clip);
    }),
  ];

  return { handlers };
}
