import { SystemPage } from '@/features/system/SystemPage';
import { createFileRoute } from '@tanstack/react-router';

export const Route = createFileRoute('/_app/system')({
  component: SystemPage,
});
