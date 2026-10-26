import { ZonesPage } from '@/features/zones/ZonesPage';
import { parseZonesSearch } from '@/features/zones/zonesSearch';
import { createFileRoute, useNavigate } from '@tanstack/react-router';

function ZonesRoute() {
  const { zone, edit } = Route.useSearch();
  const navigate = useNavigate();
  return (
    <ZonesPage
      initialZoneId={zone ?? null}
      autoStartEdit={edit === '1'}
      onSearchConsumed={() => void navigate({ to: '/zones', search: {}, replace: true })}
    />
  );
}

export const Route = createFileRoute('/_app/zones')({
  validateSearch: parseZonesSearch,
  component: ZonesRoute,
  staticData: { fullBleed: true, eventPanel: true, zoneEditor: true },
});
