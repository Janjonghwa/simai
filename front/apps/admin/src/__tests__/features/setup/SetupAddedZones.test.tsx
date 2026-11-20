import { SetupAddedZones } from '@/features/setup/SetupAddedZones';
import type { SetupZonePlanView } from '@/features/setup/setupZonePlan';
import { readZones } from '@/features/zones/zones';
import { createSetupMock } from '@/mocks/setup';
import { createZonesMock, MOCK_ZONES } from '@/mocks/zones';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});

const server = setupServer();
let setupMock: ReturnType<typeof createSetupMock>;

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  setupMock = createSetupMock({ now: () => 1_800_000_000_000 });
  const zonesMock = createZonesMock();
  setupMock.bindLiveZones(() => zonesMock.currentZones());
  server.resetHandlers(...zonesMock.handlers, ...setupMock.handlers);
});
afterEach(() => cleanup());

const liveZones = readZones(MOCK_ZONES);

describe('SetupAddedZones', () => {
  it('AI 초안은 빼고 사용자 추가 구역과 이전 맵 구역만 보이며, 이전 맵 표시와 hover·빼기를 처리한다', async () => {
    const [z04, z02] = liveZones as [(typeof liveZones)[0], (typeof liveZones)[1]];
    const manualZone = { ...z02, zoneId: 'setup-new-1', name: '사용자 추가 구역 1' };
    const aiZone = { ...z04, zoneId: 'setup-new-2', name: '단상 개구부' };
    const plan: SetupZonePlanView = {
      setup_session_id: 's-1',
      revision: 1,
      items: [
        {
          zone_id: z04.zoneId,
          source: 'MANUAL',
          candidate_id: null,
          zone: null,
          surface: null,
          needs_review: false,
        },
        {
          zone_id: null,
          source: 'MANUAL',
          candidate_id: null,
          zone: null,
          surface: null,
          needs_review: false,
        },
        {
          zone_id: null,
          source: 'AI',
          candidate_id: 'demo-opening-stage',
          zone: null,
          surface: null,
          needs_review: false,
        },
      ],
    };
    const onHover = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <SetupAddedZones
          sessionId="s-1"
          plan={plan}
          zones={[z04, manualZone, aiZone]}
          onHover={onHover}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByRole('heading', { name: '사용자 추가 구역' })).toBeTruthy();
    expect(screen.getByText(z04.name)).toBeTruthy();
    expect(screen.getByText('이전 맵')).toBeTruthy();
    expect(screen.getByText('사용자 추가 구역 1')).toBeTruthy();
    expect(screen.queryByText('단상 개구부')).toBeNull();

    const manualRow = screen.getByText('사용자 추가 구역 1').closest('li')!;
    fireEvent.mouseEnter(manualRow);
    expect(onHover).toHaveBeenCalledWith(manualZone);
    fireEvent.mouseLeave(manualRow);
    expect(onHover).toHaveBeenLastCalledWith(null);
  });

  it('AI 초안만 있으면 아무것도 그리지 않는다', async () => {
    const [z04] = liveZones as [(typeof liveZones)[0]];
    const aiZone = { ...z04, zoneId: 'setup-new-0', name: '단상 개구부' };
    const plan: SetupZonePlanView = {
      setup_session_id: 's-1',
      revision: 1,
      items: [
        {
          zone_id: null,
          source: 'AI',
          candidate_id: 'demo-opening-stage',
          zone: null,
          surface: null,
          needs_review: false,
        },
      ],
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(
      <QueryClientProvider client={client}>
        <SetupAddedZones sessionId="s-1" plan={plan} zones={[aiZone]} onHover={vi.fn()} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(container.firstChild).toBeNull());
  });
});
