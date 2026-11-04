import { MonitorPage } from '@/features/monitor/MonitorPage';
import { useEditEntry } from '@/features/monitor/useEditEntry';
import { createFileRoute, useNavigate } from '@tanstack/react-router';
import { useCallback } from 'react';

export const Route = createFileRoute('/_app/')({
  validateSearch: (search: Record<string, unknown>): { edit?: boolean } => ({
    edit: search.edit === true || search.edit === 'true' ? true : undefined,
  }),
  component: MonitorRoute,
  staticData: { fullBleed: true, eventPanel: true },
});

function MonitorRoute() {
  const { edit } = Route.useSearch();
  const navigate = useNavigate();
  // 편집 모드 요청은 한 번만 받는다: 켠 뒤 주소에서 지워 새로고침해도 다시 켜지지 않는다.
  const clearEdit = useCallback(
    () => void navigate({ to: '/', search: {}, replace: true }),
    [navigate],
  );
  useEditEntry(edit, clearEdit);
  return <MonitorPage />;
}
