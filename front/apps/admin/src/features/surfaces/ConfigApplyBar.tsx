import { APPLY_LABEL, publishErrorText, useConfigApply } from '@/features/zones/useConfigApply';
import { Button } from '@simai/ui/components/button';
import { useRef } from 'react';

/**
 * 현장 적용(명세 FR-17): 구성 묶음 버전과 Tier 0 적용 상태, 이 화면에서 발행하지 않은 저장 건수, 발행 버튼.
 * 발행은 구역·작업면을 함께 보낸다. 구역 화면(ZonesPage)의 같은 줄과 모양을 맞춘다.
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
  const latest = apply.latest;
  return (
    <div
      role="group"
      aria-label="현장 적용"
      className="flex items-center justify-between gap-2 rounded-[10px] bg-surface-2 px-2.5 py-1.5 text-xs"
    >
      {/* 적용 대기 → 현장 적용됨 변화를 화면 읽기 프로그램도 알린다. */}
      <span aria-live="polite" className="flex min-w-0 flex-wrap items-center gap-x-1.5">
        <span className="text-muted-foreground">구성</span>
        {latest ? (
          <>
            <span className="font-mono font-semibold text-foreground">{latest.config_version}</span>
            <span className="font-medium text-foreground">{APPLY_LABEL[latest.status]}</span>
            {/* 거부·불일치면 Tier 0 가 계속 쓰는 묶음을 함께 보인다. */}
            {latest.status !== 'PENDING' &&
              latest.status !== 'APPLIED' &&
              latest.last_ack?.active_config_version && (
                <span className="text-muted-foreground">
                  · 현장 {latest.last_ack.active_config_version}
                </span>
              )}
          </>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
        {unpublishedCount > 0 && (
          <span className="text-muted-foreground">· 발행 전 {unpublishedCount}건</span>
        )}
      </span>
      {canEdit && (
        <Button
          size="sm"
          variant="secondary"
          disabled={apply.publish.isPending || !apply.canPublish}
          onClick={handlePublish}
          className="shrink-0 whitespace-nowrap"
        >
          현장에 적용
        </Button>
      )}
    </div>
  );
}
