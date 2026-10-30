import type { TbmTask } from '@/features/tbm/tbmDemo';
import type { PrintItem } from '@/features/tbm/TbmDocument';
import { formOf, type TbmEditsByTask } from '@/features/tbm/tbmForm';
import { useMemo, useState } from 'react';
import { flushSync } from 'react-dom';

/** 인쇄 창을 여는 동안만 출력용 사본을 그린다: 지금 작업 하나 또는 하루치 전부. */
export function usePrint(tasks: readonly TbmTask[], task: TbmTask, edits: TbmEditsByTask) {
  const [scope, setScope] = useState<'one' | 'all' | null>(null);
  const items = useMemo<PrintItem[]>(() => {
    if (scope === null) return [];
    const targets = scope === 'all' ? tasks : [task];
    return targets.map((t) => ({ id: t.id, form: formOf(t, edits), people: t.people }));
  }, [scope, tasks, task, edits]);

  /** 출력용 사본(하루치면 작업마다 쪽을 나눠 이어서)을 먼저 그린 뒤 인쇄 창을 연다. */
  const print = (whole: boolean, beforePrint: () => void) => {
    flushSync(() => {
      setScope(whole ? 'all' : 'one');
      beforePrint();
    });
    window.print();
    setScope(null);
  };

  return { printing: scope !== null, items, print };
}
