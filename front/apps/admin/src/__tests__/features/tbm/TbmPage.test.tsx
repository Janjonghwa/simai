import { TbmPage } from '@/features/tbm/TbmPage';
import { TBM_FORM_KEY, useTbmFormStore } from '@/features/tbm/tbm.store';
import { createTbmMock } from '@/mocks/tbm';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

let tbmMock = createTbmMock();
const server = setupServer(...tbmMock.handlers);

// jsdom 에는 innerText 가 없다. 서식 칸(Fx)이 글자를 넣고 읽는 길을 textContent 로 잇는다.
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'innerText', {
    configurable: true,
    get() {
      return this.textContent;
    },
    set(value: string) {
      this.textContent = value;
    },
  });
  server.listen();
});

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  localStorage.removeItem(TBM_FORM_KEY);
  useTbmFormStore.setState({ edits: {} });
  tbmMock = createTbmMock();
  server.resetHandlers(...tbmMock.handlers);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

afterAll(() => {
  server.close();
});

/** 작업을 고른 뒤 회의록을 받는 중(700ms 흉내)이 끝나게 한다. */
const received = () =>
  act(() => {
    vi.advanceTimersByTime(800);
  });

describe('왼쪽 패널', () => {
  it('처음에는 레이아웃을 크게 보이고 오른쪽 위 단추 하나로 평가 · 간섭 보기와 바꾼다', () => {
    render(<TbmPage />);
    const panel = screen.getByRole('region', { name: 'T1 가상 레이아웃' });
    expect(within(panel).getByRole('img', { name: /평면 도식/ })).toBeTruthy();
    expect(within(panel).queryByRole('table')).toBeNull();
    // 한 화면에 단추는 하나뿐이고 글자가 보기 전환을 알린다.
    const toggle = within(panel).getByRole('button', { name: '평가 · 간섭 보기' });
    expect(within(panel).getAllByRole('button')).toHaveLength(1);

    fireEvent.click(toggle);
    const detail = screen.getByRole('region', { name: 'T1 평가 · 간섭' });
    expect(within(detail).getByRole('table')).toBeTruthy();
    expect(within(detail).queryByRole('img', { name: /평면 도식/ })).toBeNull();
    expect(within(detail).getByText('T1 ↔ T2')).toBeTruthy();
    expect(within(detail).getAllByRole('button')).toHaveLength(1);

    fireEvent.click(within(detail).getByRole('button', { name: '레이아웃 보기' }));
    expect(screen.getByRole('region', { name: 'T1 가상 레이아웃' })).toBeTruthy();
  });

  it('작업을 바꿔도 고른 보기를 유지한다', () => {
    render(<TbmPage />);
    fireEvent.click(screen.getByRole('button', { name: '평가 · 간섭 보기' }));
    fireEvent.click(screen.getByRole('button', { name: /^T2 자재 반입/ }));
    expect(screen.getByRole('region', { name: 'T2 평가 · 간섭' })).toBeTruthy();
  });
});

describe('작업 등록 · 수정 모달', () => {
  it('+ 작업 등록은 모달 창을 열고, 공종·가상 레이아웃을 골라 등록하면 새 작업을 고른다', async () => {
    render(<TbmPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 재생성' })).toBeTruthy();
    });

    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '+ 작업 등록' }));
    const dialog = screen.getByRole('dialog', { name: '작업 등록' });
    expect(dialog.tagName).toBe('DIALOG');

    const name = within(dialog).getByLabelText('작업명');
    const workTypeSelect = within(dialog).getByLabelText('공종');
    const layoutSelect = within(dialog).getByLabelText('가상 레이아웃');
    expect(within(dialog).getByRole('button', { name: '등록' }).hasAttribute('disabled')).toBe(
      true,
    );

    fireEvent.change(name, { target: { value: '전기 배선' } });
    fireEvent.change(workTypeSelect, { target: { value: 'OPENING_WORK' } });
    fireEvent.change(layoutSelect, { target: { value: '102' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '등록' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText(/T4 등록됨/)).toBeTruthy();
    expect(screen.getByRole('region', { name: 'T4 TBM 회의록' })).toBeTruthy();

    await waitFor(() => {
      const createdInMock = tbmMock.works.find((w) => w.name === '전기 배선');
      expect(createdInMock).toMatchObject({
        work_type: 'OPENING_WORK',
        layout_id: 102,
      });
    });
  });

  it('작업 수정은 선택한 작업의 공종·가상 레이아웃을 PUT /api/work-units/{id} 로 갱신한다', async () => {
    render(<TbmPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 재생성' })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: '작업 수정' }));
    const dialog = screen.getByRole('dialog', { name: '작업 수정' });
    const workTypeSelect = within(dialog).getByLabelText('공종');
    const layoutSelect = within(dialog).getByLabelText('가상 레이아웃');

    fireEvent.change(workTypeSelect, { target: { value: 'HIGH_PLACE' } });
    fireEvent.change(layoutSelect, { target: { value: '103' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '저장' }));

    await waitFor(() => {
      expect(screen.getByText(/T1 수정됨/)).toBeTruthy();
      expect(tbmMock.works[0]).toMatchObject({
        work_type: 'HIGH_PLACE',
        layout_id: 103,
      });
    });
  });

  it('Esc(cancel)와 바깥(backdrop) 누르기, 취소 단추로 닫는다', () => {
    render(<TbmPage />);
    const open = () => fireEvent.click(screen.getByRole('button', { name: '+ 작업 등록' }));

    open();
    const byEsc = screen.getByRole('dialog', { name: '작업 등록' });
    const cancel = new Event('cancel', { bubbles: false, cancelable: true });
    act(() => {
      byEsc.dispatchEvent(cancel);
    });
    expect(cancel.defaultPrevented).toBe(true);
    expect(screen.queryByRole('dialog')).toBeNull();

    open();
    // 창 안쪽을 누르면 닫히지 않고, 창 자체(바깥 backdrop)를 누르면 닫힌다.
    const dialog = screen.getByRole('dialog', { name: '작업 등록' });
    fireEvent.click(within(dialog).getByRole('heading', { name: /작업 등록/ }));
    expect(screen.queryByRole('dialog')).toBeTruthy();
    fireEvent.click(dialog);
    expect(screen.queryByRole('dialog')).toBeNull();

    open();
    fireEvent.click(screen.getByRole('button', { name: '취소' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('TBM 보고서 수명주기 · 법령 지식 그래프 · 오류 및 폴백', () => {
  it('대책·법령 근거(sections·citations), 법령 지식 그래프 SVG(노드 클릭 상세), 생성 단계(steps)를 표시한다', async () => {
    render(<TbmPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 재생성' })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: '평가 · 간섭 보기' }));
    const detail = screen.getByRole('region', { name: 'T1 평가 · 간섭' });

    // sections(hazards, measures, focus, legal) 및 citations 확인
    expect(within(detail).getByTestId('report-section-hazards')).toBeTruthy();
    expect(within(detail).getByTestId('report-section-measures')).toBeTruthy();
    expect(within(detail).getByTestId('report-section-focus')).toBeTruthy();
    expect(within(detail).getByTestId('report-section-legal')).toBeTruthy();
    expect(within(detail).getByTestId('report-citation-OSH-STD-A43')).toBeTruthy();

    // 법령 지식 그래프 SVG: cited 강조(data-cited="true"), hop 구분(0 vs 1), 노드 클릭 시 상세 발췌문 전환
    const citedNode = within(detail).getByTestId('graph-node-OSH-STD-A43');
    const hop1Node = within(detail).getByTestId('graph-node-OSH-ACT-A38');
    expect(citedNode.getAttribute('data-cited')).toBe('true');
    expect(citedNode.getAttribute('data-hop')).toBe('0');
    expect(hop1Node.getAttribute('data-cited')).toBe('false');
    expect(hop1Node.getAttribute('data-hop')).toBe('1');

    const nodeDetail = within(detail).getByTestId('graph-node-detail');
    expect(nodeDetail.textContent).toContain('개구부 등의 방호조치');
    expect(nodeDetail.textContent).toContain('안전난간, 울타리, 수직형 추락방망');

    fireEvent.click(within(detail).getByTestId('graph-node-OSH-STD-A56'));
    expect(within(detail).getByTestId('graph-node-detail').textContent).toContain(
      '작업발판의 구조',
    );
    expect(within(detail).getByTestId('graph-node-detail').textContent).toContain(
      '폭이 40센티미터',
    );

    // steps(4단계) 표시 확인
    expect(within(detail).getByTestId('report-step-CANDIDATE_SEARCH').textContent).toContain(
      '42ms',
    );
    expect(within(detail).getByTestId('report-step-GRAPH_EXPAND').textContent).toContain('18ms');
    expect(within(detail).getByTestId('report-step-LLM_GENERATE').textContent).toContain('1420ms');
    expect(within(detail).getByTestId('report-step-CITATION_VALIDATE').textContent).toContain(
      '7ms',
    );
  });

  it('보고서 생성 요청(POST 202) → 2초 간격 폴링(GET /api/reports/tbm/{id}) → 완료(SUCCEEDED) 흐름이 동작한다', async () => {
    render(<TbmPage />);
    const regenBtn = await screen.findByRole('button', { name: '보고서 재생성' });

    fireEvent.click(regenBtn);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 생성 중…' })).toBeTruthy();
    });

    // 2초 간격 폴링 실행
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 재생성' })).toBeTruthy();
      const versionSelect = screen.getByLabelText('보고서 버전') as HTMLSelectElement;
      expect(versionSelect.textContent).toContain('v2 (SUCCEEDED)');
    });
  });

  it('FAILED 상태 보고서는 failure_reason 안내 문구를 표시하고 지어낸 문장으로 채우지 않는다', async () => {
    tbmMock.setNextReportOutcome({
      status: 'FAILED',
      failureReason: 'CITATION_VIOLATION',
    });

    render(<TbmPage />);
    const regenBtn = await screen.findByRole('button', { name: '보고서 재생성' });
    fireEvent.click(regenBtn);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 생성 중…' })).toBeTruthy();
    });

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    await waitFor(() => {
      const alert = screen.getByTestId('tbm-report-failed');
      expect(alert.textContent).toContain('CITATION_VIOLATION');
    });

    // 평가·간섭 패널에서도 지어낸 sections 본문 없이 실패 사유만 보인다
    fireEvent.click(screen.getByRole('button', { name: '평가 · 간섭 보기' }));
    const detail = screen.getByRole('region', { name: 'T1 평가 · 간섭' });
    expect(within(detail).queryByTestId('report-section-measures')).toBeNull();
    expect(within(detail).getByRole('alert').textContent).toContain('CITATION_VIOLATION');
  });

  it('409 응답(WORK_HAS_NO_LAYOUT · REPORT_IN_PROGRESS)을 구분해 안내한다', async () => {
    // 1) WORK_HAS_NO_LAYOUT: 레이아웃 미연결 작업 등록 후 보고서 생성 요청
    tbmMock = createTbmMock({ pollsBeforeComplete: 2 });
    server.resetHandlers(...tbmMock.handlers);

    render(<TbmPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 재생성' })).toBeTruthy();
    });

    // T1 의 가상 레이아웃을 '연결 안 함'으로 수정한 뒤 보고서 생성 시도
    fireEvent.click(screen.getByRole('button', { name: '작업 수정' }));
    const editDialog = screen.getByRole('dialog', { name: '작업 수정' });
    fireEvent.change(within(editDialog).getByLabelText('가상 레이아웃'), {
      target: { value: '' },
    });
    fireEvent.click(within(editDialog).getByRole('button', { name: '저장' }));

    await waitFor(() => {
      expect(tbmMock.works[0]?.layout_id).toBeNull();
    });

    fireEvent.click(screen.getByRole('button', { name: '보고서 재생성' }));
    await waitFor(() => {
      expect(screen.getByTestId('tbm-request-error').textContent).toContain('WORK_HAS_NO_LAYOUT');
    });

    // 2) REPORT_IN_PROGRESS: T2(레이아웃 연결됨) 선택 후 생성 시작(GENERATING) 중 중복 생성 요청
    fireEvent.click(screen.getByRole('button', { name: /^T2 자재 반입/ }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 재생성' })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: '보고서 재생성' }));
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '보고서 생성 중…' })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole('button', { name: '보고서 생성 중…' }));
    await waitFor(() => {
      expect(screen.getByTestId('tbm-request-error').textContent).toContain('REPORT_IN_PROGRESS');
    });
  });

  it('API 요청이 5xx 또는 네트워크 오류로 실패하면 tbmDemo.ts 로 대체하고 "데모 데이터"를 표시한다', async () => {
    server.use(
      http.get('*/api/work-units', () =>
        HttpResponse.json({ message: 'Internal Server Error' }, { status: 500 }),
      ),
    );

    render(<TbmPage />);
    await waitFor(() => {
      expect(screen.getByTestId('tbm-demo-badge').textContent).toBe('데모 데이터');
    });
    expect(screen.getByRole('region', { name: 'T1 가상 레이아웃' })).toBeTruthy();
  });
});

describe('회의록', () => {
  it('A4 쪽 미리보기를 보이고, 받는 중에는 편집할 수 없다', () => {
    render(<TbmPage />);
    expect(screen.getByRole('button', { name: '편집' }).hasAttribute('disabled')).toBe(true);
    received();
    expect(screen.getByRole('button', { name: '편집' }).hasAttribute('disabled')).toBe(false);
    const preview = screen.getByRole('group', { name: 'TBM 회의록 미리보기' });
    expect(preview.textContent).toContain('Tool Box Meeting 회의록');
    expect(preview.textContent).toContain('개구부 옆 비계 설치');
  });

  it('편집은 모달 창에서 하고 고친 칸은 저장소에 남는다(자동 저장)', () => {
    render(<TbmPage />);
    received();
    fireEvent.click(screen.getByRole('button', { name: '편집' }));
    const dialog = screen.getByRole('dialog', { name: 'TBM 회의록 편집' });
    const boxes = within(dialog).getAllByRole('textbox');
    expect(boxes.length).toBeGreaterThan(10);

    const first = boxes[0]!;
    first.textContent = '새 일시';
    fireEvent.input(first);
    expect(useTbmFormStore.getState().edits.T1).toBeTruthy();
    expect(localStorage.getItem(TBM_FORM_KEY)).toContain('새 일시');

    // 편집 완료로 닫으면 미리보기에 고친 값이 보인다.
    fireEvent.click(within(dialog).getByRole('button', { name: '편집 완료' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('group', { name: 'TBM 회의록 미리보기' }).textContent).toContain(
      '새 일시',
    );
  });

  it('편집 창의 되돌리기는 고친 칸을 버린다', () => {
    render(<TbmPage />);
    received();
    fireEvent.click(screen.getByRole('button', { name: '편집' }));
    const dialog = screen.getByRole('dialog', { name: 'TBM 회의록 편집' });
    const undo = within(dialog).getByRole('button', { name: '되돌리기' });
    expect(undo.hasAttribute('disabled')).toBe(true);
    const box = within(dialog).getAllByRole('textbox')[0]!;
    box.textContent = '고침';
    fireEvent.input(box);
    expect(undo.hasAttribute('disabled')).toBe(false);
    fireEvent.click(undo);
    expect(useTbmFormStore.getState().edits.T1).toBeUndefined();
  });

  it('출력은 인쇄 창을 여는 동안만 출력용 사본을 그린다', () => {
    const print = vi.fn(() => {
      // 인쇄 창이 열린 시점에는 출력용 사본(@page A4)이 문서에 있다.
      expect(document.body.textContent).toContain('@page');
    });
    vi.stubGlobal('print', print);
    render(<TbmPage />);
    received();
    fireEvent.click(screen.getByRole('button', { name: '출력' }));
    expect(print).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).not.toContain('@page');
    vi.unstubAllGlobals();
  });
});
