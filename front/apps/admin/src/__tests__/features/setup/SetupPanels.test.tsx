import { useCctvStore } from '@/features/cctv/cctv.store';
import { deriveStep1Coverage } from '@/features/setup/setup';
import { CameraPanel, SetupCctvPanel } from '@/features/setup/SetupPanels';
import type { CoverageGuidesResponse } from '@/features/setup/types';
import { buildCoverageGuidePayload } from '@/mocks/setup';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/features/cctv/useCctvPreview', () => ({
  useCctvPreview: () => ({ attach: vi.fn() }),
}));

afterEach(() => {
  cleanup();
  useCctvStore.getState().reset();
});

describe('세팅 CCTV 높이', () => {
  it.each([false, true])(
    '실시간 연결 %s 에서 size 컨테이너까지 높이와 flex 를 전달한다',
    (live) => {
      if (live) {
        useCctvStore.getState().hello(['cam1', 'cam2', 'cam3', 'cam4']);
        useCctvStore.getState().setLink('open');
      }
      const { container } = render(<SetupCctvPanel />);
      const sizeContainer = container.querySelector('[class*="container-type:size"]')!;
      const wall = sizeContainer.parentElement!;
      const frame = live ? wall.parentElement! : wall.parentElement!.parentElement!;
      expect(frame.className).toContain('h-[clamp(260px,40vh,520px)]');
      expect(frame.classList.contains('flex')).toBe(true);
      expect(frame.classList.contains('flex-col')).toBe(true);
      if (!live) expect(wall.parentElement!.classList.contains('flex-1')).toBe(true);
      expect(
        live ? container.querySelectorAll('canvas') : container.querySelectorAll('svg[viewBox]'),
      ).toHaveLength(4);
    },
  );
});

const latest = buildCoverageGuidePayload({ connectivity: 'CONNECTED', guides: [] });
const fresh: CoverageGuidesResponse = {
  calibration_version: null,
  guides: [],
  site_id: 'classroom-1',
  status: 'OK',
  stale: false,
  received_at_ms: 100,
  age_ms: 0,
  latest,
};

describe('카메라 각도 표시', () => {
  it.each([
    { label: '확인 중', response: undefined, error: false, expected: '—' },
    { label: '계산 요청 실패', response: fresh, error: true, expected: '—' },
    {
      label: '결과 없음',
      response: { ...fresh, status: 'EMPTY' as const, latest: null },
      error: false,
      expected: '—',
    },
    {
      label: '응답 STALE',
      response: { ...fresh, status: 'STALE' as const },
      error: false,
      expected: '—',
    },
    { label: 'stale 플래그', response: { ...fresh, stale: true }, error: false, expected: '—' },
    {
      label: '계산 불가',
      response: { ...fresh, latest: { ...latest, connectivity: 'UNKNOWN' as const } },
      error: false,
      expected: '—',
    },
    ...(['STALE', 'NO_VIDEO', 'OK'] as const).map((status) => ({
      label: `카메라 ${status}`,
      response: {
        ...fresh,
        latest: buildCoverageGuidePayload({
          connectivity: 'CONNECTED',
          guides: [],
          cameraStatuses: { cam1: status },
        }),
      },
      error: false,
      expected: status === 'OK' ? '각도 양호' : '—',
    })),
    {
      label: 'OK + 회전 안내',
      response: {
        ...fresh,
        latest: buildCoverageGuidePayload({
          connectivity: 'DISCONNECTED',
          guides: [
            {
              camera_id: 'cam1',
              pair: ['cam1', 'cam2'],
              direction: 'RIGHT',
              magnitude: 'SMALL',
              reason_code: 'WEAK_PAIR_EDGE_CLUSTER',
            },
          ],
        }),
      },
      error: false,
      expected: 'cam1 카메라를 오른쪽으로 조금 돌려 주세요 (cam1–cam2)',
    },
    {
      label: 'CONNECTED + 선택 보강만',
      response: {
        ...fresh,
        latest: buildCoverageGuidePayload({
          guides: [
            {
              camera_id: 'cam1',
              pair: ['cam1', 'cam2'],
              direction: 'RIGHT',
              magnitude: 'SMALL',
              reason_code: 'OPTIONAL_IMPROVE',
            },
          ],
        }),
      },
      error: false,
      expected: '각도 양호',
    },
  ])('$label', ({ response, error, expected }) => {
    render(<CameraPanel coverage={deriveStep1Coverage(response, error)} />);
    const row = screen.getByText('cam1', { selector: 'b' }).closest('li')!;
    expect(within(row).getByText(expected)).toBeTruthy();
    if (expected !== '각도 양호') expect(within(row).queryByText('각도 양호')).toBeNull();
  });
});
