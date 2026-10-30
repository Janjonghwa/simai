import { TbmPage } from '@/features/tbm/TbmPage';
import { TBM_FORM_KEY, useTbmFormStore } from '@/features/tbm/tbm.store';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

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
});

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.removeItem(TBM_FORM_KEY);
  useTbmFormStore.setState({ edits: {} });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
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

describe('작업 등록 모달', () => {
  it('+ 작업 등록은 모달 창을 열고, 등록하면 새 작업을 고른다', () => {
    render(<TbmPage />);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '+ 작업 등록' }));
    const dialog = screen.getByRole('dialog', { name: '작업 등록' });
    expect(dialog.tagName).toBe('DIALOG');

    const name = within(dialog).getByLabelText('작업명');
    expect(within(dialog).getByRole('button', { name: '등록' }).hasAttribute('disabled')).toBe(
      true,
    );
    fireEvent.change(name, { target: { value: '전기 배선' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '등록' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText(/T4 등록됨/)).toBeTruthy();
    expect(screen.getByRole('region', { name: 'T4 TBM 회의록' })).toBeTruthy();
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
