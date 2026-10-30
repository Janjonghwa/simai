import { rulesSchema } from '@/features/rules/types';
import type { Http } from '@/lib/http';

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.

/** 현장이 지금 판정에 쓰는 규칙(로그인한 모든 역할). */
export function requestRules(http: Http, signal?: AbortSignal) {
  return http('/api/rules', { schema: rulesSchema, signal });
}
