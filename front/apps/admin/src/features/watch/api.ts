import type { Http } from '@/lib/http';
import { z } from 'zod';

// 워치 배정 조회 키. 패널·이름표(useWatchBindings)와 잇기·풀기(useWatchBindingMutations)가 함께 쓴다.
export const WATCH_BINDINGS_KEY = ['watch-bindings'] as const;

/**
 * 워치 ↔ 익명 World ID 배정 하나(서버 WatchBindingController, S15P31A101-338·-340). 사람 이름은 없다(11.4-5).
 * method 는 관리자 수동(MANUAL) 또는 움직임 대조(MOTION_MATCH). channel_state 는 응답 시각의 워치 연결(BOUND)·끊김(LOST).
 */
export const watchBindingSchema = z.object({
  binding_id: z.number(),
  device_id: z.string(),
  world_id: z.string(),
  method: z.string(),
  bound_at: z.number(),
  channel_state: z.enum(['BOUND', 'LOST']),
});

export type WatchBinding = z.infer<typeof watchBindingSchema>;

const bindingsSchema = z.array(watchBindingSchema);

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.
export function requestWatchBindings(http: Http, signal?: AbortSignal) {
  return http('/api/watch-bindings', { schema: bindingsSchema, signal });
}

/** 관리자 수동 배정. 그 워치·World ID 의 옛 배정은 서버가 끝낸다. 움직임 대조는 이 배정을 건드리지 않는다. */
export function bindWatch(http: Http, deviceId: string, worldId: string) {
  return http(`/api/watch-bindings/${encodeURIComponent(deviceId)}`, {
    method: 'PUT',
    body: { world_id: worldId },
    schema: watchBindingSchema,
  });
}

/** 배정 풀기(204). 풀면 움직임 대조가 다시 맡는다. */
export function releaseWatch(http: Http, deviceId: string) {
  return http(`/api/watch-bindings/${encodeURIComponent(deviceId)}`, {
    method: 'DELETE',
    schema: z.void(),
  });
}
