import { ZonesPage } from '@/features/zones/ZonesPage';
import { parseZonesSearch } from '@/features/zones/zonesSearch';
import { createFileRoute, useNavigate } from '@tanstack/react-router';

function ZonesRoute() {
  const { zone } = Route.useSearch();
  const navigate = useNavigate();
  return (
    <ZonesPage
      initialZoneId={zone ?? null}
      onSearchConsumed={() => void navigate({ to: '/zones', search: {}, replace: true })}
    />
  );
}

export const Route = createFileRoute('/_app/zones')({
  validateSearch: parseZonesSearch,
  component: ZonesRoute,
  staticData: { fullBleed: true, zoneEditor: true },
});
