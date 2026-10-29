import { BUNDLES_KEY, publishBundle, requestBundles, ZONES_KEY } from '@/features/zones/api';
import {
  configBundleSchema,
  type BundleStatus,
  type ConfigBundle,
  type Zone,
} from '@/features/zones/types';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo } from 'react';

/** 적용 대기 동안 적용 상태를 다시 받는 간격(ms). 서버는 Tier 0 의 config_ack 를 받으면 상태를 바꾼다. */
const PENDING_POLL_MS = 3_000;

/**
 * 현장 반영 줄 문구(명세 FR-17). 저장한 구역·작업면은 구성 묶음으로 발행해야 현장 판정(Tier 0)에 가고, 상태는 서버가
 * Tier 0 의 config_ack 로 정한다. 묶음 번호 대신 쉬운 말로 보인다. unpublished 는 발행하지 않은 저장 건수다.
 */
export function applySummary(latest: ConfigBundle | null | undefined, unpublished: number): string {
  // 묶음 목록을 받는 중이거나 받지 못했으면 반영 여부를 모른다(묶음이 하나도 없으면 null).
  if (latest === undefined) return '—';
  const status: BundleStatus | undefined = latest?.status;
  if (status === 'PENDING') return '현장에 보내는 중';
  if (status === 'REJECTED') return '현장이 거부함';
  if (status === 'MISMATCH') return '현장 반영 확인 안 됨';
  if (unpublished > 0) return `현장 미반영 ${unpublished}건`;
  return latest ? '현장 반영됨' : '현장 미반영';
}

export function publishErrorText(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) return '현장에 반영할 권한이 없습니다.';
  if (error instanceof ApiError && error.code === 'CALIBRATION_MISSING')
    return '보정 문서가 없어 현장에 반영할 수 없습니다.';
  if (error instanceof ApiError && error.code === 'RULES_MISSING')
    return '유효한 규칙 버전이 없어 현장에 반영할 수 없습니다.';
  if (error instanceof ApiError && error.code === 'PUBLIC_URL_NOT_SET')
    return '서버 공개 주소가 설정되지 않아 현장에 반영할 수 없습니다.';
  return '현장에 반영하지 못했습니다. 다시 시도하세요.';
}

/** 받은 묶음 목록(새것부터)에서 형식이 맞는 것만 고른다. */
export function readBundles(items: readonly unknown[]): ConfigBundle[] {
  return items.flatMap((item) => {
    const parsed = configBundleSchema.safeParse(item);
    return parsed.success ? [parsed.data] : [];
  });
}

/**
 * 마지막 묶음보다 늦게 저장한(이 버전의 valid_from) 구역 ID(발행 전). 서버는 발행할 때 그때 유효한 버전을 묶는다.
 * 묶음이 하나도 없으면(null) 모든 구역이 발행 전이고, 목록을 아직 모르면(undefined) null 이다.
 */
export function unpublishedZoneIds(
  zones: readonly Pick<Zone, 'zoneId' | 'validFrom'>[],
  latest: ConfigBundle | null | undefined,
): Set<string> | null {
  return unpublishedOf(
    zones.map((z) => ({ id: z.zoneId, validFrom: z.validFrom })),
    latest,
  );
}

/** 발행 전 판단의 공통 규칙(구역·작업면). 묶음이 없으면 모두, 목록을 모르면 null. */
function unpublishedOf(
  items: readonly { id: string; validFrom: number }[],
  latest: ConfigBundle | null | undefined,
): Set<string> | null {
  if (latest === undefined) return null;
  return new Set(
    items.filter((i) => latest === null || i.validFrom > latest.issued_at).map((i) => i.id),
  );
}

/** 발행 전 판단에 쓰는 작업면 칸(features/surfaces 의 WorkSurface). */
export interface PublishableSurface {
  objectId: string;
  validFrom: number;
}

const versionOf = (item: unknown) =>
  typeof item === 'object' && item !== null ? Reflect.get(item, 'config_version') : undefined;

/**
 * 현장 적용 상태와 발행. 상태는 최근 묶음 목록(GET /api/config-bundles)의 첫 묶음으로 보이고, 적용 대기 동안 다시 받는다.
 * 발행 응답(새 묶음, 적용 대기)은 목록 맨 앞에 넣는다(응답 뒤에 그린다, 낙관적 갱신 아님).
 * surfaces 를 주면 작업면도 발행 전을 따로 센다(발행은 구역·작업면을 함께 보낸다). 모르면(undefined) 0 으로 보아 발행을 막지
 * 않는다. 작업면 목록은 이 훅이 받지 않는다(작업면 화면이 받는다).
 */
export function useConfigApply(zones: readonly Zone[], surfaces?: readonly PublishableSurface[]) {
  const queryClient = useQueryClient();
  // 구역 목록은 로그인마다 한 번만 받는다. 다른 관리자가 저장한 새 버전도 발행 전으로 보이게 화면에 들어올 때 다시 받는다
  // (이미 받는 중이면 그 응답을 쓴다).
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: ZONES_KEY }, { cancelRefetch: false });
  }, [queryClient]);
  const query = useQuery({
    queryKey: BUNDLES_KEY,
    queryFn: ({ signal }) => requestBundles(api, signal),
    select: readBundles,
    refetchInterval: (q) =>
      readBundles(q.state.data ?? [])[0]?.status === 'PENDING' ? PENDING_POLL_MS : false,
  });
  const latest = query.data ? (query.data[0] ?? null) : undefined;
  const publish = useMutation({
    mutationKey: ['config-bundles', 'publish'],
    mutationFn: () => publishBundle(api),
    onSuccess: async (bundle) => {
      // 발행 전에 시작한 목록 받기는 새 묶음이 없는 옛 목록을 줄 수 있어 취소한다(적용 대기라 곧 다시 받는다).
      await queryClient.cancelQueries({ queryKey: BUNDLES_KEY });
      queryClient.setQueryData<unknown[]>(BUNDLES_KEY, (prev) => [
        bundle,
        ...(prev ?? []).filter((item) => versionOf(item) !== bundle.config_version),
      ]);
    },
  });
  const unpublished = useMemo(() => unpublishedZoneIds(zones, latest), [zones, latest]);
  const unpublishedSurfaces = useMemo(
    () =>
      surfaces === undefined
        ? null
        : unpublishedOf(
            surfaces.map((s) => ({ id: s.objectId, validFrom: s.validFrom })),
            latest,
          ),
    [surfaces, latest],
  );
  // 발행 전 구역·작업면이 있거나, 마지막 묶음이 거부·불일치라 현장이 이전 묶음을 쓰면 (다시) 발행할 수 있다.
  // 묶음 목록을 모르면 막지 않는다.
  const canPublish =
    unpublished === null ||
    unpublished.size > 0 ||
    (unpublishedSurfaces?.size ?? 0) > 0 ||
    latest?.status === 'REJECTED' ||
    latest?.status === 'MISMATCH';
  return { latest, unpublished, unpublishedSurfaces, canPublish, publish };
}
