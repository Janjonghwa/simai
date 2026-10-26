import type { Http } from '@/lib/http';
import { validateMessage } from '@simai/contracts';
import { z } from 'zod';

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.

/** viewer_health 메시지(봉투 포함). /topic/health 와 같은 계약 검사를 통과한 것만 받는다. */
const viewerHealthMessage = z.unknown().transform((raw, ctx) => {
  const checked = validateMessage('viewer_health', raw);
  if (checked.ok) return checked.message;
  ctx.addIssue({ code: 'custom', message: 'viewer_health 계약과 다르다' });
  return z.NEVER;
});

/**
 * 지금 System Health(명세 12.6 GET /api/health, 열람). 응답은 /topic/health 와 같은 viewer_health 메시지라고
 * FE 가 제안했다(S15P31A101-268, BE S15P31A101-262 에서 확정 전). 형식이 정해지면 이 파일과 mock 만 바꾼다.
 */
export function requestHealth(http: Http, signal?: AbortSignal) {
  return http('/api/health', { schema: viewerHealthMessage, signal });
}
