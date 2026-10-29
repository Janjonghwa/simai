import { readMockUser } from '@/mocks/mockUser';
import { http, HttpResponse } from 'msw/http';

// 개발 하네스와 같은 자산만 재사용한다. 뷰어 내부 코드는 import 하지 않는다.
const demoSceneUrl = new URL(
  '../features/viewer3d/dev-harness/fixtures/scene-demo.gltf',
  import.meta.url,
).href;

export const sceneHandlers = [
  http.get('*/api/scenes/current', ({ request }) => {
    const now = Date.now();
    const user = readMockUser(request.headers.get('Authorization'));
    if (!user || (user.expiresAt !== undefined && user.expiresAt <= now)) {
      return HttpResponse.json({ code: 'UNAUTHORIZED' }, { status: 401 });
    }
    // devScene.ts 의 DEFAULT 본문과 같다. 실측·보정 정합을 주장하지 않는다.
    return HttpResponse.json({
      scene_version: 'scene-demo',
      state: 'DEFAULT',
      default: true,
      calibration_version: null,
      committed_at_ms: null,
      quality: null,
      gltf_url: demoSceneUrl,
      gltf_expires_at_ms: now + 5 * 60_000,
    });
  }),
];
