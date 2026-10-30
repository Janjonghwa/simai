import { ModalDialog } from '@/features/tbm/ModalDialog';
import { ACTION, H2, SECONDARY } from '@/features/tbm/styles';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { useRef, type ReactNode } from 'react';

// 큰 편집 창: 종이 하나에서 모든 칸을 고친다. 고칠 때마다 저장되므로(tbm.store) 저장 단추는 없다.

export function TbmEditDialog({
  onClose,
  onUndo,
  canUndo,
  children,
}: {
  onClose: () => void;
  onUndo: () => void;
  canUndo: boolean;
  children: ReactNode;
}) {
  const done = useRef<HTMLButtonElement>(null);
  return (
    <ModalDialog
      label="TBM 회의록 편집"
      onClose={onClose}
      className="h-[90vh] w-[min(1100px,calc(100vw-48px))] overflow-hidden open:flex open:flex-col"
    >
      <header className="flex flex-none items-center gap-2.5 border-b border-border px-5 py-3">
        <h2 className={H2}>TBM 회의록 편집</h2>
        <span className="ml-auto" />
        <Button
          variant="secondary"
          disabled={!canUndo}
          onClick={() => {
            onUndo();
            // 되돌리기가 꺼지므로 포커스를 편집 완료로 옮긴다.
            done.current?.focus();
          }}
          className={SECONDARY}
        >
          되돌리기
        </Button>
        <Button ref={done} onClick={onClose} className={cn(ACTION, 'font-bold')}>
          편집 완료
        </Button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto bg-surface-2 p-5">{children}</div>
    </ModalDialog>
  );
}
