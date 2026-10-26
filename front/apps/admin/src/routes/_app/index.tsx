import { MonitorPage } from '@/features/monitor/MonitorPage';
import { createFileRoute, useNavigate } from '@tanstack/react-router';

function MonitorRoute() {
  const navigate = useNavigate();
  return (
    <MonitorPage
      onEditZone={(zoneId) => void navigate({ to: '/zones', search: { zone: zoneId, edit: '1' } })}
    />
  );
}

export const Route = createFileRoute('/_app/')({
  component: MonitorRoute,
  staticData: { fullBleed: true, eventPanel: true },
});
