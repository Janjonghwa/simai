import {
  bundlesResponseSchema,
  configBundleSchema,
  zoneItemSchema,
  zonesResponseSchema,
  type ZoneBody,
} from '@/features/zones/types';
import type { Http } from '@/lib/http';

// 구역 목록 조회 키. 화면(useZones)과 저장(useZoneSave)이 함께 쓴다.
export const ZONES_KEY = ['zones'] as const;

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.
export function requestZones(http: Http, signal?: AbortSignal) {
  return http('/api/zones', { schema: zonesResponseSchema, signal });
}

/** 새 구역(v1). ID 는 서버가 정한다. */
export function createZone(http: Http, body: ZoneBody) {
  return http('/api/zones', { method: 'POST', body, schema: zoneItemSchema });
}

/**
 * 구역의 새 버전(명세 FR-16, 덮어쓰지 않는다). 서버는 지금 버전을 닫고 다음 버전을 더한다. 같은 구역을 동시에 고쳐
 * 다른 요청이 먼저 새 버전을 만들었으면 409(CONCURRENT_EDIT)다.
 */
export function updateZone(http: Http, zoneId: string, body: ZoneBody) {
  return http(`/api/zones/${encodeURIComponent(zoneId)}`, {
    method: 'PUT',
    body,
    schema: zoneItemSchema,
  });
}

// 최근 구성 묶음 조회 키. 첫 항목이 가장 최근에 발행한 묶음이다.
export const BUNDLES_KEY = ['config-bundles'] as const;

/** 최근 구성 묶음(새것부터, 서버가 20개까지 준다). 적용 상태(status)가 함께 온다. */
export function requestBundles(http: Http, signal?: AbortSignal) {
  return http('/api/config-bundles', { schema: bundlesResponseSchema, signal });
}

/**
 * 구성 묶음 발행(관리자). 서버가 그때 유효한 구역·규칙·보정·작업면으로 묶어 Tier 0 에 알린다. 본문은 비운다(규칙 프로필은
 * 이전 묶음의 것을 이어 쓴다). 응답은 새 묶음이다(적용 대기).
 */
export function publishBundle(http: Http) {
  return http('/api/config-bundles', { method: 'POST', body: {}, schema: configBundleSchema });
}
