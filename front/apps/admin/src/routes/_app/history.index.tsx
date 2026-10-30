import { HistoryListPage } from '@/features/history/HistoryListPage';
import { createFileRoute } from '@tanstack/react-router';

export const Route = createFileRoute('/_app/history/')({
  component: HistoryListPage,
});
