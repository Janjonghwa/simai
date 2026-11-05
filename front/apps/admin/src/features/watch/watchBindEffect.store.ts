import type { NewBind } from '@/features/watch/watchStatus';
import { BIND_METHOD_TEXT } from '@/features/watch/watchStatus';
import { create } from 'zustand';

/** 3D 강조 시간(ms). 관객이 손 흔든 사람을 눈으로 찾아가기에 충분한 4초(이름표 한 번 읽고 시선이 가는 시간) */
export const HIGHLIGHT_MS = 4_000;
/** 패널 알림 시간(ms). 3D 강조보다 조금 더 길게 둔다 */
export const NOTICE_MS = 6_000;

export interface BindNotice {
  id: number;
  text: string;
}

interface State {
  /** World ID → 3D 큰 이름표 글. 강조 시간 동안만 있다 */
  highlights: Readonly<Record<string, string>>;
  notice: BindNotice | null;
  announce: (bind: NewBind) => void;
  clear: () => void;
}

let seq = 0;
// 사람마다 가장 최근 알림 번호. 같은 사람에게 다시 이어지면 앞선 타이머가 새 강조를 일찍 지우지 못하게 한다
const latest = new Map<string, number>();
const timers = new Set<ReturnType<typeof setTimeout>>();

/** 새로 이어진 워치의 3D 강조·패널 알림. 시간이 지나면 스스로 지운다(관제 화면 한 곳이 감지해 알린다) */
export const useWatchBindEffectStore = create<State>()((set, get) => ({
  highlights: {},
  notice: null,
  announce: ({ deviceId, worldId, method }) => {
    const id = ++seq;
    const text = `워치 ${deviceId} 연결됨`;
    latest.set(worldId, id);
    set({
      highlights: { ...get().highlights, [worldId]: text },
      notice: {
        id,
        text: `${deviceId} → ${worldId} 연결됨(${BIND_METHOD_TEXT[method]})`,
      },
    });
    const drop = setTimeout(() => {
      timers.delete(drop);
      if (latest.get(worldId) === id) {
        latest.delete(worldId);
        const rest = { ...get().highlights };
        delete rest[worldId];
        set({ highlights: rest });
      }
    }, HIGHLIGHT_MS);
    const hide = setTimeout(() => {
      timers.delete(hide);
      if (get().notice?.id === id) set({ notice: null });
    }, NOTICE_MS);
    timers.add(drop);
    timers.add(hide);
  },
  clear: () => {
    for (const t of timers) clearTimeout(t);
    timers.clear();
    latest.clear();
    set({ highlights: {}, notice: null });
  },
}));
