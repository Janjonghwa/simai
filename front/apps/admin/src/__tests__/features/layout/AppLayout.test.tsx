import { AppLayout } from '@/features/layout/AppLayout';
import { cleanup, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 레이아웃이 알림 자리를 어디에 어떻게 두는지만 본다. 화면 조각은 대역으로 바꾼다.
const route = vi.hoisted(() => ({
  staticData: {} as { fullBleed?: boolean; eventPanel?: boolean },
  failure: true,
  alarm: true,
}));

vi.mock('@tanstack/react-router', () => ({
  useMatches: ({ select }: { select: (matches: unknown[]) => unknown }) =>
    select([{ staticData: route.staticData }]),
  Outlet: () => <div data-testid="page" />,
}));
vi.mock('@/features/layout/TopBar', () => ({ TopBar: () => <header /> }));
vi.mock('@/features/layout/Dock', () => ({ Dock: () => null }));
vi.mock('@/features/layout/SpatialViewportHost', () => ({
  SpatialViewportHost: ({ children }: { children?: ReactNode }) => children,
}));
vi.mock('@/features/events/AlarmSoundLoop', () => ({ AlarmSoundLoop: () => null }));
vi.mock('@/features/layout/FailureBand', () => ({
  FailureBand: () => (route.failure ? <div data-testid="failure" /> : null),
}));
vi.mock('@/features/events/AlarmBanner', () => ({
  AlarmBanner: ({ withPanel }: { withPanel: boolean }) =>
    route.alarm ? <div data-testid="alarm" data-with-panel={String(withPanel)} /> : null,
}));

const notices = () => screen.getByRole('region', { name: '알림' });
const ids = (el: HTMLElement) => [...el.children].map((child) => child.getAttribute('data-testid'));

beforeEach(() => {
  route.staticData = {};
  route.failure = true;
  route.alarm = true;
});
afterEach(cleanup);

describe('AppLayout 알림 자리', () => {
  it('실시간 관제(화면을 채움)에서는 장면 위에 띄우고, 장애 띠 아래 경보 배너를 두며 이벤트 패널 자리를 비킨다', () => {
    route.staticData = { fullBleed: true, eventPanel: true };
    render(<AppLayout />);

    const box = notices();
    expect(ids(box)).toEqual(['failure', 'alarm']);
    expect(box.className).toContain('absolute');
    expect(box.className).toContain('right-[370px]');
    // 띄운 자리는 클릭을 통과시키고, 띠와 배너만 받는다(각자 pointer-events-auto).
    expect(box.className).toContain('pointer-events-none');
    expect(screen.getByTestId('alarm').dataset.withPanel).toBe('true');
  });

  it('다른 화면에서는 본문을 가리지 않게 본문 위에 자리를 차지한다', () => {
    route.staticData = {};
    render(<AppLayout />);

    const box = notices();
    expect(ids(box)).toEqual(['failure', 'alarm']);
    expect(box.className).not.toContain('absolute');
    expect(box.className).not.toContain('right-[370px]');
    // 본문(main)보다 앞에 온다.
    const page = screen.getByRole('main');
    expect(box.compareDocumentPosition(page) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('알릴 것이 없으면 자리가 비어 숨는다(empty:hidden)', () => {
    route.failure = false;
    route.alarm = false;
    render(<AppLayout />);

    const box = notices();
    expect(box.childElementCount).toBe(0);
    expect(box.className).toContain('empty:hidden');
  });
});
