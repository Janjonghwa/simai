import { useEventsStore } from '@/features/events/events.store';
import { fitPanel, type PanelBox } from '@/features/events/panelBox';
import { measure } from '@/features/events/panelMeasure';
import { useEffect, useLayoutEffect, useState, type RefObject } from 'react';

/**
 * 옮기거나 크기를 바꾼 패널의 자리. null 이면 오른쪽 기본 자리다.
 * - 옮긴 패널의 가로 자리를 알린다(상단 알림이 비킨다). 기본 자리로 돌아가거나 패널이 사라지면 지운다.
 * - 창 크기가 바뀌면 화면 밖으로 나가지 않게 다시 맞춘다.
 */
export function useFloatingBox(panel: RefObject<HTMLElement | null>) {
  const [box, setBox] = useState<PanelBox | null>(null);
  const setPanelSpan = useEventsStore((s) => s.setPanelSpan);

  useLayoutEffect(() => {
    const now = box && measure(panel.current);
    setPanelSpan(
      box && now
        ? { left: box.left, width: box.width, right: now.bounds.width - box.left - box.width }
        : null,
    );
  }, [box, panel, setPanelSpan]);
  useEffect(() => () => setPanelSpan(null), [setPanelSpan]);

  const moved = box !== null;
  useEffect(() => {
    if (!moved) return undefined;
    const refit = () => {
      const now = measure(panel.current);
      if (now) setBox((prev) => (prev ? fitPanel(prev, now.bounds) : prev));
    };
    window.addEventListener('resize', refit);
    return () => window.removeEventListener('resize', refit);
  }, [moved, panel]);

  return { box, setBox, reset: () => setBox(null) };
}
