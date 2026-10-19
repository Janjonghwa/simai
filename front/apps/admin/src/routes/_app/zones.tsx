import { ZonesPage } from '@/features/zones/ZonesPage';
import { createFileRoute } from '@tanstack/react-router';

export const Route = createFileRoute('/_app/zones')({
  component: ZonesPage,
});
