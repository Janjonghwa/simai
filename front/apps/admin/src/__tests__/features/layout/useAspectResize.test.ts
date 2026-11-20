import {
  collectBottomPanelObstacles,
  defaultMinimapSize,
  maxBottomPanelHeight,
  maxWidthAgainstObstacles,
  widthFromDrag,
} from '@/features/layout/useAspectResize';
import { describe, expect, it } from 'vitest';

describe('useAspectResize 경계·비율 계산', () => {
  it('미니맵 기본 크기는 260px 과 화면 높이 34% 중 작은 값이다', () => {
    expect(defaultMinimapSize(900)).toBe(260);
    expect(defaultMinimapSize(674)).toBe(229);
    expect(defaultMinimapSize(600)).toBe(204);
  });

  it('최대 높이는 화면 짧은 변의 절반이다', () => {
    expect(maxBottomPanelHeight({ width: 1440, height: 900 })).toBe(450);
    expect(maxBottomPanelHeight({ width: 1288, height: 674 })).toBe(337);
    expect(maxBottomPanelHeight({ width: 640, height: 900 })).toBe(320);
  });

  it('오른쪽 위(미니맵)·왼쪽 위(CCTV) 모서리 끌기를 비율 고정 폭으로 변환한다', () => {
    // 미니맵(1:1): 오른쪽(+dx)이나 위(-dy)로 끌면 커진다.
    expect(widthFromDrag(260, 100, -100, 'top-right', 1)).toBe(360);
    expect(widthFromDrag(260, 80, 0, 'top-right', 1)).toBe(340);
    expect(widthFromDrag(260, 0, -90, 'top-right', 1)).toBe(350);
    expect(widthFromDrag(360, -40, 40, 'top-right', 1)).toBe(320);

    // CCTV(16:9): 왼쪽(-dx)이나 위(-dy)로 끌면 커진다.
    expect(widthFromDrag(462, -160, -90, 'top-left', 16 / 9)).toBe(622);
    expect(widthFromDrag(462, -120, 0, 'top-left', 16 / 9)).toBe(582);
    expect(widthFromDrag(462, 0, -90, 'top-left', 16 / 9)).toBe(622);
    expect(widthFromDrag(622, 80, 45, 'top-left', 16 / 9)).toBe(542);
  });

  it('두 창이 서로 겹치지 않게 상대 창 앞에서 최대 폭을 막는다', () => {
    // 1280×900 에서 CCTV 가 폭 800(left=466) 으로 열려 있으면 미니맵(left=14)은 466 - 14 - 14 = 438 까지다.
    const bounds = { width: 1280, height: 900 };
    const cctv = { left: 466, top: 436, width: 800, height: 450 };
    expect(
      maxWidthAgainstObstacles({
        corner: 'top-right',
        anchor: { x: 14, bottom: 886 },
        aspect: 1,
        bounds,
        obstacles: [cctv],
      }),
    ).toBe(438);

    // 반대로 미니맵이 450(left=14..464) 이면 CCTV(right=1266)는 1266 - 464 - 14 = 788 까지다.
    const minimap = { left: 14, top: 436, width: 450, height: 450 };
    expect(
      maxWidthAgainstObstacles({
        corner: 'top-left',
        anchor: { x: 1266, bottom: 886 },
        aspect: 16 / 9,
        bounds,
        obstacles: [minimap],
      }),
    ).toBe(788);
  });

  it('작업 표시줄이나 이벤트 패널 아래에 있으면 그 아래 틈까지만 커진다', () => {
    // CCTV 를 화면 위쪽 근처(bottom=450)로 옮긴 상태에서 작업 표시줄(top=72, height=100 → bottom=172)이 위에 있으면
    // 최대 높이는 450 - (172 + 14) = 264, 16:9 폭은 469px 로 제한된다.
    const workBar = { left: 400, top: 72, width: 500, height: 100 };
    expect(
      maxWidthAgainstObstacles({
        corner: 'top-left',
        anchor: { x: 1000, bottom: 450 },
        aspect: 16 / 9,
        bounds: { width: 1440, height: 900 },
        obstacles: [workBar],
      }),
    ).toBe(Math.floor(264 * (16 / 9)));

    // 이벤트 패널이 왼쪽 아래 미니맵 위로 옮겨진 경우(top=72, height=420 → bottom=492) 미니맵은 886 - 506 = 380 까지다.
    const movedEventPanel = { left: 14, top: 72, width: 340, height: 420 };
    expect(
      maxWidthAgainstObstacles({
        corner: 'top-right',
        anchor: { x: 14, bottom: 886 },
        aspect: 1,
        bounds: { width: 1440, height: 900 },
        obstacles: [movedEventPanel],
      }),
    ).toBe(380);
  });

  it('collectBottomPanelObstacles 는 CCTV 기준으로 이벤트 패널의 최소 높이 구간만 장애물로 잡는다', () => {
    const root = document.createElement('div');
    root.className = 'h-svh';
    const main = document.createElement('main');
    const cctv = document.createElement('section');
    cctv.setAttribute('aria-label', '떠 있는 CCTV');
    const eventPanel = document.createElement('aside');
    eventPanel.setAttribute('aria-label', '이벤트');
    const notices = document.createElement('div');
    notices.setAttribute('role', 'region');
    notices.setAttribute('aria-label', '알림');
    const sessionBar = document.createElement('section');
    sessionBar.setAttribute('aria-label', '작업 세션');
    notices.appendChild(sessionBar);
    main.append(cctv, eventPanel);
    root.append(main, notices);

    const mockRect = (el: Element, left: number, top: number, width: number, height: number) => {
      el.getBoundingClientRect = () => ({ left, top, width, height }) as DOMRect;
    };
    mockRect(main, 0, 0, 1440, 900);
    mockRect(cctv, 964, 626, 462, 260);
    mockRect(eventPanel, 1086, 72, 340, 414);
    mockRect(sessionBar, 520, 72, 400, 44);

    // CCTV 기준: 이벤트 패널은 자동 축소 최소 높이(120px)만 막고, 작업 세션 바는 전체 크기를 막는다.
    const forCctv = collectBottomPanelObstacles(main, cctv, { shrinkableEventPanel: true });
    expect(forCctv).toContainEqual({ left: 1086, top: 72, width: 340, height: 120 });
    expect(forCctv).toContainEqual({ left: 520, top: 72, width: 400, height: 44 });

    // 미니맵 기준: 이벤트 패널 전체 높이를 장애물로 본다.
    const forMinimap = collectBottomPanelObstacles(main, null);
    expect(forMinimap).toContainEqual({ left: 1086, top: 72, width: 340, height: 414 });
    expect(forMinimap).toContainEqual({ left: 964, top: 626, width: 462, height: 260 });
  });
});
