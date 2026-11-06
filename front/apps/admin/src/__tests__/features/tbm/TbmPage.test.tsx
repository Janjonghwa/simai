import { TbmPage } from '@/features/tbm/TbmPage';
import { TBM_FORM_KEY, useTbmFormStore } from '@/features/tbm/tbm.store';
import { MOCK_STATIC_OBJECTS } from '@/mocks/staticObjects';
import { createTbmMock } from '@/mocks/tbm';
import { MOCK_ZONES, SEEDED_VALID_FROM } from '@/mocks/zones';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

// 이 화면은 폴링·쿼리가 많아 화면을 내릴 때 요청이 중간에 취소된다. 그때 MSW(0.45) 소켓 가로채기 아래에서 undici 가
// 진짜 서버로 다시 붙어(CI 에서 ERR_TLS_CERT_ALTNAME_INVALID 로 터짐) 시험 밖으로 새어 나갔다. 핸들러는 '*/api/...' 라
// 주소와 무관하게 맞으므로, 이 파일만 존재하지 않는 도메인(.invalid, RFC 6761)을 써서 새는 연결이 DNS 에서 조용히 끝나게 한다.
vi.mock('@/lib/env', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/env')>();
  return {
    ...actual,
    env: {
      ...actual.env,
      VITE_API_BASE_URL: 'https://tbm-test.invalid',
      VITE_STOMP_URL: 'wss://tbm-test.invalid/ws',
    },
  };
});

// 3D 뷰어는 jsdom 에서 그릴 수 없다(WebGL·ResizeObserver 없음). 진짜 뷰어를 띄우면 오류 경계로 쓰러지며 MSW 가 못 막는
// 실제 연결이 나가 CI 에서 TLS 오류(ERR_TLS_CERT_ALTNAME_INVALID)로 깨졌다. /zones·관제 시험처럼 자리표시로 바꾸고,
// 편집 동작은 대화상자 패널(숫자·추가·삭제·가져오기)과 layoutEditorModel 시험으로 확인한다.
vi.mock('@/features/viewer3d', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/viewer3d')>();
  const { forwardRef } = await import('react');
  return {
    ...actual,
    Viewer3D: forwardRef<unknown, { initialMode?: string; topView?: boolean }>(
      function Viewer3DStub(props, ref) {
        void ref; // forwardRef 는 두 인자를 받아야 경고가 없다
        // 진짜 ViewerShell 과 같은 data 속성만 남긴다(모드·위에서 보기).
        return (
          <div
            data-testid="viewer3d-stub"
            data-viewer-mode={props.initialMode ?? 'LIVE'}
            data-viewer-top-view={props.topView ? 'on' : 'off'}
          />
        );
      },
    ),
  };
});

const TEST_ZONES = [
  ...MOCK_ZONES,
  {
    zone_id: 'z-05',
    zone_version: 1,
    name: '높이 없는 바닥 위험구역',
    zone_kind: 'HAZARD' as const,
    eval_type: 'GROUND_FOOTPRINT' as const,
    polygon_xy: [
      [1.0, 1.0],
      [3.0, 1.0],
      [3.0, 2.5],
      [1.0, 2.5],
    ] as [number, number][],
    z_min_m: null,
    z_max_m: null,
    severity: 'HIGH' as const,
    requirements: null,
    has_guardrail: null,
    valid_from: SEEDED_VALID_FROM,
    valid_to: null,
  },
];

const geometryHandlers = [
  http.get('*/api/zones', () => HttpResponse.json(TEST_ZONES)),
  http.get('*/api/static-objects', () => HttpResponse.json(MOCK_STATIC_OBJECTS)),
  http.get('*/api/scenes/current', () =>
    HttpResponse.json({ status: 404, code: 'SCENE_NOT_FOUND', detail: '없음' }, { status: 404 }),
  ),
];

let tbmMock = createTbmMock();
const server = setupServer(...tbmMock.handlers, ...geometryHandlers);

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
  window.innerWidth = 1280;
  tbmMock = createTbmMock();
  server.resetHandlers(...tbmMock.handlers, ...geometryHandlers);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

afterAll(() => {
  server.close();
});

/** 고른 작업의 ⋯ 메뉴에서 항목(작업 수정 · 작업 삭제)을 연다. */
const pickTaskMenu = (taskId: string, item: '작업 수정' | '작업 삭제') => {
  fireEvent.click(screen.getByRole('button', { name: `${taskId} 작업 메뉴` }));
  fireEvent.click(screen.getByRole('menuitem', { name: item }));
};

/** 법령 근거 블록(가운데). */
const legalPanel = () => screen.findByRole('region', { name: '법령 근거' });

/** 작업 한 건을 종료 상태로 만든다(서명 · 결재 패널은 종료한 작업에만 열린다). */
const endWork = (workId = '1') => {
  const work = tbmMock.works.find((w) => w.work_id === workId)!;
  work.status = 'DONE';
  work.actual_started_at = Date.now() - 3_600_000;
  work.actual_ended_at = Date.now();
};

/** 단계 줄의 서명하기로 서명 · 결재 패널을 펼친다. */
const openSignPanel = async () => {
  fireEvent.click(await screen.findByRole('button', { name: '서명하기' }));
  return screen.findByRole('region', { name: '참석자 서명' });
};

describe('왼쪽 패널', () => {
  it('처음에는 레이아웃을 크게 보이고 오른쪽 위 단추 하나로 평가 · 간섭 보기와 바꾼다', async () => {
    render(<TbmPage />);
    const panel = await screen.findByRole('region', { name: 'T1 가상 레이아웃' });
    expect(within(panel).getByRole('img', { name: /평면 도식/ })).toBeTruthy();
    expect(within(panel).queryByRole('table')).toBeNull();
    // 머리에는 3D 레이아웃 편집과 보기 전환 단추 둘뿐이고 글자가 보기 전환을 알린다.
    const toggle = within(panel).getByRole('button', { name: '평가 · 간섭 보기' });
    expect(within(panel).getByRole('button', { name: '3D 레이아웃 편집' })).toBeTruthy();
    expect(within(panel).getAllByRole('button')).toHaveLength(2);

    fireEvent.click(toggle);
    const detail = screen.getByRole('region', { name: 'T1 평가 · 간섭' });
    expect(within(detail).getByRole('table')).toBeTruthy();
    expect(within(detail).queryByRole('img', { name: /평면 도식/ })).toBeNull();
    expect(within(detail).getByText('T1 ↔ T2')).toBeTruthy();
    expect(within(detail).getAllByRole('button')).toHaveLength(2);

    fireEvent.click(within(detail).getByRole('button', { name: '레이아웃 보기' }));
    expect(screen.getByRole('region', { name: 'T1 가상 레이아웃' })).toBeTruthy();
  });

  it('작업을 바꿔도 고른 보기를 유지한다', async () => {
    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });
    fireEvent.click(screen.getByRole('button', { name: '평가 · 간섭 보기' }));
    fireEvent.click(screen.getByRole('button', { name: /^T2 자재 반입/ }));
    expect(screen.getByRole('region', { name: 'T2 평가 · 간섭' })).toBeTruthy();
  });
});

describe('작업 등록 · 수정 모달', () => {
  it('+ 작업 등록은 모달 창을 열고, 작업명·시작·끝·공종·인원만 받아 등록하면 새 작업을 고른다', async () => {
    render(<TbmPage />);
    await screen.findByRole('button', { name: '보고서 재작성' });

    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '+ 작업 등록' }));
    const dialog = screen.getByRole('dialog', { name: '작업 등록' });
    expect(dialog.tagName).toBe('DIALOG');

    const name = within(dialog).getByLabelText('작업명');
    const workTypeSelect = within(dialog).getByLabelText('공종');
    for (const label of ['시작', '끝', '인원'])
      expect(within(dialog).getByLabelText(label)).toBeTruthy();
    // 구역은 세팅 뒤 구역 단계에서, 가상 레이아웃은 3D 레이아웃 편집에서 정한다. 반장은 받지 않는다.
    for (const label of ['구역', '반장', '가상 레이아웃']) {
      expect(within(dialog).queryByLabelText(label)).toBeNull();
    }
    expect(within(dialog).getByRole('button', { name: '등록' }).hasAttribute('disabled')).toBe(
      true,
    );

    fireEvent.change(name, { target: { value: '전기 배선' } });
    fireEvent.change(workTypeSelect, { target: { value: 'OPENING_WORK' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '등록' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText(/T4 등록됨/)).toBeTruthy();
    expect(screen.getByRole('region', { name: 'T4 TBM 회의록' })).toBeTruthy();

    await waitFor(() => {
      const createdInMock = tbmMock.works.find((w) => w.name === '전기 배선');
      expect(createdInMock).toMatchObject({
        work_type: 'OPENING_WORK',
        zone_id: null,
        lead: null,
        layout_id: null,
      });
    });
  });

  it('작업 수정은 공종·인원을 PUT /api/work-units/{id} 로 갱신하고, 폼에 없는 구역·반장·레이아웃은 그대로 둔다', async () => {
    render(<TbmPage />);
    await screen.findByRole('button', { name: '보고서 재작성' });

    pickTaskMenu('T1', '작업 수정');
    const dialog = screen.getByRole('dialog', { name: '작업 수정' });
    fireEvent.change(within(dialog).getByLabelText('공종'), { target: { value: 'HIGH_PLACE' } });
    fireEvent.change(within(dialog).getByLabelText('인원'), { target: { value: '5' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '저장' }));

    await waitFor(() => {
      expect(screen.getByText(/T1 수정됨/)).toBeTruthy();
      expect(tbmMock.works[0]).toMatchObject({
        work_type: 'HIGH_PLACE',
        planned_people: 5,
        zone_id: 'z-04',
        lead: 'A조',
        layout_id: 101,
      });
    });
  });

  it('Esc(cancel)와 바깥(backdrop) 누르기, 취소 단추로 닫는다', async () => {
    render(<TbmPage />);
    await screen.findByRole('button', { name: '+ 작업 등록' });
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
  it('법령 근거 블록의 최근 사건 반영은 구역 번호 대신 이름만 보인다(이름을 모르면 구역)', async () => {
    render(<TbmPage />);
    const detail = await legalPanel();
    const recent = await within(detail).findByRole('region', { name: '최근 사건 반영' });
    expect(recent.textContent).toContain('구역');
    expect(recent.textContent).toContain('5건');
    expect(recent.textContent).not.toContain('z-04');
  });

  it('법령 근거 블록이 인용 조문(citations)·법령 지식 그래프 SVG(노드 클릭 상세)·생성 단계(steps)를 표시하고 sections 카드는 그리지 않는다', async () => {
    render(<TbmPage />);
    const detail = await legalPanel();
    await within(detail).findByTestId('report-citation-OSH-STD-A43');

    // 대책·위험 sections 카드는 어디에도 없다(회의록 문서가 그 글을 담는다)
    expect(screen.queryByTestId('report-section-hazards')).toBeNull();
    expect(screen.queryByTestId('report-section-measures')).toBeNull();
    expect(screen.queryByTestId('report-section-focus')).toBeNull();
    expect(screen.queryByTestId('report-section-legal')).toBeNull();
    expect(within(detail).getByRole('region', { name: '인용 법령 조문' })).toBeTruthy();

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

  it('보고서 재작성 요청(POST 202) → 2초 간격 폴링(GET /api/reports/tbm/{id}) → 완료(SUCCEEDED) 흐름이 동작한다', async () => {
    render(<TbmPage />);
    const regenBtn = await screen.findByRole('button', { name: '보고서 재작성' });

    fireEvent.click(regenBtn);
    // 만드는 중에는 재작성 단추가 막히고 생성 단계 목록이 보인다.
    await waitFor(() => {
      expect(
        (screen.getByRole('button', { name: '보고서 재작성' }) as HTMLButtonElement).disabled,
      ).toBe(true);
    });

    // 2초 간격 폴링 실행
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    await waitFor(() => {
      expect(
        (screen.getByRole('button', { name: '보고서 재작성' }) as HTMLButtonElement).disabled,
      ).toBe(false);
      const versionSelect = screen.getByLabelText('보고서 버전') as HTMLSelectElement;
      expect(versionSelect.textContent).toContain('v2 (SUCCEEDED)');
    });
    expect(screen.getByRole('region', { name: 'T1 TBM 회의록' }).textContent).toContain('v2');
  });

  it('FAILED 상태 보고서는 failure_reason 안내 문구를 표시하고 지어낸 문장으로 채우지 않는다', async () => {
    tbmMock.setNextReportOutcome({
      status: 'FAILED',
      failureReason: 'CITATION_VIOLATION',
    });

    render(<TbmPage />);
    const regenBtn = await screen.findByRole('button', { name: '보고서 재작성' });
    fireEvent.click(regenBtn);

    await waitFor(() => {
      expect(
        (screen.getByRole('button', { name: '보고서 재작성' }) as HTMLButtonElement).disabled,
      ).toBe(true);
    });

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    await waitFor(() => {
      const alert = screen.getByTestId('tbm-report-failed');
      expect(alert.textContent).toContain('CITATION_VIOLATION');
    });

    // 법령 근거 블록에서도 지어낸 인용 · 본문 없이 실패 사유만 보인다
    const detail = screen.getByRole('region', { name: '법령 근거' });
    expect(within(detail).queryByRole('region', { name: '인용 법령 조문' })).toBeNull();
    expect(within(detail).queryByTestId('report-citation-OSH-STD-A43')).toBeNull();
    expect(within(detail).getByRole('alert').textContent).toContain('CITATION_VIOLATION');
  });

  it('409 응답(WORK_HAS_NO_LAYOUT · REPORT_IN_PROGRESS)을 구분해 안내한다', async () => {
    // 1) WORK_HAS_NO_LAYOUT: 가상 레이아웃이 끊긴 작업(T1)에 보고서 재작성 요청
    tbmMock = createTbmMock({ pollsBeforeComplete: 2 });
    server.resetHandlers(...tbmMock.handlers, ...geometryHandlers);
    tbmMock.works[0]!.layout_id = null;

    render(<TbmPage />);
    await screen.findByRole('button', { name: '보고서 재작성' });

    fireEvent.click(screen.getByRole('button', { name: '보고서 재작성' }));
    await waitFor(() => {
      expect(screen.getByTestId('tbm-request-error').textContent).toContain('WORK_HAS_NO_LAYOUT');
    });

    // 2) REPORT_IN_PROGRESS: T2(레이아웃 연결됨) 선택 후, 다른 화면이 먼저 생성을 시작한 상태에서 재작성 요청
    fireEvent.click(screen.getByRole('button', { name: /^T2 자재 반입/ }));
    await waitFor(() => {
      expect(
        (screen.getByRole('button', { name: '보고서 재작성' }) as HTMLButtonElement).disabled,
      ).toBe(false);
    });
    const t2Reports = tbmMock.reportsByWork.get('2')!;
    t2Reports.push({
      ...t2Reports.at(-1)!,
      report_id: 999,
      report_version: 2,
      status: 'GENERATING',
    });

    fireEvent.click(screen.getByRole('button', { name: '보고서 재작성' }));
    await waitFor(() => {
      expect(screen.getByTestId('tbm-request-error').textContent).toContain('REPORT_IN_PROGRESS');
    });
  });

  it('보고서 생성 요청이 404 WORK_NOT_FOUND 로 실패하면 사용자가 이해할 안내 문구를 표시한다', async () => {
    server.use(
      http.post('*/api/reports/tbm', () =>
        HttpResponse.json(
          { status: 404, code: 'WORK_NOT_FOUND', detail: '작업을 찾을 수 없습니다.' },
          { status: 404 },
        ),
      ),
    );

    render(<TbmPage />);
    const regenBtn = await screen.findByRole('button', { name: '보고서 재작성' });
    fireEvent.click(regenBtn);

    await waitFor(() => {
      const alert = screen.getByTestId('tbm-request-error');
      expect(alert.textContent).toContain('작업을 찾을 수 없습니다');
      expect(alert.textContent).toContain('WORK_NOT_FOUND');
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

  it('서버가 빈 작업 목록(works: [])을 돌려주면 데모 작업을 보이지 않고 빈 상태 화면을 표시한다', async () => {
    server.use(http.get('*/api/work-units', () => HttpResponse.json({ works: [] })));

    render(<TbmPage />);
    const emptyState = await screen.findByTestId('tbm-empty-state');
    await waitFor(() => {
      expect(emptyState.textContent).toContain('등록된 작업이 없습니다');
    });
    expect(screen.queryByTestId('tbm-demo-badge')).toBeNull();
    expect(screen.queryByRole('region', { name: 'T1 가상 레이아웃' })).toBeNull();
  });

  it('서버 모드에서 작업 등록·수정·삭제 API 가 실패하면 오류 안내를 띄우고 로컬 가짜 작업을 남기지 않는다', async () => {
    server.use(
      http.post('*/api/work-units', () =>
        HttpResponse.json(
          { status: 500, code: 'INTERNAL_ERROR', detail: '서버 오류' },
          { status: 500 },
        ),
      ),
      http.put('*/api/work-units/:id', () =>
        HttpResponse.json(
          { status: 500, code: 'INTERNAL_ERROR', detail: '서버 오류' },
          { status: 500 },
        ),
      ),
      http.delete('*/api/work-units/:id', () =>
        HttpResponse.json(
          { status: 500, code: 'INTERNAL_ERROR', detail: '서버 오류' },
          { status: 500 },
        ),
      ),
    );

    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });

    // 1) 작업 등록 실패 → 롤백 및 오류 안내, 가짜 T4 가 남지 않는다
    fireEvent.click(screen.getByRole('button', { name: '+ 작업 등록' }));
    const addDialog = screen.getByRole('dialog', { name: '작업 등록' });
    fireEvent.change(within(addDialog).getByLabelText('작업명'), {
      target: { value: '실패할 가짜 작업' },
    });
    fireEvent.click(within(addDialog).getByRole('button', { name: '등록' }));

    await waitFor(() => {
      expect(screen.getByTestId('tbm-mutation-error').textContent).toContain(
        '작업 등록에 실패했습니다',
      );
    });
    expect(screen.queryByRole('button', { name: /실패할 가짜 작업/ })).toBeNull();
    expect(screen.getByRole('region', { name: 'T1 가상 레이아웃' })).toBeTruthy();

    // 2) 작업 수정 실패 → 롤백 및 오류 안내
    pickTaskMenu('T1', '작업 수정');
    const editDialog = screen.getByRole('dialog', { name: '작업 수정' });
    fireEvent.change(within(editDialog).getByLabelText('작업명'), {
      target: { value: '수정 실패 이름' },
    });
    fireEvent.click(within(editDialog).getByRole('button', { name: '저장' }));

    await waitFor(() => {
      expect(screen.getByTestId('tbm-mutation-error').textContent).toContain(
        '작업 수정에 실패했습니다',
      );
    });
    expect(screen.queryByRole('button', { name: /수정 실패 이름/ })).toBeNull();

    // 3) 작업 삭제 실패 → 롤백 및 오류 안내 (T1 이 그대로 남아 있음)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    pickTaskMenu('T1', '작업 삭제');
    expect(confirm.mock.calls[0]![0]).toContain('작업을 삭제하시겠습니까?');

    await waitFor(() => {
      expect(screen.getByTestId('tbm-mutation-error').textContent).toContain(
        '작업 삭제에 실패했습니다',
      );
    });
    expect(screen.getByRole('button', { name: /^T1 개구부 옆 비계 설치/ })).toBeTruthy();
  });
});

describe('3D 가상 레이아웃 편집기', () => {
  it('3D 뷰어와 위에서 보기를 전환하고, 물체 추가·수치 다듬기·삭제 후 저장하면 새 레이아웃을 만들고 작업에 연결한다', async () => {
    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });

    fireEvent.click(screen.getByRole('button', { name: '3D 레이아웃 편집' }));
    const dialog = await screen.findByRole('dialog', { name: '3D 가상 레이아웃 편집' });

    // 3D 뷰어가 열리고 기본은 top-view off
    const viewport = within(dialog).getByTestId('layout-editor-viewport');
    const viewer = viewport.firstElementChild as HTMLElement;
    expect(viewer.getAttribute('data-viewer-mode')).toBe('PLANNED');
    expect(viewer.getAttribute('data-viewer-top-view')).toBe('off');

    // "위에서 보기" 토글 확인
    const topViewBtn = within(dialog).getByRole('button', { name: '위에서 보기' });
    fireEvent.click(topViewBtn);
    expect(topViewBtn.getAttribute('aria-pressed')).toBe('true');
    expect(viewer.getAttribute('data-viewer-top-view')).toBe('on');
    fireEvent.click(within(dialog).getByRole('button', { name: '입체 시점으로 보기' }));
    expect(viewer.getAttribute('data-viewer-top-view')).toBe('off');

    // 새 가상 레이아웃 만들기 선택
    fireEvent.change(within(dialog).getByLabelText('편집할 레이아웃'), {
      target: { value: 'new' },
    });
    fireEvent.change(within(dialog).getByLabelText('레이아웃 이름'), {
      target: { value: '신규 3D 비계 배치' },
    });

    // 작업면(단상 작업면 top 0.4m)을 고른 뒤 비계 추가 + 장비 추가 후 장비 삭제
    await waitFor(() => {
      const surfaceSelect = within(dialog).getByLabelText('놓을 기준면') as HTMLSelectElement;
      expect(surfaceSelect.options.length).toBeGreaterThan(1);
    });
    fireEvent.change(within(dialog).getByLabelText('놓을 기준면'), {
      target: { value: 'ws-0a0c0001' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '+ 비계' }));
    fireEvent.click(within(dialog).getByRole('button', { name: '+ 장비' }));

    expect(within(dialog).getByText('배치 물체 (2)')).toBeTruthy();

    // 현재 선택된 두 번째 물체(장비) 삭제 → 첫 번째 물체(비계)만 남음
    fireEvent.click(within(dialog).getByRole('button', { name: '물체 삭제' }));
    expect(within(dialog).getByText('배치 물체 (1)')).toBeTruthy();

    // 남은 비계 항목의 이름·크기·회전 다듬기
    fireEvent.change(within(dialog).getByLabelText('물체 이름'), {
      target: { value: '단상 위 이동식 비계' },
    });
    fireEvent.change(within(dialog).getByLabelText('폭 X'), {
      target: { value: '2.4' },
    });
    fireEvent.change(within(dialog).getByLabelText('회전 각도'), {
      target: { value: '45' },
    });

    // 저장(POST /api/layouts) → 작업(T1)에 새 layout_id(104) 연결(PUT /api/work-units/1)
    fireEvent.click(within(dialog).getByRole('button', { name: '레이아웃 저장 · 작업 연결' }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '3D 가상 레이아웃 편집' })).toBeNull();
      expect(tbmMock.layouts[0]).toMatchObject({
        layout_id: 104,
        name: '신규 3D 비계 배치',
        revision: 1,
      });
      expect(tbmMock.layouts[0]?.items[0]).toMatchObject({
        kind: 'SCAFFOLD',
        label: '단상 위 이동식 비계',
      });
      expect(tbmMock.works[0]?.layout_id).toBe(104);
    });
  });

  it('구역에서 가져오기: 높이가 없는 구역은 Z 값을 비워 두고 사용자가 입력하기 전까지 저장을 막는다', async () => {
    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });

    fireEvent.click(screen.getByRole('button', { name: '3D 레이아웃 편집' }));
    const dialog = await screen.findByRole('dialog', { name: '3D 가상 레이아웃 편집' });

    fireEvent.change(within(dialog).getByLabelText('편집할 레이아웃'), {
      target: { value: 'new' },
    });

    await waitFor(() => {
      const zoneSelect = within(dialog).getByLabelText('구역에서 가져오기') as HTMLSelectElement;
      expect(zoneSelect.options.length).toBeGreaterThan(2);
    });

    // 높이 범위가 없는 구역(z-05) 가져오기
    fireEvent.change(within(dialog).getByLabelText('구역에서 가져오기'), {
      target: { value: 'z-05' },
    });

    const bottomZInput = within(dialog).getByLabelText('바닥 높이 Z') as HTMLInputElement;
    const heightInput = within(dialog).getByLabelText('높이 Z') as HTMLInputElement;
    expect(bottomZInput.value).toBe('');
    expect(heightInput.value).toBe('');

    // Z 미입력 상태에서 저장 시도 → 검증 오류로 차단
    fireEvent.click(within(dialog).getByRole('button', { name: '레이아웃 저장 · 작업 연결' }));
    expect(within(dialog).getByTestId('layout-editor-error').textContent).toContain(
      '바닥 높이(Z)가 비어 있습니다',
    );

    // 사용자가 직접 바닥 높이와 높이를 입력하면 저장 성공
    fireEvent.change(bottomZInput, { target: { value: '0' } });
    fireEvent.change(heightInput, { target: { value: '1.5' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '레이아웃 저장 · 작업 연결' }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: '3D 가상 레이아웃 편집' })).toBeNull();
    });
  });

  it('기존 레이아웃 수정 시 revision 충돌(409 LAYOUT_REVISION_CONFLICT)이 나면 안내하고 다시 불러올 수 있다', async () => {
    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });

    fireEvent.click(screen.getByRole('button', { name: '3D 레이아웃 편집' }));
    const dialog = await screen.findByRole('dialog', { name: '3D 가상 레이아웃 편집' });

    // 레이아웃 #101 (rev.3) 상세가 불러와질 때까지 대기
    await waitFor(() => {
      const nameInput = within(dialog).getByLabelText('레이아웃 이름') as HTMLInputElement;
      expect(nameInput.value).toContain('개구부 옆 비계 배치');
    });

    // 서버 쪽에서 다른 사람이 먼저 수정해 revision 이 4 로 올라간 상황 흉내
    tbmMock.layouts[0]!.revision = 4;
    tbmMock.layouts[0]!.name = '다른 사람이 고친 레이아웃';

    fireEvent.click(within(dialog).getByRole('button', { name: '레이아웃 저장 · 작업 연결' }));

    await waitFor(() => {
      expect(within(dialog).getByTestId('layout-editor-error').textContent).toContain(
        '다른 사람이 먼저 고침 — 다시 불러오기',
      );
    });

    // "다시 불러오기" 단추를 누르면 서버의 최신 버전(rev.4)을 불러와 충돌 안내가 사라진다
    fireEvent.click(within(dialog).getByRole('button', { name: '다시 불러오기' }));
    await waitFor(() => {
      expect(within(dialog).queryByTestId('layout-editor-error')).toBeNull();
      const nameInput = within(dialog).getByLabelText('레이아웃 이름') as HTMLInputElement;
      expect(nameInput.value).toBe('다른 사람이 고친 레이아웃');
    });
  });

  it('휴대폰 폭(좁은 화면)에서는 편집 대신 "PC 에서 편집" 안내와 읽기 전용 미리보기를 보인다', async () => {
    window.innerWidth = 500;
    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });

    fireEvent.click(screen.getByRole('button', { name: '3D 레이아웃 편집' }));
    const dialog = await screen.findByRole('dialog', { name: '3D 가상 레이아웃 편집' });

    const readonlyNotice = within(dialog).getByTestId('layout-mobile-readonly');
    expect(readonlyNotice.textContent).toContain('PC 에서 편집');
    expect(within(dialog).queryByRole('button', { name: '레이아웃 저장 · 작업 연결' })).toBeNull();
  });
});

describe('회의록', () => {
  it('A4 쪽 미리보기를 보이고, 보고서가 없으면 편집·되돌리기·출력을 막는다', async () => {
    // T1 은 보고서가 없는 작업으로 둔다
    tbmMock.reportsByWork.delete('1');
    render(<TbmPage />);
    const blank = await screen.findByRole('region', { name: 'T1 TBM 회의록' });
    await within(await legalPanel()).findByRole('button', { name: '보고서 작성' });
    for (const name of ['편집', '되돌리기', '출력']) {
      expect((within(blank).getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(
        true,
      );
    }

    // 보고서가 있는 작업을 고르면 편집·출력이 열리고 제목에 판 번호가 붙는다
    fireEvent.click(screen.getByRole('button', { name: /^T2 자재 반입/ }));
    const minutes = await screen.findByRole('region', { name: 'T2 TBM 회의록' });
    await waitFor(() => {
      expect(
        (within(minutes).getByRole('button', { name: '편집' }) as HTMLButtonElement).disabled,
      ).toBe(false);
    });
    expect(minutes.textContent).toContain('TBM 회의록 · v1');
    const preview = within(minutes).getByRole('group', { name: 'TBM 회의록 미리보기' });
    expect(preview.textContent).toContain('Tool Box Meeting 회의록');
    expect(preview.textContent).toContain('자재 반입');
  });

  it('편집은 모달 창에서 하고 고친 칸은 저장소에 남는다(자동 저장)', async () => {
    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });
    await waitFor(() =>
      expect((screen.getByRole('button', { name: '편집' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
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

  it('편집 창의 되돌리기는 고친 칸을 버린다', async () => {
    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });
    await waitFor(() =>
      expect((screen.getByRole('button', { name: '편집' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
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

  it('출력은 인쇄 창을 여는 동안만 출력용 사본을 그린다', async () => {
    const print = vi.fn(() => {
      // 인쇄 창이 열린 시점에는 출력용 사본(@page A4)이 문서에 있다.
      expect(document.body.textContent).toContain('@page');
    });
    vi.stubGlobal('print', print);
    render(<TbmPage />);
    await screen.findByRole('region', { name: 'T1 가상 레이아웃' });
    await waitFor(() =>
      expect((screen.getByRole('button', { name: '편집' }) as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    fireEvent.click(screen.getByRole('button', { name: '출력' }));
    await waitFor(() => expect(print).toHaveBeenCalledTimes(1));
    expect(document.body.textContent).not.toContain('@page');
    vi.unstubAllGlobals();
  });
});

function mockSignatureCanvas() {
  const context = {
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    clearRect: vi.fn(),
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context as unknown as CanvasRenderingContext2D,
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) =>
    callback(new Blob(['png'], { type: 'image/png' })),
  );
  HTMLCanvasElement.prototype.setPointerCapture = vi.fn();
}

function fillSignature(panel: HTMLElement, name = '홍길동') {
  fireEvent.change(within(panel).getByLabelText('서명자 실명'), { target: { value: name } });
  fireEvent.pointerDown(within(panel).getByLabelText('서명 패드'), {
    clientX: 10,
    clientY: 10,
    pointerId: 1,
  });
  fireEvent.click(within(panel).getByLabelText('고지를 확인했습니다'));
}

describe('작업 세션과 서명', () => {
  it('작업을 시작하고 종료하면 새 보고서의 마무리를 표시한다', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<TbmPage />);
    fireEvent.click(await screen.findByRole('button', { name: '작업 시작' }));
    fireEvent.click(await screen.findByRole('button', { name: '작업 종료' }));
    expect(await screen.findByText('작업 마무리 작성 중')).toBeTruthy();
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: '보고서 버전' }).textContent).toContain('v2'),
    );
    await act(async () => {
      vi.advanceTimersByTime(2200);
    });
    // 마무리 문단은 따로 줄을 두지 않고 작업 중 사건 띠 안에 들어간다.
    const band = await screen.findByRole('status', { name: '작업 중 사건' });
    expect(await within(band).findByText(/작업 구간의 위험 이벤트 집계/)).toBeTruthy();
    expect(screen.queryByText(/작업 마무리:/)).toBeNull();
    vi.restoreAllMocks();
  });

  it('다른 화면에서 종료하고 들어와도 마무리를 만드는 중이면 작성 중이라고 알린다', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    // 마무리가 끝나지 않은 채로 두려고 보고서 완성을 늦춘다(목은 id 로 받을 때마다 한 번씩 센다).
    tbmMock = createTbmMock({ pollsBeforeComplete: 99 });
    server.resetHandlers(...tbmMock.handlers, ...geometryHandlers);
    const first = render(<TbmPage />);
    fireEvent.click(await screen.findByRole('button', { name: '작업 시작' }));
    fireEvent.click(await screen.findByRole('button', { name: '작업 종료' }));
    expect(await screen.findByText('작업 마무리 작성 중')).toBeTruthy();
    // 이 화면의 안내(session.message)는 사라지고, 서버가 마무리를 만드는 중인 보고서만 남는다.
    // 끝난 작업은 기본 선택에서 빠지므로(진행·예정 우선) 그 작업을 initialWorkId 로 연다.
    first.unmount();
    render(<TbmPage initialWorkId="1" />);
    expect(await screen.findByText('작업 마무리 작성 중')).toBeTruthy();
    vi.restoreAllMocks();
  });

  it('다른 작업이 진행 중이면 409 안내를 표시한다', async () => {
    // 세팅·구역 전 시작 경고(확인)는 계속 진행으로 답한다.
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<TbmPage />);
    fireEvent.click(await screen.findByRole('button', { name: '작업 시작' }));
    await screen.findByRole('button', { name: '작업 종료' });
    fireEvent.click(screen.getByRole('button', { name: /^T2 자재 반입/ }));
    fireEvent.click(await screen.findByRole('button', { name: '작업 시작' }));
    expect(await screen.findByText(/진행 중인 작업이 있습니다:/)).toBeTruthy();
  });

  it('서명 등록에 빠진 실명·서명·고지를 각각 안내한다', async () => {
    mockSignatureCanvas();
    endWork();
    render(<TbmPage initialWorkId="1" />);
    const panel = await openSignPanel();
    const notice = () => within(panel).queryByText(/서명 등록을 위해 필요:/);
    expect(notice()?.textContent).toContain('실명 · 서명 · 고지 확인');
    fireEvent.change(within(panel).getByLabelText('서명자 실명'), { target: { value: '홍길동' } });
    expect(notice()?.textContent).toContain('서명 · 고지 확인');
    fireEvent.pointerDown(within(panel).getByLabelText('서명 패드'), {
      clientX: 10,
      clientY: 10,
      pointerId: 1,
    });
    expect(notice()?.textContent).toContain('고지 확인');
    fireEvent.click(within(panel).getByLabelText('고지를 확인했습니다'));
    expect(notice()).toBeNull();
    expect(
      (within(panel).getByRole('button', { name: '서명 등록' }) as HTMLButtonElement).disabled,
    ).toBe(false);
    vi.restoreAllMocks();
  });

  it.each([false, true])('서명 등록 및 오래된 버전 안내 (stale=%s)', async (stale) => {
    mockSignatureCanvas();
    if (stale)
      server.use(
        http.post('*/api/reports/tbm/:reportId/signatures', () =>
          HttpResponse.json({ code: 'REPORT_VERSION_STALE' }, { status: 409 }),
        ),
      );
    endWork();
    render(<TbmPage initialWorkId="1" />);
    const panel = await openSignPanel();
    fireEvent.change(within(panel).getByLabelText('서명자 실명'), { target: { value: '홍길동' } });
    fireEvent.pointerDown(within(panel).getByLabelText('서명 패드'), {
      clientX: 10,
      clientY: 10,
      pointerId: 1,
    });
    fireEvent.click(within(panel).getByLabelText('고지를 확인했습니다'));
    fireEvent.click(within(panel).getByRole('button', { name: '서명 등록' }));
    expect(
      await within(panel).findByText(
        stale ? '새 버전이 있어 다시 서명해야 합니다' : '서명을 등록했습니다.',
      ),
    ).toBeTruthy();
    if (!stale) expect(await within(panel).findByText(/홍길동/)).toBeTruthy();
    vi.restoreAllMocks();
  });
});

describe('관리자 최종 결재', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    server.events.removeAllListeners('request:start');
  });

  it.each([0, 1])('참석자 %i명 뒤 확인 대화상자로 결재하고 입력을 닫는다', async (count) => {
    mockSignatureCanvas();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const bodies: unknown[] = [];
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST' && request.url.endsWith('/signatures')) {
        void request
          .clone()
          .json()
          .then((body: unknown) => bodies.push(body));
      }
    });
    endWork();
    render(<TbmPage initialWorkId="1" />);
    const panel = await openSignPanel();
    await within(panel).findByText('참석자 서명 0명');
    if (count) {
      fillSignature(panel);
      fireEvent.click(within(panel).getByRole('button', { name: '서명 등록' }));
      await within(panel).findByText('서명을 등록했습니다.');
      expect(within(panel).queryByText('참석자 서명 0명')).toBeNull();
    }
    fillSignature(panel, '결재 실명');
    fireEvent.click(within(panel).getByRole('button', { name: '관리자 결재' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(within(panel).queryByText('결재 완료')).toBeNull();
    expect(within(panel).getByLabelText('서명 패드')).toBeTruthy();
    expect(bodies).toHaveLength(count);
    confirm.mockReturnValue(true);
    fireEvent.click(within(panel).getByRole('button', { name: '관리자 결재' }));
    await within(panel).findByText('결재 완료');
    expect(within(panel).queryByLabelText('서명 패드')).toBeNull();
    expect(within(panel).queryByRole('button', { name: '서명 등록' })).toBeNull();
    expect(panel.textContent).toContain('admin');
    expect(bodies.at(-1)).toMatchObject({ signer_role: 'APPROVER', signer_name: '결재 실명' });
    if (count) expect(bodies[0]).toMatchObject({ signer_role: 'PARTICIPANT' });
    else expect(confirm.mock.calls[0]?.[0]).toContain('참석자 서명 0명');
    const approved = tbmMock.reportsByWork.get('1')?.[0];
    expect(approved?.approved_at).toEqual(expect.any(Number));
    expect(approved?.approved_by_name).toBe('admin');
    for (const signer_role of ['PARTICIPANT', 'APPROVER']) {
      const response = await fetch(
        `https://tbm-test.invalid/api/reports/tbm/${approved!.report_id}/signatures`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ report_version: 1, signer_role }),
        },
      );
      expect(response.status).toBe(409);
      expect(await response.json()).toMatchObject({ code: 'REPORT_APPROVED' });
    }
    server.events.removeAllListeners('request:start');
  });

  it('결재한 버전은 인쇄본에 결재란을 넣고 보고서를 다시 작성한 새 버전은 다시 결재한다', async () => {
    mockSignatureCanvas();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const print = vi.spyOn(window, 'print').mockImplementation(() => {
      const printed = document.querySelector('.print\\:block');
      expect(printed?.textContent).toContain('관리자 결재');
      expect(printed?.textContent).toContain('결재 완료 · admin');
      expect(printed?.querySelector('img[alt="결재 실명 결재 서명"]')).toBeTruthy();
    });
    endWork();
    render(<TbmPage initialWorkId="1" />);
    const firstPanel = await openSignPanel();
    await within(firstPanel).findByText('참석자 서명 0명');
    fillSignature(firstPanel, '결재 실명');
    fireEvent.click(within(firstPanel).getByRole('button', { name: '관리자 결재' }));
    await within(firstPanel).findByText('결재 완료');
    const approved = tbmMock.reportsByWork.get('1')?.[0];
    const firstApprovedAt = approved?.approved_at;
    fireEvent.click(screen.getByRole('button', { name: '출력' }));
    await waitFor(() => expect(print).toHaveBeenCalledTimes(1));

    // 결재 뒤 보고서를 다시 작성하면 새 버전이 생기고 그 버전은 결재 전이다.
    fireEvent.click(screen.getByRole('button', { name: '보고서 재작성' }));
    await waitFor(() =>
      expect(
        (screen.getByRole('button', { name: '보고서 재작성' }) as HTMLButtonElement).disabled,
      ).toBe(true),
    );
    await act(async () => {
      vi.advanceTimersByTime(2200);
    });
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: '보고서 버전' }).textContent).toContain('v2'),
    );
    await waitFor(() =>
      expect(
        (screen.getByRole('button', { name: '보고서 재작성' }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
    const secondPanel = await openSignPanel();
    await within(secondPanel).findByText('참석자 서명 0명');
    expect(within(secondPanel).queryByText('결재 완료')).toBeNull();
    fillSignature(secondPanel, '두 번째 결재 실명');
    fireEvent.click(within(secondPanel).getByRole('button', { name: '관리자 결재' }));
    await within(secondPanel).findByText('결재 완료');
    expect(approved?.approved_at).toBe(firstApprovedAt);
    expect(tbmMock.reportsByWork.get('1')?.[1]?.approved_at).toEqual(expect.any(Number));
  });

  it('다른 화면에서 먼저 결재하면 409 안내와 갱신된 결재 상태를 표시한다', async () => {
    mockSignatureCanvas();
    server.use(
      http.post('*/api/reports/tbm/:reportId/signatures', ({ params }) => {
        const report = tbmMock.reportsByWork
          .get('1')
          ?.find((r) => r.report_id === Number(params.reportId));
        if (report) {
          report.approved_at = Date.now();
          report.approved_by_name = '다른 관리자';
        }
        return HttpResponse.json({ code: 'REPORT_APPROVED' }, { status: 409 });
      }),
    );
    endWork();
    render(<TbmPage initialWorkId="1" />);
    const panel = await openSignPanel();
    fillSignature(panel);
    fireEvent.click(within(panel).getByRole('button', { name: '서명 등록' }));
    await within(panel).findByText('이미 관리자 결재가 완료된 보고서입니다.');
    await within(panel).findByText('결재 완료');
    expect(panel.textContent).toContain('다른 관리자');
    expect(within(panel).queryByLabelText('서명 패드')).toBeNull();
  });
});
