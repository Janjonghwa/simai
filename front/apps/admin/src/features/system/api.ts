import { metricsResponseSchema } from '@/features/system/types';
import type { Http } from '@/lib/http';

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.
export function requestMetrics(http: Http, signal?: AbortSignal) {
  return http('/api/metrics', { schema: metricsResponseSchema, signal });
}
