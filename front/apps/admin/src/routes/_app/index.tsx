import { MonitorPage } from '@/features/monitor/MonitorPage';
import { createFileRoute } from '@tanstack/react-router';

export const Route = createFileRoute('/_app/')({
  component: MonitorPage,
  staticData: { fullBleed: true },
});
