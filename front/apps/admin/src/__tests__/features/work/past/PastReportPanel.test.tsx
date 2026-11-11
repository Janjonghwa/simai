import {
  installReportServer,
  openDay,
  renderWithClient,
} from '@/__tests__/features/work/report/reportHarness';
import { PastReportPanel } from '@/features/work/past/PastReportPanel';
import { cleanup, fireEvent, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test', getToken: () => 'mock-token.admin' }) };
});

// 지난 날짜의 'TBM 보고서' 칸: 열에는 작은 미리보기만 두고, 누르면 창에 크게(출력 포함, 읽기 전용) 보인다.

const server = installReportServer();
const PAPER_TITLE = 'TBM(작업 전 안전대화) 보고서';
const THUMBNAIL = { name: 'TBM 보고서 크게 보기' };
const EDIT = { name: 'TBM 보고서 편집' };

afterEach(cleanup);

const papers = () => screen.getAllByText(PAPER_TITLE);

describe('PastReportPanel', () => {
  it('처음에는 작은 미리보기 종이 한 장만 보이고 창은 닫혀 있다', async () => {
    const day = await openDay();
    renderWithClient(<PastReportPanel workId={day.work_id} />);

    expect(await screen.findByRole('button', THUMBNAIL)).toBeTruthy();
    expect(papers()).toHaveLength(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('button', { name: '출력' })).toBeNull();
  });

  it('미리보기를 누르면 창에 같은 종이가 읽기 전용으로 크게 열리고 출력할 수 있다', async () => {
    const day = await openDay();
    renderWithClient(<PastReportPanel workId={day.work_id} />);
    fireEvent.click(await screen.findByRole('button', THUMBNAIL));

    const dialog = screen.getByRole('dialog', { name: 'TBM 보고서' });
    expect(within(dialog).getByText(PAPER_TITLE)).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: '출력' })).toBeTruthy();
    // 지난 날짜는 읽기 전용이라 종이를 눌러도 편집 창이 없다.
    expect(within(dialog).queryByRole('button', EDIT)).toBeNull();
    expect(papers()).toHaveLength(2);
  });

  it('Enter 로도 열고, 닫기로 닫는다', async () => {
    const day = await openDay();
    renderWithClient(<PastReportPanel workId={day.work_id} />);
    fireEvent.keyDown(await screen.findByRole('button', THUMBNAIL), { key: 'Enter' });
    const dialog = screen.getByRole('dialog', { name: 'TBM 보고서' });

    fireEvent.click(within(dialog).getByRole('button', { name: '닫기' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(papers()).toHaveLength(1);
  });

  it('보고서를 못 받으면 미리보기 위에서 알리고 다시 불러올 수 있다', async () => {
    const day = await openDay();
    let fail = true;
    server.server.use(
      http.get('*/api/reports/tbm', () =>
        fail ? HttpResponse.json({ code: 'BOOM' }, { status: 500 }) : undefined,
      ),
    );
    renderWithClient(<PastReportPanel workId={day.work_id} />);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('보고서를 불러오지 못했습니다');
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: '다시 불러오기' }));
    // 읽기 전용 종이라 '+ 작업내용 선택' 은 없고, 받은 보고서의 일시가 채워진다.
    await screen.findByText('2026-11-11 09:00');
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
