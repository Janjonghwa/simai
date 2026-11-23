import type { Http } from '@/lib/http';
import { z } from 'zod';

// 워치 배정 조회 키. 아바타 이름표(useWatchBadges)가 쓴다.
export const WATCH_BINDINGS_KEY = ['watch-bindings'] as const;

/**
 * 워치 ↔ 익명 World ID 배정 하나(서버 WatchBindingController, S15P31A101-338·-340). 사람 이름은 없다(11.4-5).
 * method 는 관리자 수동(MANUAL) 또는 움직임 대조(MOTION_MATCH). channel_state 는 응답 시각의 워치 연결(BOUND)·끊김(LOST).
 * handoff_from·handoff_confirmed_at 은 자리 이어받기(-793) 표시다.
 */
const watchBindingSchema = z.object({
  binding_id: z.number(),
  device_id: z.string(),
  world_id: z.string(),
  method: z.string(),
  bound_at: z.number(),
  channel_state: z.enum(['BOUND', 'LOST']),
  // 자리 이어받기(-793)로 옮겨 온 배정이면 사라진 옛 World ID. 확정 전(임시)이면 handoff_confirmed_at 이 null 이다.
  // 이전 서버는 싣지 않으므로 선택 필드다. 3D 아바타 외형 이어받기(-851)가 쓴다.
  handoff_from: z.string().nullish(),
  handoff_confirmed_at: z.number().nullish(),
});

export type WatchBinding = z.infer<typeof watchBindingSchema>;

const bindingsSchema = z.array(watchBindingSchema);

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.
export function requestWatchBindings(http: Http, signal?: AbortSignal) {
  return http('/api/watch-bindings', { schema: bindingsSchema, signal });
}
