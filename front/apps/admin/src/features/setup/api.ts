import {
  cameraHeightDefaultsResponseSchema,
  coverageGuidesResponseSchema,
  setupSessionResponseSchema,
  type CameraHeightDefaultsResponse,
  type CoverageGuidesResponse,
  type CreateSetupSessionBody,
  type SetupSessionResponse,
} from '@/features/setup/types';
import type { Http } from '@/lib/http';

// 세팅 1단계 REST 요청(http 주입, S15P31A101-448).

/** 세팅 1단계 화면이 열려 있는 동안 카메라 각도 헬퍼를 다시 받는 주기(2~3초). */
export const COVERAGE_GUIDE_POLL_MS = 2_500;

export const SETUP_COVERAGE_GUIDES_KEY = ['setup', 'coverage-guides'] as const;
export const SETUP_HEIGHT_DEFAULTS_KEY = ['setup', 'camera-height-defaults'] as const;
export const setupSessionKey = (id: string) => ['setup', 'session', id] as const;

export function requestCoverageGuides(
  http: Http,
  signal?: AbortSignal,
): Promise<CoverageGuidesResponse> {
  return http('/api/cameras/coverage-guides', {
    schema: coverageGuidesResponseSchema,
    signal,
  });
}

export function requestCameraHeightDefaults(
  http: Http,
  signal?: AbortSignal,
): Promise<CameraHeightDefaultsResponse> {
  return http('/api/setup-sessions/camera-height-defaults', {
    schema: cameraHeightDefaultsResponseSchema,
    signal,
  });
}

export function createSetupSession(
  http: Http,
  body: CreateSetupSessionBody,
): Promise<SetupSessionResponse> {
  return http('/api/setup-sessions', {
    method: 'POST',
    body,
    schema: setupSessionResponseSchema,
  });
}

export function requestSetupSession(
  http: Http,
  setupSessionId: string,
  signal?: AbortSignal,
): Promise<SetupSessionResponse> {
  return http(`/api/setup-sessions/${encodeURIComponent(setupSessionId)}`, {
    schema: setupSessionResponseSchema,
    signal,
  });
}
