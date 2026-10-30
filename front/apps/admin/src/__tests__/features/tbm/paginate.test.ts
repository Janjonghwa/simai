import { A4_H, A4_PAD, A4_W } from '@/features/tbm/constants';
import { paginateSheet, previewScale } from '@/features/tbm/paginate';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// jsdom 은 레이아웃이 없다. 쪽 나눔 상자(body 에 붙는 복사본)의 높이를 행 수로 흉내 낸다.
const ROW_PX = 100;

function sheet(rows: { head?: boolean; bar?: boolean; text?: string }[][]) {
  const root = document.createElement('div');
  root.className = 'paper';
  const title = document.createElement('h3');
  title.textContent = '제목';
  root.append(title);
  for (const group of rows) {
    const table = document.createElement('table');
    const body = document.createElement('tbody');
    for (const row of group) {
      const tr = document.createElement('tr');
      const cell = document.createElement(row.head ? 'th' : 'td');
      if (row.bar) cell.setAttribute('data-tbm-bar', '');
      if (row.text !== undefined) {
        const field = document.createElement('span');
        field.setAttribute('data-tbm-field', '');
        field.textContent = row.text;
        cell.append(field);
      }
      tr.append(cell);
      body.append(tr);
    }
    table.append(body);
    root.append(table);
  }
  return root;
}

const rowsIn = (html: string) => {
  const holder = document.createElement('div');
  holder.innerHTML = html;
  return holder.querySelectorAll('tr').length;
};

describe('previewScale', () => {
  it('A4 한 장이 들어가는 만큼 줄이고 늘리지는 않는다', () => {
    expect(previewScale(A4_W, A4_H)).toBe(1);
    expect(previewScale(A4_W * 3, A4_H * 3)).toBe(1);
    expect(previewScale(A4_W / 2, A4_H * 4)).toBeCloseTo(0.5);
    expect(previewScale(A4_W * 4, A4_H / 4)).toBeCloseTo(0.25);
  });

  it('보이는 영역이 없어도 0 이 되지 않는다', () => {
    expect(previewScale(0, 0)).toBe(0.01);
    expect(previewScale(-24, -24)).toBe(0.01);
  });
});

describe('paginateSheet', () => {
  beforeEach(() => {
    // 상자 높이 = 안쪽 여백 + 제목 + 표 행 수 × ROW_PX. 한 쪽에는 행 10개까지 들어간다(76 + 10 × 100 ≤ 1121).
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: HTMLElement,
    ) {
      const body = this.style.left === '-100000px';
      const rows = body ? this.querySelectorAll('tr').length : 0;
      const height = body ? A4_PAD * 2 + rows * ROW_PX : 0;
      return { height, width: 0, top: 0, left: 0, right: 0, bottom: height, x: 0, y: 0 } as DOMRect;
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it('한 쪽에 들어가면 한 장이다', () => {
    const pages = paginateSheet(sheet([[{ text: 'a' }, { text: 'b' }], [{ text: 'c' }]]));
    expect(pages).toHaveLength(1);
    expect(rowsIn(pages[0]!)).toBe(3);
    expect(pages[0]).toContain('제목');
  });

  it('넘치면 다음 쪽으로 넘기고 쪽마다 제목을 단다', () => {
    const many = Array.from({ length: 12 }, (_, n) => ({ text: `r${n}` }));
    const pages = paginateSheet(sheet([many]));
    expect(pages).toHaveLength(2);
    expect(rowsIn(pages[0]!)).toBe(10);
    expect(rowsIn(pages[1]!)).toBe(2);
    pages.forEach((page) => expect(page).toContain('제목'));
  });

  it('이어지는 쪽에는 표 머리글 행을 되풀이한다', () => {
    const many = [{ head: true }, ...Array.from({ length: 12 }, (_, n) => ({ text: `r${n}` }))];
    const pages = paginateSheet(sheet([many]));
    expect(pages).toHaveLength(2);
    const second = document.createElement('div');
    second.innerHTML = pages[1]!;
    expect(second.querySelector('tr')?.firstElementChild?.tagName).toBe('TH');
  });

  it('쪽 끝에 구역 제목 줄만 남지 않게 다음 쪽으로 같이 넘긴다', () => {
    const rows = [
      ...Array.from({ length: 9 }, (_, n) => ({ text: `r${n}` })),
      { bar: true, text: '■ 구역' },
      { text: 'x'.repeat(5) },
    ];
    const pages = paginateSheet(sheet([rows]));
    expect(pages).toHaveLength(2);
    expect(pages[0]).not.toContain('■ 구역');
    expect(pages[1]).toContain('■ 구역');
  });

  it('한 쪽보다 긴 칸은 글자 단위로 나눠 모든 내용을 남긴다', () => {
    // 쪽에 칸 하나만 놓아도 행 높이가 쪽을 넘도록 행 높이를 키운다.
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(function (
      this: HTMLElement,
    ) {
      const body = this.style.left === '-100000px';
      const chars = body
        ? Array.from(this.querySelectorAll('[data-tbm-field]')).reduce(
            (sum, el) => sum + (el.textContent?.length ?? 0),
            0,
          )
        : 0;
      const height = body ? A4_PAD * 2 + 50 + chars * 10 : 0;
      return { height, width: 0, top: 0, left: 0, right: 0, bottom: height, x: 0, y: 0 } as DOMRect;
    });
    const long = 'ab'.repeat(200);
    const pages = paginateSheet(sheet([[{ text: long }]]));
    expect(pages.length).toBeGreaterThan(1);
    const joined = pages
      .map((page) => {
        const holder = document.createElement('div');
        holder.innerHTML = page;
        return Array.from(holder.querySelectorAll('[data-tbm-field]'))
          .map((el) => el.textContent)
          .join('');
      })
      .join('');
    expect(joined).toBe(long);
  });

  it('쪽 나눔 상자는 끝나면 문서에서 치운다', () => {
    paginateSheet(sheet([[{ text: 'a' }]]));
    expect(document.body.querySelector('[style*="-100000px"]')).toBeNull();
  });
});
