import { type LoginForm, loginResponseSchema } from '@/features/auth/types';
import type { Http } from '@/lib/http';

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.
export function requestLogin(http: Http, input: LoginForm) {
  return http('/api/auth/login', { method: 'POST', body: input, schema: loginResponseSchema });
}
