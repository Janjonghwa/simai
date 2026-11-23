import { EventLog } from '@/features/history/WorkLists';
import type { EventRow } from '@/features/history/replayIndex';
import { MOCK_ZONES } from '@/mocks/zones';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// 구역 목록 요청만 응답한다(S15P31A101-850)
vi.mock('@/lib/api', () => ({ api: () => Promise.resolve(MOCK_ZONES) }));

afterEach(cleanup);

describe('지난 작업 이벤트 로그의 구역 이름(S15P31A101-850)', () => {
  it('구역 캐시가 비어 있어도(달력에서 바로 들어옴) 구역을 받아 「작업자 · 구역」으로 보인다', async () => {
    const event: EventRow = {
      eventId: 'e-1',
      ruleId: 'R5',
      severity: 'CAUTION',
      worldIds: ['w-mwhubgp0-0010'],
      zoneId: 'z-04',
      raisedAt: 1_760_342_400_000,
      resolvedAt: null,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <EventLog events={[event]} playhead={null} onPick={vi.fn()} />
      </QueryClientProvider>,
    );
    const list = screen.getByRole('list', { name: '이벤트 로그' });
    await waitFor(() => expect(list.textContent).toContain('개구부 구역'));
  });
});
