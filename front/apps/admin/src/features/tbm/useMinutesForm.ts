import { useTbmFormStore } from '@/features/tbm/tbm.store';
import type { TbmTask } from '@/features/tbm/tbmDemo';
import {
  hasEdits,
  tbmFill,
  type TbmEditsByTask,
  type TbmForm,
  type TbmFormValue,
} from '@/features/tbm/tbmForm';
import { useMemo } from 'react';

interface MinutesForm {
  /** 모든 작업의 고친 칸. */
  edits: TbmEditsByTask;
  /** 받은 내용 위에 이 작업의 고친 칸을 덮은 양식. */
  filled: TbmForm;
  canUndo: boolean;
  /** 칸 하나를 고친다(받은 값과 같아지면 고친 칸에서 뺀다). */
  save: (key: string, value: TbmFormValue) => void;
  /** 이 작업의 고친 칸을 모두 버린다. */
  undo: () => void;
}

/** 한 작업의 회의록 양식: 받은 내용과 고친 칸을 합치고, 고치기와 되돌리기를 저장소에 이어 준다. */
export function useMinutesForm(task: TbmTask): MinutesForm {
  const edits = useTbmFormStore((s) => s.edits);
  const setField = useTbmFormStore((s) => s.setField);
  const undo = useTbmFormStore((s) => s.undo);
  const base = useMemo(() => tbmFill(task), [task]);
  const taskEdits = edits[task.id];
  // 쪽 나눔은 양식 내용이 바뀔 때만 다시 하도록, 같은 내용이면 같은 객체를 쓴다.
  const filled = useMemo(() => ({ ...base, ...taskEdits }), [base, taskEdits]);
  return {
    edits,
    filled,
    canUndo: hasEdits(edits, task.id),
    save: (key, value) => setField(task.id, key, value, base[key] ?? null),
    undo: () => undo(task.id),
  };
}
