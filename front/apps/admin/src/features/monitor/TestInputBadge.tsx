import { TEST_INPUT_LABEL, TEST_INPUT_TITLE, useTestInput } from '@/features/monitor/useTestInput';

/** 작업 세션 바의 재생 중 표시(S15P31A101-731). 재생 중이 아니거나 모르면 그리지 않는다. */
export function TestInputBadge() {
  const testInput = useTestInput();
  if (!testInput) return null;
  return (
    <span
      role="status"
      title={TEST_INPUT_TITLE}
      className="shrink-0 rounded-full px-2.5 py-0.5 text-sm font-semibold whitespace-nowrap text-muted-foreground ring-1 ring-border ring-inset"
    >
      {TEST_INPUT_LABEL}
    </span>
  );
}
