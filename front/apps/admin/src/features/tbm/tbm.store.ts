import {
  parseEdits,
  withEdit,
  withoutEdits,
  type TbmEditsByTask,
  type TbmFormValue,
} from '@/features/tbm/tbmForm';
import { create } from 'zustand';
import { persist, type PersistStorage, type StorageValue } from 'zustand/middleware';

// TBM 회의록에서 작업마다 고친 칸. 입력할 때마다 브라우저에 남겨 새로고침해도 이어 쓴다.

export const TBM_FORM_KEY = 'simai.tbm.form';

interface TbmFormState {
  edits: TbmEditsByTask;
  /** 칸 하나를 고친다. original 은 받은 값이다(같아지면 고친 칸에서 뺀다). */
  setField: (taskId: string, key: string, value: TbmFormValue, original: TbmFormValue) => void;
  /** 그 작업의 고친 칸을 모두 버린다. */
  undo: (taskId: string) => void;
}

type Saved = Pick<TbmFormState, 'edits'>;

// 저장할 수 없는 환경(사생활 보호 창, 용량 초과)이면 메모리에만 둔다.
const storage: PersistStorage<Saved> = {
  getItem: (name) => {
    try {
      const raw = localStorage.getItem(name);
      return raw ? (JSON.parse(raw) as StorageValue<Saved>) : null;
    } catch {
      return null;
    }
  },
  setItem: (name, value) => {
    try {
      localStorage.setItem(name, JSON.stringify(value));
    } catch {
      // 메모리에만 둔다.
    }
  },
  removeItem: (name) => {
    try {
      localStorage.removeItem(name);
    } catch {
      // 지우지 못해도 화면 상태는 그대로다.
    }
  },
};

const savedEdits = (saved: unknown) =>
  parseEdits(typeof saved === 'object' && saved !== null ? (saved as Saved).edits : undefined);

export const useTbmFormStore = create<TbmFormState>()(
  persist(
    (set) => ({
      edits: {},
      setField: (taskId, key, value, original) =>
        set((s) => ({ edits: withEdit(s.edits, taskId, key, value, original) })),
      undo: (taskId) => set((s) => ({ edits: withoutEdits(s.edits, taskId) })),
    }),
    {
      name: TBM_FORM_KEY,
      storage,
      partialize: (s) => ({ edits: s.edits }),
      // 남은 값은 형식을 검사해 틀린 칸을 버린다.
      merge: (saved, current) => ({ ...current, edits: savedEdits(saved) }),
    },
  ),
);
