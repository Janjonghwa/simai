import { useAckEvent } from '@/features/events/useAckEvent';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

// 앱의 api 인스턴스는 import.meta.env 를 읽는다(CONVENTION 테스트). 같은 createHttp 로 시험 주소를 쓴다.
vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const BASE = 'http://api.test';
const RESOLVED = '이미 해소된 이벤트입니다';

const server = setupServer();
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

function setup() {
  const queryClient = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { wrapper };
}

/** 풀어 줄 때까지 응답을 미루고 409 를 돌려준다. */
function holdAck() {
  let release = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  server.use(
    http.post(`${BASE}/api/events/:id/ack`, async () => {
      await gate;
      return HttpResponse.json({ message: RESOLVED }, { status: 409 });
    }),
  );
  return () => release();
}

describe('useAckEvent', () => {
  it('같은 이벤트의 버튼들은 보내는 중 상태를 같이 본다', async () => {
    const release = holdAck();
    const { wrapper } = setup();
    const { result } = renderHook(
      () => ({ banner: useAckEvent('e-1'), card: useAckEvent('e-1'), other: useAckEvent('e-2') }),
      { wrapper },
    );

    act(() => result.current.banner.ack(3));
    await waitFor(() => expect(result.current.card.pending).toBe(true));
    expect(result.current.banner.pending).toBe(true);
    expect(result.current.other.pending).toBe(false);

    release();
    await waitFor(() => expect(result.current.card.pending).toBe(false));
  });

  it('실패 문구는 보낸 이벤트에만 보인다(배너가 다음 경보로 바뀌면 지운다)', async () => {
    const release = holdAck();
    release();
    const { wrapper } = setup();
    const { result, rerender } = renderHook(({ id }) => useAckEvent(id), {
      wrapper,
      initialProps: { id: 'e-1' },
    });

    act(() => result.current.ack(3));
    await waitFor(() => expect(result.current.error).toBe(RESOLVED));
    rerender({ id: 'e-2' });
    expect(result.current.error).toBeUndefined();
  });
});
