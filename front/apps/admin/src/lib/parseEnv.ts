import { z } from 'zod';

const schema = z.object({
  VITE_API_BASE_URL: z.url({ protocol: /^https?$/, error: 'http(s) 주소여야 한다' }),
  VITE_STOMP_URL: z.url({ protocol: /^wss?$/, error: 'ws(s) 주소여야 한다' }),
  // true 면 개발 서버에서 msw 가 REST 를 흉내 낸다. 빌드에는 들어가지 않는다.
  VITE_API_MOCK: z.stringbool({ error: 'true 또는 false 여야 한다' }).default(false),
});

export type Env = z.infer<typeof schema>;

// 운영 빌드를 내 PC 에서 띄워 볼 때(vite preview)만 암호화 없는 http·ws 를 허용한다.
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

function isCleartextRemote(value: string) {
  const url = new URL(value);
  return (url.protocol === 'http:' || url.protocol === 'ws:') && !LOCAL_HOSTS.has(url.hostname);
}

export function parseEnv(raw: Record<string, unknown>, { production = false } = {}): Env {
  const result = schema.safeParse(raw);
  const lines = result.success
    ? []
    : result.error.issues.map((issue) => `- ${issue.path.join('.')}: ${issue.message}`);
  if (result.success && production) {
    for (const key of ['VITE_API_BASE_URL', 'VITE_STOMP_URL'] as const) {
      if (isCleartextRemote(result.data[key])) {
        lines.push(
          `- ${key}: 운영 빌드에서는 https·wss 주소여야 한다(비밀번호·토큰이 암호화 없이 오간다)`,
        );
      }
    }
  }
  if (!result.success || lines.length > 0) {
    throw new Error(
      `환경변수가 비었거나 형식이 틀렸다. apps/admin/.env.example 을 .env.local 로 복사해 채운다.\n${lines.join('\n')}`,
    );
  }
  return result.data;
}
