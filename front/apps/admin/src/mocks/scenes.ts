import { readLiveUser, unauthorized } from '@/mocks/problem';
import { http, HttpResponse } from 'msw/http';

// 개발 하네스와 같은 자산만 재사용한다. 뷰어 내부 코드는 import 하지 않는다.
const demoSceneUrl = new URL(
  '../features/viewer3d/dev-harness/fixtures/scene-demo.gltf',
  import.meta.url,
).href;

// 서버가 주는 서명 URL 의 유효 시간.
const SIGNED_URL_TTL_MS = 5 * 60_000;

export const sceneHandlers = [
  http.get('*/api/scenes/current', ({ request }) => {
    const now = Date.now();
    if (!readLiveUser(request, now)) return unauthorized();
    // devScene.ts 의 DEFAULT 본문과 같다. 실측·보정 정합을 주장하지 않는다.
    return HttpResponse.json({
      scene_version: 'scene-demo',
      state: 'DEFAULT',
      default: true,
      calibration_version: null,
      committed_at_ms: null,
      quality: null,
      gltf_url: demoSceneUrl,
      gltf_expires_at_ms: now + SIGNED_URL_TTL_MS,
    });
  }),
];
