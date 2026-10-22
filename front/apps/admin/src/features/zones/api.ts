import { zonesResponseSchema } from '@/features/zones/types';
import type { Http } from '@/lib/http';

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.
export function requestZones(http: Http, signal?: AbortSignal) {
  return http('/api/zones', { schema: zonesResponseSchema, signal });
}
