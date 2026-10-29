import { SetupPage } from '@/features/setup/SetupPage';
import { createFileRoute } from '@tanstack/react-router';

export const Route = createFileRoute('/_app/setup')({
  component: SetupPage,
});
