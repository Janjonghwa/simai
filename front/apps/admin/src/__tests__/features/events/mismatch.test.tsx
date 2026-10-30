import {
  applyMismatch,
  bindMismatchCache,
  MISMATCH_KEY,
  useMismatches,
  type Mismatches,
} from '@/features/events/mismatch';
import type { RealtimeListeners } from '@/lib/stomp';
import { actionMismatchesBetween } from '@/mocks/realtimeScenario';
import type { ActionMismatchPayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it } from 'vitest';

const [base] = actionMismatchesBetween(0, 40_000).map((m) => m.payload);
const EVENT = base!.event_id;

/** 계약 모양의 불일치 하나. 바꿀 칸만 준다. */
const payload = (change: Partial<ActionMismatchPayload> = {}): ActionMismatchPayload => ({
  ...base!,
  event_sequence: 3,
  event_lifecycle: 'UPDATED',
  detected_at: 1_000,
  cleared: false,
  ...change,
});

const read = (client: QueryClient) => client.getQueryData<Mismatches>(MISMATCH_KEY);

afterEach(cleanup);

describe('applyMismatch', () => {
  it('불일치를 이벤트 id 로 담고, 같은 이벤트의 더 새 불일치로 바꾼다', () => {
    const client = new QueryClient();
    applyMismatch(client, payload());
    expect(read(client)).toEqual({
      [EVENT]: {
        ticketId: base!.ticket_id,
        ticketStatus: 'DONE',
        eventSequence: 3,
        detectedAt: 1_000,
      },
    });

    applyMismatch(client, payload({ event_sequence: 4, detected_at: 2_000, ticket_id: 'tkt-2' }));
    expect(read(client)).toEqual({
      [EVENT]: { ticketId: 'tkt-2', ticketStatus: 'DONE', eventSequence: 4, detectedAt: 2_000 },
    });
  });

  it('이벤트가 다르면 따로 담는다', () => {
    const client = new QueryClient();
    applyMismatch(client, payload());
    applyMismatch(client, payload({ event_id: '0192a6f0-7c1e-7b3a-9f2d-000000000099' }));
    expect(Object.keys(read(client)!)).toHaveLength(2);
  });

  it('cleared 면 그 이벤트의 항목만 지운다', () => {
    const client = new QueryClient();
    const other = '0192a6f0-7c1e-7b3a-9f2d-000000000099';
    applyMismatch(client, payload());
    applyMismatch(client, payload({ event_id: other }));
    applyMismatch(client, payload({ event_sequence: 4, detected_at: 2_000, cleared: true }));
    expect(Object.keys(read(client)!)).toEqual([other]);
  });

  it('항목이 없는 이벤트의 cleared 는 캐시를 만들지도 바꾸지도 않는다', () => {
    const client = new QueryClient();
    applyMismatch(client, payload({ cleared: true }));
    expect(read(client)).toBeUndefined();

    applyMismatch(client, payload({ event_id: '0192a6f0-7c1e-7b3a-9f2d-000000000099' }));
    const before = read(client);
    applyMismatch(client, payload({ cleared: true }));
    expect(read(client)).toBe(before);
  });

  it('이미 있는 것보다 오래된 메시지(순번이 작거나 같은 순번에서 더 일찍 알아낸 것)는 무시한다', () => {
    const client = new QueryClient();
    applyMismatch(client, payload({ event_sequence: 5, detected_at: 5_000 }));
    const stored = read(client);

    applyMismatch(client, payload({ event_sequence: 4, detected_at: 9_000, ticket_id: 'older' }));
    applyMismatch(client, payload({ event_sequence: 5, detected_at: 4_000, ticket_id: 'earlier' }));
    expect(read(client)).toBe(stored);

    // 오래된 해소도 더 새 불일치를 지우지 않는다.
    applyMismatch(client, payload({ event_sequence: 4, detected_at: 9_000, cleared: true }));
    applyMismatch(client, payload({ event_sequence: 5, detected_at: 4_000, cleared: true }));
    expect(read(client)).toBe(stored);

    // 같은 순번·같은 시각(다시 보낸 것)은 받아들이되 값은 그대로다.
    applyMismatch(client, payload({ event_sequence: 5, detected_at: 5_000 }));
    expect(read(client)).toEqual(stored);
  });

  it('이벤트가 이미 해소된 상태의 불일치(cleared 아님)도 담는다. 숨길지는 보는 쪽이 정한다', () => {
    const client = new QueryClient();
    applyMismatch(client, payload({ event_lifecycle: 'RESOLVED' }));
    expect(read(client)?.[EVENT]?.eventSequence).toBe(3);
  });
});

describe('bindMismatchCache', () => {
  function fakeRealtime() {
    const listeners = new Set<RealtimeListeners['actionMismatch']>();
    return {
      listeners,
      on<K extends keyof RealtimeListeners>(type: K, listener: RealtimeListeners[K]) {
        if (type !== 'actionMismatch') throw new Error(`예상하지 않은 알림: ${type}`);
        const mismatch = listener as RealtimeListeners['actionMismatch'];
        listeners.add(mismatch);
        return () => {
          listeners.delete(mismatch);
        };
      },
      emit: (message: ActionMismatchPayload) => listeners.forEach((l) => l(message, 0)),
    };
  }

  it('실시간 연결의 조치 불일치를 캐시에 반영하고, 해제하면 더 받지 않는다', () => {
    const client = new QueryClient();
    const realtime = fakeRealtime();
    const unbind = bindMismatchCache(realtime, client);

    realtime.emit(payload());
    expect(Object.keys(read(client)!)).toEqual([EVENT]);
    realtime.emit(payload({ event_sequence: 4, detected_at: 2_000, cleared: true }));
    expect(read(client)).toEqual({});

    unbind();
    expect(realtime.listeners.size).toBe(0);
    realtime.emit(payload({ event_sequence: 9, detected_at: 9_000 }));
    expect(read(client)).toEqual({});
  });

  it('다른 화면에 머무는 동안 캐시가 지워지지 않게 한다(조회 API 가 없어 지워지면 되찾을 길이 없다)', () => {
    const client = new QueryClient();
    bindMismatchCache(fakeRealtime(), client);
    expect(client.getQueryDefaults(MISMATCH_KEY)).toMatchObject({ gcTime: Infinity });
    applyMismatch(client, payload());
    expect(client.getQueryCache().find({ queryKey: MISMATCH_KEY })?.gcTime).toBe(Infinity);
  });

  it('로그아웃으로 queryClient.clear() 하면 이전 사용자의 불일치가 남지 않는다', () => {
    const client = new QueryClient();
    bindMismatchCache(fakeRealtime(), client);
    applyMismatch(client, payload());
    client.clear();
    expect(read(client)).toBeUndefined();
  });
});

/** 캐시 변경을 구독자에게 알리는 타이머(TanStack Query 의 notifyManager)가 돌 때까지 기다린다. */
const flush = (change?: () => void) =>
  act(async () => {
    change?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

describe('useMismatches', () => {
  function run(client = new QueryClient()) {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    let renders = 0;
    const hook = renderHook(
      () => {
        renders += 1;
        return useMismatches();
      },
      { wrapper },
    );
    return { client, hook, renders: () => renders };
  }

  it('불일치가 없으면 늘 같은 빈 객체다', async () => {
    const { hook } = run();
    const first = hook.result.current;
    expect(first).toEqual({});
    await flush();
    expect(hook.result.current).toBe(first);
    hook.rerender();
    expect(hook.result.current).toBe(first);
    // 다른 화면이 부른 훅과도 같은 객체다.
    expect(run().hook.result.current).toBe(first);
  });

  it('불일치가 생기고 풀릴 때만 다시 그린다', async () => {
    const { client, hook, renders } = run();
    await flush();
    const settled = renders();

    await flush(() => applyMismatch(client, payload()));
    expect(hook.result.current[EVENT]).toMatchObject({ eventSequence: 3, ticketStatus: 'DONE' });
    expect(renders()).toBe(settled + 1);

    // 같은 불일치를 다시 받거나 오래된 메시지가 와도 다시 그리지 않는다.
    await flush(() => applyMismatch(client, payload()));
    await flush(() => applyMismatch(client, payload({ event_sequence: 1, detected_at: 1 })));
    expect(renders()).toBe(settled + 1);

    await flush(() =>
      applyMismatch(client, payload({ event_sequence: 4, detected_at: 2_000, cleared: true })),
    );
    expect(hook.result.current).toEqual({});
    expect(renders()).toBe(settled + 2);
  });

  it('화면을 늦게 띄워도 먼저 받은 불일치를 본다. 다시 받기(invalidate)가 값을 지우지 않는다', async () => {
    const client = new QueryClient();
    applyMismatch(client, payload());
    const { hook } = run(client);
    expect(hook.result.current[EVENT]).toBeDefined();

    await act(async () => {
      await client.invalidateQueries({ queryKey: ['events'] });
    });
    await flush();
    expect(hook.result.current[EVENT]).toBeDefined();
  });
});
