import { TopBar } from '@/features/layout/TopBar';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import type { ViewerHealthPayload } from '@simai/contracts';
import { cleanup, render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="/">{children}</a>,
}));
vi.mock('@/features/auth/useLogout', () => ({ useLogout: () => () => undefined }));
vi.mock('@/lib/alarmSound', () => ({ playTestAlarm: vi.fn() }));

const T = 1_760_342_400_000;
const MISMATCH = { committed_version: 'scene-3', live_version: 'scene-2', mismatch: true };

function seed(change: Partial<ViewerHealthPayload> = {}, phase: 'OPEN' | 'CLOSED' = 'OPEN') {
  const now = Date.now();
  useRealtimeStore.setState({
    phase,
    openedAt: now,
    health: {
      payload: { ...viewerHealthAt(T).payload, ...change },
      receivedAt: now,
      clockOffsetMs: 0,
    },
  });
}

/** 상단 바에서 이름표로 시작하는 칸. */
function meta(container: HTMLElement, label: string) {
  const spans = [...container.querySelectorAll<HTMLElement>('header span')];
  const found = spans.find((span) => span.textContent?.startsWith(`${label} `));
  if (!found) throw new Error(`${label} 칸이 없다`);
  return found;
}

afterEach(() => {
  cleanup();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

describe('TopBar 장면 칸', () => {
  it('현장이 다른 Scene 으로 판정하면 두 버전을 줄무늬로 보이고, 자리가 모자라도 이 칸은 줄지 않는다', () => {
    seed({ scene: MISMATCH });
    const { container } = render(<TopBar floating={false} />);

    const scene = meta(container, '장면');
    expect(scene.textContent).toBe('장면 scene-3 ≠ 현장 scene-2');
    expect(scene.querySelector('b')?.className).toContain('bg-stripes');
    // 자리가 모자라면 보정·구성만 말줄임으로 줄고, 짧고 급한 데이터·카메라·장면은 줄지 않는다.
    for (const label of ['데이터', '카메라', '장면']) {
      expect(meta(container, label).className).toContain('shrink-0');
    }
    for (const label of ['보정', '구성']) {
      expect(meta(container, label).className).toContain('truncate');
      expect(meta(container, label).className).not.toContain('shrink-0');
    }
  });

  it('같은 Scene 이면 경고하지 않는다', () => {
    seed();
    const { container } = render(<TopBar floating={false} />);

    const scene = meta(container, '장면');
    expect(scene.textContent).toBe('장면 scene-3');
    expect(scene.querySelector('.bg-stripes')).toBeNull();
  });

  it('EC2 와 끊기면 마지막으로 받은 불일치로 경고하지 않고 비운다', () => {
    seed({ scene: MISMATCH }, 'CLOSED');
    const { container } = render(<TopBar floating={false} />);

    const scene = meta(container, '장면');
    expect(scene.textContent).toBe('장면 —');
    expect(scene.querySelector('.bg-stripes')).toBeNull();
  });
});
