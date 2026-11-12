import { ConfirmDialog } from '@/features/work/ConfirmDialog';
import {
  canStart,
  withCheck,
  type PreflightCheckKey,
  type PreflightChecks,
} from '@/features/work/preflight/preflight';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';
import { Check } from 'lucide-react';

// '작업 전 확인' 창: 보고서·가상 레이아웃을 확인했는지 체크하고, 참석자 서명이 1명 이상이면 '작업 시작'이 켜진다.
// 서명이 없으면 이 창을 닫고 서명 창을 여는 '서명하기'가 있다. 시작이 거절되면 이유를 창 안에 보인다.

const CHECK_ROWS: readonly { key: PreflightCheckKey; label: string }[] = [
  { key: 'reportChecked', label: 'TBM 보고서를 확인했습니다' },
  { key: 'layoutChecked', label: '가상 레이아웃을 확인했습니다' },
];

export function PreflightConfirmDialog({
  checks,
  onChecksChange,
  signerCount,
  pending,
  error,
  draftWarning = null,
  onSign,
  onStart,
  onCancel,
}: {
  checks: PreflightChecks;
  onChecksChange: (next: PreflightChecks) => void;
  signerCount: number;
  /** 작업 시작 요청이 가는 중. */
  pending: boolean;
  /** 시작이 거절됐을 때 보일 글. */
  error: string | null;
  /** AI 초안이 끝나지 않았을 때의 경고(-625). 시작을 막지 않는다. */
  draftWarning?: string | null;
  /** '서명하기': 이 창을 닫고 서명 창을 연다. */
  onSign: () => void;
  onStart: () => void;
  onCancel: () => void;
}) {
  const signed = signerCount > 0;
  return (
    <ConfirmDialog
      title="작업 전 확인"
      confirmLabel="작업 시작"
      confirmDisabled={!canStart({ ...checks, signerCount })}
      pending={pending}
      onConfirm={onStart}
      onCancel={onCancel}
    >
      <div className="flex flex-col gap-3">
        {CHECK_ROWS.map(({ key, label }) => (
          <label key={key} className="flex cursor-pointer items-center gap-2.5 text-sm select-none">
            <input
              type="checkbox"
              checked={checks[key]}
              onChange={(event) => onChecksChange(withCheck(checks, key, event.target.checked))}
              className="size-[18px] cursor-pointer accent-primary"
            />
            <span>{label}</span>
          </label>
        ))}
        <div className="flex items-center gap-2.5 text-sm">
          <span
            aria-hidden
            className={cn(
              'grid size-[18px] flex-none place-items-center rounded-[3px] border-2',
              signed
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-muted-foreground',
            )}
          >
            {signed && <Check className="size-3" strokeWidth={3} />}
          </span>
          {signed ? (
            <span>{`참석자 서명 ${signerCount}명`}</span>
          ) : (
            <>
              <span>참석자 서명이 필요합니다</span>
              <Button variant="secondary" size="sm" onClick={onSign}>
                서명하기
              </Button>
            </>
          )}
        </div>
        {draftWarning && <p className="text-[13px] text-sev-caution">{draftWarning}</p>}
        {error && (
          <p role="alert" className="text-[13px] text-destructive">
            {error}
          </p>
        )}
      </div>
    </ConfirmDialog>
  );
}
