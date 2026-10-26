import { z } from 'zod';

const zoneIdSchema = z
  .string()
  .trim()
  .min(1)
  .max(64)
  .regex(/^[A-Za-z0-9_-]+$/);

export interface ZonesSearch {
  zone?: string;
  edit?: '1';
}

/** /zones?zone=z-04&edit=1 쿼리를 검증한다. 형식이 틀린 값은 버린다. */
export function parseZonesSearch(raw: Record<string, unknown>): ZonesSearch {
  const zone = zoneIdSchema.safeParse(raw.zone);
  const edit = raw.edit === '1' || raw.edit === 1 || raw.edit === true;
  return {
    ...(zone.success ? { zone: zone.data } : {}),
    ...(zone.success && edit ? { edit: '1' as const } : {}),
  };
}
