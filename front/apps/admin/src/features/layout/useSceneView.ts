import { currentSceneQuery, sceneViewQuery, useSceneQuery } from '@/features/layout/useSceneQuery';

/** 버전을 지정한 배경(/view). 버전이 없으면(null·빈 문자열) 받지 않고 scene 은 null 이다. */
export function useSceneView(sceneVersion: string | null) {
  return useSceneQuery(sceneVersion ? sceneViewQuery(sceneVersion) : null);
}

/** 호스트가 그릴 배경. 버전을 주면 그 버전(/view), 없으면 지금 관제 Scene(/current). */
export function useSceneOrCurrent(sceneVersion: string | null) {
  return useSceneQuery(sceneVersion ? sceneViewQuery(sceneVersion) : currentSceneQuery);
}
