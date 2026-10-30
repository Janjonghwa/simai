import {
  A4_H,
  A4_PAD,
  A4_W,
  BAR_ATTR,
  FIELD_ATTR,
  NOTE_ATTR,
  OFFSCREEN_PX,
  PAGE_FIT_SLACK,
  PLACEHOLDER_ATTR,
  PREVIEW_SCALE_MIN,
} from '@/features/tbm/constants';

// 회의록 쪽 나눔(관제 데모와 같다). 한 장은 A4 다(constants).
// 눈에 안 보이는 사본에서 실제 DOM 높이로 표 행을 쪽에 넣고, 이어지는 쪽에는 표 머리글을 되풀이한다.
// 한 쪽보다 긴 칸은 글자 단위로 나눠 모든 내용을 이어지는 행에 남긴다(입력 칸 자체는 쪼개지 않는다).
// 서식(TbmFormSheet)이 쪽 나눔에 쓰는 표시: 칸 · 구역 제목 줄 · 안내 글(constants 의 *_ATTR).

const FIELD_SELECTOR = `[${FIELD_ATTR}]`;
const NOTE_SELECTOR = `[${NOTE_ATTR}]`;
const BAR_SELECTOR = `td[${BAR_ATTR}]`;

/** 보이는 영역(px)에 A4 한 장을 맞추는 축소율. 늘리지는 않는다. */
export const previewScale = (width: number, height: number) =>
  Math.max(PREVIEW_SCALE_MIN, Math.min(1, width / A4_W, height / A4_H));

/** 칸 글자. 줄바꿈은 <br> 로 들어 있어 textContent 로는 사라지므로 직접 모은다. */
function fieldText(el: Element): string {
  let text = '';
  el.childNodes.forEach((node) => {
    text += node.nodeName === 'BR' ? '\n' : (node.textContent ?? '');
  });
  return text;
}

const isHeadRow = (row: HTMLTableRowElement) =>
  Array.from(row.cells).every((cell) => cell.tagName === 'TH');

const hasText = (rest: string[][]) => rest.some((chars) => chars.length > 0);

/** 쪽 하나를 채워 가는 보이지 않는 상자. 가득 차면 쪽으로 내보내고 비운다. */
interface Pager {
  box: HTMLElement;
  pages: string[];
  isFull: () => boolean;
  isEmpty: () => boolean;
  flush: () => void;
}

function createPager(source: HTMLElement): Pager {
  const box = source.cloneNode(false) as HTMLElement;
  box.style.cssText = `position:absolute;left:-${OFFSCREEN_PX}px;top:0;width:${A4_W}px;padding:${A4_PAD}px;box-sizing:border-box;height:auto;visibility:hidden`;
  document.body.append(box);
  const title = source.querySelector('h3');
  const pages: string[] = [];
  const reset = () => {
    box.replaceChildren();
    if (title) box.append(title.cloneNode(true));
  };
  reset();
  return {
    box,
    pages,
    isFull: () => box.getBoundingClientRect().height > A4_H - PAGE_FIT_SLACK,
    isEmpty: () => box.childElementCount <= (title ? 1 : 0),
    flush: () => {
      pages.push(box.innerHTML);
      reset();
    },
  };
}

/** 표 하나를 쪽에 나눠 담는 중의 자리: 지금 쪽의 표 본문과, 이어지는 쪽에 되풀이할 머리글 행. */
interface Cursor {
  pager: Pager;
  table: HTMLTableElement;
  head: HTMLTableRowElement | null;
  body: HTMLTableSectionElement;
}

/** 지금 쪽에 표 껍데기(열 정의 포함)를 새로 열고 그 본문을 자리로 삼는다. 이어지는 쪽이면 머리글을 되풀이한다. */
function openBody(cur: Cursor, repeatHead: boolean) {
  const copy = cur.table.cloneNode(false) as HTMLTableElement;
  const cols = cur.table.querySelector('colgroup');
  if (cols) copy.append(cols.cloneNode(true));
  const body = document.createElement('tbody');
  copy.append(body);
  if (repeatHead && cur.head) body.append(cur.head.cloneNode(true));
  cur.pager.box.append(copy);
  cur.body = body;
}

/** 이 쪽에 넘친 행(row)을 다음 쪽 첫 줄로 옮긴다. 쪽 끝에 남은 구역 제목 줄(■)은 같이 옮긴다. */
function moveToNextPage(cur: Cursor, row: HTMLTableRowElement, repeatHead: boolean) {
  row.remove();
  const last = cur.body.rows[cur.body.rows.length - 1];
  const carry = last?.querySelector(BAR_SELECTOR) ? last : null;
  carry?.remove();
  if (!cur.body.rows.length) cur.body.parentElement?.remove();
  if (!cur.pager.isEmpty()) cur.pager.flush();
  openBody(cur, repeatHead);
  if (carry) cur.body.append(carry);
  cur.body.append(row);
}

/** 이어지는 행 하나. 첫 조각이 아니면 안내 글을 되풀이하지 않는다. */
function makePiece(sourceRow: HTMLTableRowElement, withNotes: boolean) {
  const piece = sourceRow.cloneNode(true) as HTMLTableRowElement;
  const cells = Array.from(piece.querySelectorAll(FIELD_SELECTOR));
  cells.forEach((cell) => cell.removeAttribute(PLACEHOLDER_ATTR));
  if (!withNotes) piece.querySelectorAll(NOTE_SELECTOR).forEach((note) => note.remove());
  return { piece, cells };
}

/** 칸마다 남은 글자(rest)의 앞에서 같은 글자 수만큼 넣어 쪽이 안 넘치는 가장 큰 수를 이분 탐색한다(칸에는 그 수만큼 들어간 채 남는다). */
function longestFit(pager: Pager, cells: Element[], rest: string[][]) {
  let lo = 1;
  let hi = Math.max(...rest.map((chars) => chars.length));
  let best = 0;
  while (lo <= hi) {
    const n = Math.floor((lo + hi) / 2);
    cells.forEach((cell, i) => {
      cell.textContent = rest[i]!.slice(0, n).join('');
    });
    if (pager.isFull()) {
      hi = n - 1;
    } else {
      best = n;
      lo = n + 1;
    }
  }
  return best;
}

/** 한 쪽을 통째로 써도 안 들어가는 행: 같은 열의 이어지는 행으로 나눈다. */
function splitRow(cur: Cursor, sourceRow: HTMLTableRowElement, row: HTMLTableRowElement) {
  const fields = Array.from(row.querySelectorAll(FIELD_SELECTOR));
  if (!fields.length) throw new Error('행을 나눌 수 없음');
  const rest = fields.map((field) => Array.from(fieldText(field)));
  row.remove();
  let first = true;
  let part = 0;
  while (hasText(rest)) {
    const { piece, cells } = makePiece(sourceRow, part === 0);
    cur.body.append(piece);
    const best = longestFit(cur.pager, cells, rest);
    if (best) {
      cells.forEach((cell, i) => {
        cell.textContent = rest[i]!.splice(0, best).join('');
      });
      if (hasText(rest)) {
        cur.pager.flush();
        openBody(cur, true);
      }
      part++;
    } else {
      // 새 쪽에서도 한 글자가 안 들어가면 더 나눌 수 없다.
      piece.remove();
      if (!first) throw new Error('행을 쪽에 놓지 못함');
      cur.pager.flush();
      openBody(cur, true);
    }
    first = false;
  }
}

function paginateTable(pager: Pager, table: HTMLTableElement) {
  const cur: Cursor = { pager, table, head: null, body: document.createElement('tbody') };
  openBody(cur, false);
  for (const sourceRow of Array.from(table.rows)) {
    const isHead = isHeadRow(sourceRow);
    if (isHead) cur.head = sourceRow;
    const row = sourceRow.cloneNode(true) as HTMLTableRowElement;
    cur.body.append(row);
    if (!pager.isFull()) continue;
    moveToNextPage(cur, row, !isHead);
    if (pager.isFull()) splitRow(cur, sourceRow, row);
  }
}

/**
 * 서식 한 벌(제목과 표들)을 A4 쪽으로 나눠 쪽마다 안쪽 HTML 을 돌려준다.
 * 행 하나를 한 쪽에도 놓지 못하면 오류를 던진다.
 */
export function paginateSheet(source: HTMLElement): string[] {
  const pager = createPager(source);
  try {
    for (const table of Array.from(source.children)) {
      if (table instanceof HTMLTableElement) paginateTable(pager, table);
    }
    if (!pager.isEmpty() || !pager.pages.length) pager.pages.push(pager.box.innerHTML);
    return pager.pages;
  } finally {
    pager.box.remove();
  }
}
