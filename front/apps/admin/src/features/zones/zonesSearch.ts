import { z } from 'zod';

const zoneIdSchema = z
  .string()
  .trim()
  .min(1)
  .max(64)
  .regex(/^[A-Za-z0-9_-]+$/);

interface ZonesSearch {
  zone?: string;
}

/** /zones?zone=z-04 쿼리를 검증한다. 형식이 틀린 값은 버린다. */
export function parseZonesSearch(raw: Record<string, unknown>): ZonesSearch {
  const zone = zoneIdSchema.safeParse(raw.zone);
  return zone.success ? { zone: zone.data } : {};
}
