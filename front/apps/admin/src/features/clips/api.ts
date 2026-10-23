import { clipUrlSchema, eventClipsSchema } from '@/features/clips/types';
import type { Http } from '@/lib/http';
import { z } from 'zod';

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.

/** 이벤트의 카메라별 클립 목록(가정 경로, 관리자). */
export function requestEventClips(http: Http, eventId: string, signal?: AbortSignal) {
  return http(`/api/events/${encodeURIComponent(eventId)}/clips`, {
    schema: eventClipsSchema,
    signal,
  });
}

/** 클립 재생 주소. EC2 가 이 요청마다 조회 기록을 남긴다(명세 14장). */
export function requestClipUrl(http: Http, clipId: string) {
  return http(`/api/clips/${encodeURIComponent(clipId)}`, { schema: clipUrlSchema });
}

/** 보존 표시 붙이기·떼기(관리자). 응답 형식은 정해지지 않아 받기만 하고, 화면은 목록을 다시 받아 그린다. */
export function requestClipRetention(http: Http, clipId: string, retentionHold: boolean) {
  return http(`/api/clips/${encodeURIComponent(clipId)}/retention`, {
    method: 'PUT',
    body: { retention_hold: retentionHold },
    schema: z.unknown(),
  });
}
