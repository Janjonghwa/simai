import { normalizeGear, usePpeAssets } from '@/features/viewer3d/people/ppe/ppeAssets';
import { renderHook } from '@testing-library/react';
import { BoxGeometry, Group, Mesh, MeshStandardMaterial, Texture } from 'three';
import { afterEach, describe, expect, it, vi } from 'vitest';

// 앱 Vite 설정(assetsInclude)을 바꾸지 않으려고 파일 바이트는 Node fs 로 읽는다(avatarMesh.test.tsx 와 같은 방식, 앱 tsconfig 에 Node 타입이 없다).
const fs = (await import(/* @vite-ignore */ 'node:fs' as string)) as {
  readFileSync(path: string): Uint8Array;
};
// vitest 는 앱 폴더(front/apps/admin)에서 돈다.
const cwd = (Reflect.get(globalThis, 'process') as { cwd(): string }).cwd();

/** 시험 환경(jsdom)의 ArrayBuffer 로 복사해 로더가 바이너리로 알아보게 한다(Node Buffer 의 buffer 는 다른 영역이다). */
const readAsset = (name: string): ArrayBuffer =>
  new Uint8Array(fs.readFileSync(`${cwd}/src/features/viewer3d/people/ppe/assets/${name}`)).buffer;

describe('normalizeGear', () => {
  it('노드 변환을 구워 넣고 가로·세로·앞뒤 1 인 상자로 맞춘다. 안전모는 아래 가운데, 조끼는 위쪽 가운데가 원점이다', () => {
    const scene = new Group();
    const mesh = new Mesh(new BoxGeometry(2, 4, 6), new MeshStandardMaterial());
    // 노드 변환: 옆으로 이동해 둔다(원점이 가운데가 아니다).
    mesh.position.set(10, 20, 30);
    scene.add(mesh);

    const helmet = normalizeGear(scene, 'BOTTOM')!;
    const helmetBox = helmet.geometry.boundingBox!;
    expect(helmetBox.min.x).toBeCloseTo(-0.5);
    expect(helmetBox.max.x).toBeCloseTo(0.5);
    expect(helmetBox.min.y).toBeCloseTo(0);
    expect(helmetBox.max.y).toBeCloseTo(1);
    expect(helmetBox.min.z).toBeCloseTo(-0.5);
    expect(helmetBox.max.z).toBeCloseTo(0.5);

    const vest = normalizeGear(scene, 'TOP')!;
    const vestBox = vest.geometry.boundingBox!;
    expect(vestBox.max.y).toBeCloseTo(0);
    expect(vestBox.min.y).toBeCloseTo(-1);
  });

  it('원본 메시를 바꾸지 않고, 기본 색 텍스처를 그대로 쓴다', () => {
    const scene = new Group();
    const map = new Texture();
    const geometry = new BoxGeometry(1, 1, 1);
    scene.add(new Mesh(geometry, new MeshStandardMaterial({ map })));
    const gear = normalizeGear(scene, 'BOTTOM')!;
    expect(gear.map).toBe(map);
    expect(gear.geometry).not.toBe(geometry);
    expect(gear.edges.getAttribute('position').count).toBeGreaterThan(0);
  });

  it('메시가 없는 장면이면 null 이다', () => {
    expect(normalizeGear(new Group(), 'TOP')).toBeNull();
  });
});

/** GLB 의 JSON 청크. jsdom 은 GLB 안의 텍스처 그림을 풀지 못해 로더로는 읽지 않고 구조만 본다. */
function glbJson(file: string) {
  const buffer = readAsset(file);
  const view = new DataView(buffer);
  expect(new TextDecoder().decode(new Uint8Array(buffer, 0, 4))).toBe('glTF');
  const jsonLength = view.getUint32(12, true);
  return JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 20, jsonLength))) as {
    extensionsUsed?: string[];
    extensionsRequired?: string[];
    meshes: { primitives: { attributes: Record<string, number> }[] }[];
    images?: { mimeType: string }[];
    materials: {
      normalTexture?: unknown;
      pbrMetallicRoughness?: { metallicRoughnessTexture?: unknown };
    }[];
  };
}

describe('실제 안전모·조끼 파일', () => {
  // 사람 레이어의 로더는 Meshopt·KTX2·Draco 를 붙이지 않는다. 확장 없이 읽을 수 있어야 한다.
  it.each(['helmet.glb', 'vest.glb'])('%s 는 확장 없이 일반 로더로 읽는 모양이다', (file) => {
    const json = glbJson(file);
    expect(json.extensionsUsed ?? []).toEqual([]);
    expect(json.extensionsRequired ?? []).toEqual([]);
    expect(
      json.images?.every((image) => ['image/jpeg', 'image/png'].includes(image.mimeType)),
    ).toBe(true);
  });

  it.each(['helmet.glb', 'vest.glb'])(
    '%s 에는 안 쓰는 UV·접선·법선·거칠기 텍스처가 없다',
    (file) => {
      const json = glbJson(file);
      const attributes = json.meshes.flatMap((mesh) =>
        mesh.primitives.flatMap((p) => Object.keys(p.attributes)),
      );
      expect(attributes.sort()).toEqual(['NORMAL', 'POSITION', 'TEXCOORD_0']);
      expect(json.materials[0]?.normalTexture).toBeUndefined();
      expect(json.materials[0]?.pbrMetallicRoughness?.metallicRoughnessTexture).toBeUndefined();
      expect(json.images).toHaveLength(1);
    },
  );

  it('두 파일을 합쳐 300 KB 를 넘지 않는다', () => {
    expect(readAsset('helmet.glb').byteLength + readAsset('vest.glb').byteLength).toBeLessThan(
      300 * 1024,
    );
  });
});

describe('usePpeAssets', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('그릴 장비가 없으면(wanted=false) 모델 파일을 받지 않고, 생기면 받기 시작한다', () => {
    const fetchSpy = vi.fn(() => Promise.resolve({ ok: false, status: 404 }));
    vi.stubGlobal('fetch', fetchSpy);
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const { rerender } = renderHook(({ wanted }) => usePpeAssets(wanted), {
      initialProps: { wanted: false },
    });
    expect(fetchSpy).not.toHaveBeenCalled();
    rerender({ wanted: true });
    // 안전모와 조끼 두 파일
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});
