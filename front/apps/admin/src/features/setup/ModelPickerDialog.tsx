import { formatRemainingKorean, SETUP_TARGET_DURATION_SEC } from '@/features/setup/setup';
import { ModalDialog } from '@/features/tbm/ModalDialog';
import { H2 } from '@/features/tbm/styles';
import { useEffect, useRef } from 'react';

// 새 세팅 세션을 만들기 직전에 뜨는 방식 고르기 창. 고르기 전에는 세션을 만들지 않으므로 닫을 수 없다(Esc·바깥 클릭 모두).
// 이전 모델 쓰기는 서버가 아직 받지 못해 늘 막혀 있다.

const NEW_ETA = `예상 ${formatRemainingKorean(SETUP_TARGET_DURATION_SEC)}`;
const REUSE_UNAVAILABLE = '지금은 쓸 수 없어요';

/** ModalDialog 는 Esc 를 막고 onClose 만 부른다. 아무것도 안 하면 창이 열린 채 남는다. */
const KEEP_OPEN = () => {};

const CHOICE =
  'flex min-h-24 w-full flex-col items-start justify-center gap-2.5 rounded-xl border border-border bg-card px-5 py-[18px] text-left outline-none enabled:hover:bg-surface-2 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50';
const CHOICE_TITLE = 'text-lg font-bold';
const CHOICE_NOTE = 'text-[13px] text-muted-foreground';

export function ModelPickerDialog({ onNew }: { onNew: () => void }) {
  const newButton = useRef<HTMLButtonElement>(null);
  // ModalDialog 가 창을 연 다음(자식 효과가 먼저 돈다) 첫 포커스를 새로 만들기에 둔다.
  useEffect(() => {
    newButton.current?.focus();
  }, []);
  return (
    <ModalDialog label="3D 공간" onClose={KEEP_OPEN} className="w-[min(520px,calc(100vw-48px))]">
      <div className="flex flex-col gap-3 p-5">
        <h2 className={H2}>3D 공간</h2>
        <button ref={newButton} type="button" className={CHOICE} onClick={() => onNew()}>
          <span className={CHOICE_TITLE}>새로 3D 공간 만들기</span>
          <span className={`${CHOICE_NOTE} tabular-nums`}>{NEW_ETA}</span>
        </button>
        <button type="button" disabled title={REUSE_UNAVAILABLE} className={CHOICE}>
          <span className={CHOICE_TITLE}>이전 모델 쓰기</span>
          <span className={CHOICE_NOTE}>{REUSE_UNAVAILABLE}</span>
        </button>
      </div>
    </ModalDialog>
  );
}
