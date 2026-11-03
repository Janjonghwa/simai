import { MonitorPage } from '@/features/monitor/MonitorPage';
import { createFileRoute } from '@tanstack/react-router';

export const Route = createFileRoute('/_app/')({
  validateSearch: (search: Record<string, unknown>): { edit?: boolean } => ({
    edit: search.edit === true || search.edit === 'true' ? true : undefined,
  }),
  component: MonitorRoute,
  staticData: { fullBleed: true, eventPanel: true },
});

function MonitorRoute() {
  const { edit } = Route.useSearch();
  return <MonitorPage initialEdit={edit} />;
}
