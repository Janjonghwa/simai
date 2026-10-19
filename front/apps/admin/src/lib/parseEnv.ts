import { z } from 'zod';

const schema = z.object({
  VITE_API_BASE_URL: z.url({ protocol: /^https?$/, error: 'http(s) 주소여야 한다' }),
  VITE_STOMP_URL: z.url({ protocol: /^wss?$/, error: 'ws(s) 주소여야 한다' }),
});

export type Env = z.infer<typeof schema>;

export function parseEnv(raw: Record<string, unknown>): Env {
  const result = schema.safeParse(raw);
  if (!result.success) {
    const lines = result.error.issues.map((issue) => `- ${issue.path.join('.')}: ${issue.message}`);
    throw new Error(
      `환경변수가 비었거나 형식이 틀렸다. apps/admin/.env.example 을 .env.local 로 복사해 채운다.\n${lines.join('\n')}`,
    );
  }
  return result.data;
}
