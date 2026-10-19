import { TbmPage } from '@/features/tbm/TbmPage';
import { createFileRoute } from '@tanstack/react-router';

export const Route = createFileRoute('/_app/tbm')({
  component: TbmPage,
});
