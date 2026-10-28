import { useCctvStore } from '@/features/cctv/cctv.store';
import { startPreviewSession } from '@/features/cctv/previewSession';
import { browserDeps, createCanvasRenderer, hasWebCodecs } from '@/features/cctv/webcodecs';
import { useEffect, useState } from 'react';

/**
 * CCTV 화면에 있는 동안만 현장 노트북 중계의 미리보기에 붙는다. 상태는 cctv.store 로, 영상은 캔버스로 바로 간다.
 * 돌려주는 attach 를 카메라 칸 캔버스의 ref 로 쓴다.
 */
export function useCctvPreview(url: string) {
  const [renderer] = useState(createCanvasRenderer);
  useEffect(() => {
    const store = useCctvStore.getState();
    store.reset();
    if (!hasWebCodecs()) {
      store.setLink('unsupported');
      return;
    }
    const session = startPreviewSession(url, browserDeps(renderer), store);
    return () => {
      session.stop();
      renderer.dispose();
      useCctvStore.getState().reset();
    };
  }, [url, renderer]);
  return renderer.attach;
}
