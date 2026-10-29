import { applySummary, publishErrorText, useConfigApply } from '@/features/zones/useConfigApply';
import { Button } from '@simai/ui/components/button';
import { useRef } from 'react';

/**
 * 현장 반영 줄(명세 FR-17): 저장한 구역·작업면이 현장 판정(Tier 0)에 반영됐는지와 반영 버튼(구성 묶음 발행).
 * 발행은 구역·작업면을 함께 보낸다. 구역 화면(ZonesPage)과 작업면 화면이 같이 쓴다.
 */
export function ConfigApplyBar({
  apply,
  unpublishedCount,
  canEdit,
  onError,
}: {
  apply: ReturnType<typeof useConfigApply>;
  unpublishedCount: number;
  canEdit: boolean;
  onError: (text: string) => void;
}) {
  // isPending 은 다음 턴에 바뀌어, 같은 턴에 두 번 눌리면 두 번 보낸다. 보내는 중이면 여기서 바로 막는다.
  const sending = useRef(false);
  const handlePublish = () => {
    if (sending.current) return;
    sending.current = true;
    apply.publish.mutate(undefined, {
      onSettled: () => {
        sending.current = false;
      },
      onError: (error) => onError(publishErrorText(error)),
    });
  };
  return (
    <div
      role="group"
      aria-label="현장 반영"
      className="flex items-center justify-between gap-2 rounded-[10px] bg-surface-2 px-2.5 py-1.5 text-xs"
    >
      {/* 보내는 중 → 반영됨 변화를 화면 읽기 프로그램도 알린다. */}
      <span aria-live="polite" className="min-w-0 font-medium text-foreground">
        {applySummary(apply.latest, unpublishedCount)}
      </span>
      {canEdit && (
        <Button
          size="sm"
          variant="secondary"
          disabled={apply.publish.isPending || !apply.canPublish}
          onClick={handlePublish}
          className="shrink-0 whitespace-nowrap"
        >
          현장에 반영
        </Button>
      )}
    </div>
  );
}
