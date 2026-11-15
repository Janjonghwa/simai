import type { BufferAttribute, BufferGeometry, Material, Mesh, Object3D } from 'three';

// 아바타 외형 표시용 채운 albedo(S15P31A101-742). 설계: docs/design/S15P31A101-742-avatar-fill-display.md.
// surface_source 데이터(관측 2·보간 1·미관측 0, 얼굴 +128)는 그대로 두고, 화면에 그릴 색만 CPU 에서 채운다.
// - 미관측(0) 몸 텍셀: 같은 UV 섬·같은 상하(허리 기준) 부위 안에서 관측·보간 텍셀 색을 낮은 해상도에서 번져 채운다.
//   남는 텍셀은 그 부위 관측 색 중앙값, 부위에 관측이 없으면 인접 부위(이음매로 붙은 섬) 관측 색 중앙값, 그다음 같은 상하 관측 색 중앙값.
//   그래도 없으면 알파 0 으로 두어 셰이더가 기본 재질 색을 쓴다.
// - 얼굴 미관측(128): 관측 얼굴(130, face_policy 가 EXCLUDED 가 아닐 때)이나 목 피부 텍셀의 단색 평균. 이목구비를 만들지 않는다(AGENTS 규칙 8).
// - 관측·보간·얼굴 관측 텍셀은 원래 색 그대로 둔다(130 의 face_policy 처리는 셰이더 몫).

/** surface_source 부호(avatarAppearance.ts 와 같은 값. 순환 import 를 피하려고 여기 따로 둔다). */
const CODE_INTERPOLATED = 1;
const CODE_OBSERVED = 2;
const CODE_FACE_BIT = 0x80;
const CODE_FACE_OBSERVED = 130;

/** 채우기 계산 격자의 목표 한 변(텍셀). 1024 텍스처는 4×4 묶음으로 256 격자에서 번진다. */
export const AVATAR_FILL_GRID_TARGET = 256;
/** 번진 색의 계단을 없애는 완화 반복 횟수(관측 씨앗은 고정). */
export const AVATAR_FILL_SMOOTH_PASSES = 2;
/** UV 섬 밖(아틀라스 빈칸)으로 더 번지는 격자 칸 수. 이음매 쌍선형 표본에 검정이 섞이지 않게 한다. */
export const AVATAR_FILL_GUTTER_CELLS = 2;
/** 목 피부를 찾는 얼굴 경계 반경(격자 칸). */
export const AVATAR_FILL_NECK_RADIUS_CELLS = 2;

export type AvatarFillFacePolicy = 'OBSERVED' | 'BLURRED' | 'EXCLUDED';
type Rgb = [number, number, number];

/** UV 격자 칸마다의 몸 부위 번호(섬 × 상하). 메시 TEXCOORD_0 과 바인드 자세 높이에서 만든다. */
export interface AvatarUvPartMap {
  width: number;
  height: number;
  /** 칸별 부위 번호(-1: 표면 없음). */
  part: Int32Array;
  /** 부위별 상하(0: 허리 위, 1: 허리 아래). */
  partBand: Uint8Array;
  /** 부위별 UV 섬 번호. */
  partIsland: Int32Array;
  /** 부위별 인접 부위(같은 섬의 다른 상하, 3D 이음매로 붙은 다른 섬). */
  adjacency: readonly (readonly number[])[];
}

export interface AvatarFillGrid {
  factor: number;
  width: number;
  height: number;
}

/** 텍스처 크기에서 채우기 격자 크기를 정한다. */
export function avatarFillGridSize(width: number, height: number): AvatarFillGrid {
  const factor = Math.max(1, Math.floor(Math.max(width, height) / AVATAR_FILL_GRID_TARGET));
  return { factor, width: Math.ceil(width / factor), height: Math.ceil(height / factor) };
}

// ---- UV 부위 지도 ----

class UnionFind {
  private readonly parent: Int32Array;
  constructor(n: number) {
    this.parent = new Int32Array(n);
    for (let i = 0; i < n; i++) this.parent[i] = i;
  }
  find(a: number): number {
    let r = a;
    while (this.parent[r] !== r) r = this.parent[r]!;
    let c = a;
    while (this.parent[c] !== r) {
      const next = this.parent[c]!;
      this.parent[c] = r;
      c = next;
    }
    return r;
  }
  union(a: number, b: number) {
    const ra = this.find(a);
    const rb = this.find(b);
    if (ra !== rb) this.parent[ra] = rb;
  }
}

function triangleIndex(geometry: BufferGeometry): ArrayLike<number> {
  const index = geometry.getIndex();
  if (index) return index.array;
  const count = (geometry.getAttribute('position') as BufferAttribute).count;
  const seq = new Uint32Array(count - (count % 3));
  for (let i = 0; i < seq.length; i++) seq[i] = i;
  return seq;
}

/**
 * 메시들의 UV(glTF 상단 원점, flipY=false)를 width×height 격자에 래스터화해 부위 지도를 만든다.
 * - UV 섬: 삼각형으로 이어진 정점, 그리고 UV 좌표가 같은 정점을 한 섬으로 묶는다.
 * - 부위: 섬 × 상하(삼각형 무게중심의 바인드 자세 높이가 waistY 이상이면 위). 셰이더의 상·하 기본색 경계와 같다.
 * - 인접: 같은 섬의 위·아래 부위, 그리고 바인드 위치가 같은 정점(이음매에서 갈라진 정점)을 가진 다른 섬.
 */
export function buildAvatarUvPartMap(
  geometries: readonly BufferGeometry[],
  width: number,
  height: number,
  waistY: number,
): AvatarUvPartMap {
  const part = new Int32Array(width * height).fill(-1);
  const islandOfKey = new Map<string, number>();
  const seamPairs: [number, number][] = [];
  const posKeyIsland = new Map<string, number>();
  let islandBase = 0;
  const triangles: ArrayLike<number>[] = [];
  const islandPerVertex: Int32Array[] = [];

  geometries.forEach((geometry) => {
    const pos = geometry.getAttribute('position') as BufferAttribute | undefined;
    const uv = geometry.getAttribute('uv') as BufferAttribute | undefined;
    if (!pos || !uv || pos.count === 0) {
      islandPerVertex.push(new Int32Array(0));
      triangles.push([]);
      return;
    }
    const tris = triangleIndex(geometry);
    triangles.push(tris);
    const uf = new UnionFind(pos.count);
    for (let t = 0; t + 2 < tris.length; t += 3) {
      uf.union(tris[t]!, tris[t + 1]!);
      uf.union(tris[t + 1]!, tris[t + 2]!);
    }
    const uvKeyVertex = new Map<string, number>();
    for (let v = 0; v < uv.count; v++) {
      const key = `${Math.round(uv.getX(v) * 1e5)}:${Math.round(uv.getY(v) * 1e5)}`;
      const other = uvKeyVertex.get(key);
      if (other === undefined) uvKeyVertex.set(key, v);
      else uf.union(v, other);
    }
    const islands = new Int32Array(pos.count);
    islandOfKey.clear();
    for (let v = 0; v < pos.count; v++) {
      const rootKey = String(uf.find(v));
      let island = islandOfKey.get(rootKey);
      if (island === undefined) {
        island = islandBase + islandOfKey.size;
        islandOfKey.set(rootKey, island);
      }
      islands[v] = island;
      const pk = `${Math.round(pos.getX(v) * 1e4)}:${Math.round(pos.getY(v) * 1e4)}:${Math.round(pos.getZ(v) * 1e4)}`;
      const seen = posKeyIsland.get(pk);
      if (seen === undefined) posKeyIsland.set(pk, island);
      else if (seen !== island) seamPairs.push([seen, island]);
    }
    islandBase += islandOfKey.size;
    islandPerVertex.push(islands);
  });

  const islandCount = islandBase;
  // 부위 번호 = 섬 × 2 + 상하. 실제로 삼각형이 있는 부위만 쓴다.
  const partUsed = new Uint8Array(islandCount * 2);
  geometries.forEach((geometry, gi) => {
    const pos = geometry.getAttribute('position') as BufferAttribute | undefined;
    const uv = geometry.getAttribute('uv') as BufferAttribute | undefined;
    const islands = islandPerVertex[gi]!;
    const tris = triangles[gi]!;
    if (!pos || !uv) return;
    for (let t = 0; t + 2 < tris.length; t += 3) {
      const a = tris[t]!;
      const b = tris[t + 1]!;
      const c = tris[t + 2]!;
      const cy = (pos.getY(a) + pos.getY(b) + pos.getY(c)) / 3;
      const band = cy >= waistY ? 0 : 1;
      const pid = islands[a]! * 2 + band;
      partUsed[pid] = 1;
      rasterTriangle(
        part,
        width,
        height,
        pid,
        uv.getX(a) * width,
        uv.getY(a) * height,
        uv.getX(b) * width,
        uv.getY(b) * height,
        uv.getX(c) * width,
        uv.getY(c) * height,
      );
    }
  });

  // 격자 칸 중심을 덮지 못한 얇은 가장자리 칸을 이웃 부위로 한 칸 메운다.
  const grown = part.slice();
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const i = y * width + x;
      if (part[i]! >= 0) continue;
      const left = x > 0 ? part[i - 1]! : -1;
      const right = x + 1 < width ? part[i + 1]! : -1;
      const up = y > 0 ? part[i - width]! : -1;
      const down = y + 1 < height ? part[i + width]! : -1;
      grown[i] = left >= 0 ? left : right >= 0 ? right : up >= 0 ? up : down;
    }
  }

  const partCount = islandCount * 2;
  const partBand = new Uint8Array(partCount);
  const partIsland = new Int32Array(partCount);
  const adjacencySets: Set<number>[] = Array.from({ length: partCount }, () => new Set<number>());
  for (let p = 0; p < partCount; p++) {
    partBand[p] = p % 2;
    partIsland[p] = Math.floor(p / 2);
  }
  for (let island = 0; island < islandCount; island++) {
    const up = island * 2;
    const down = up + 1;
    if (partUsed[up] && partUsed[down]) {
      adjacencySets[up]!.add(down);
      adjacencySets[down]!.add(up);
    }
  }
  for (const [a, b] of seamPairs) {
    for (const pa of [a * 2, a * 2 + 1]) {
      for (const pb of [b * 2, b * 2 + 1]) {
        if (!partUsed[pa] || !partUsed[pb]) continue;
        adjacencySets[pa]!.add(pb);
        adjacencySets[pb]!.add(pa);
      }
    }
  }
  return {
    width,
    height,
    part: grown,
    partBand,
    partIsland,
    adjacency: adjacencySets.map((s) => [...s]),
  };
}

function rasterTriangle(
  out: Int32Array,
  width: number,
  height: number,
  value: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
  cx: number,
  cy: number,
) {
  const minX = Math.max(0, Math.floor(Math.min(ax, bx, cx)));
  const maxX = Math.min(width - 1, Math.ceil(Math.max(ax, bx, cx)));
  const minY = Math.max(0, Math.floor(Math.min(ay, by, cy)));
  const maxY = Math.min(height - 1, Math.ceil(Math.max(ay, by, cy)));
  const area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
  if (Math.abs(area) < 1e-12) {
    // 넓이 없는 삼각형도 정점 칸은 표시한다.
    const vx = Math.min(width - 1, Math.max(0, Math.floor(ax)));
    const vy = Math.min(height - 1, Math.max(0, Math.floor(ay)));
    out[vy * width + vx] = value;
    return;
  }
  const inv = 1 / area;
  const eps = -1e-6;
  for (let y = minY; y <= maxY; y++) {
    const py = y + 0.5;
    for (let x = minX; x <= maxX; x++) {
      const px = x + 0.5;
      const w0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) * inv;
      const w1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) * inv;
      const w2 = 1 - w0 - w1;
      if (w0 >= eps && w1 >= eps && w2 >= eps) out[y * width + x] = value;
    }
  }
  // 칸 중심을 하나도 덮지 못한 작은 삼각형도 첫 정점 칸은 표시한다.
  const vx = Math.min(width - 1, Math.max(0, Math.floor(ax)));
  const vy = Math.min(height - 1, Math.max(0, Math.floor(ay)));
  if (out[vy * width + vx]! < 0) out[vy * width + vx] = value;
}

const partMapCache = new WeakMap<Object3D, Map<string, AvatarUvPartMap | null>>();

/** 아바타 자산 장면(공유 형상)의 부위 지도를 격자 크기별로 한 번만 만든다. 관절(Joints) 재질 메시는 뺀다. */
export function avatarUvPartMapForScene(
  scene: Object3D,
  width: number,
  height: number,
  waistY: number,
): AvatarUvPartMap | null {
  const key = `${width}x${height}@${waistY}`;
  let byKey = partMapCache.get(scene);
  if (!byKey) {
    byKey = new Map();
    partMapCache.set(scene, byKey);
  }
  if (byKey.has(key)) return byKey.get(key)!;
  const geometries: BufferGeometry[] = [];
  scene.traverse((node) => {
    const mesh = node as Mesh;
    if (!mesh.isMesh || !mesh.geometry) return;
    const materials = [mesh.material].flat() as Material[];
    if (materials.some((m) => m && /joints/i.test(m.name))) return;
    if (!mesh.geometry.getAttribute('uv')) return;
    geometries.push(mesh.geometry);
  });
  const map =
    geometries.length > 0 ? buildAvatarUvPartMap(geometries, width, height, waistY) : null;
  byKey.set(key, map);
  return map;
}

// ---- 채우기 ----

export interface AvatarFillInput {
  width: number;
  height: number;
  /** albedo RGBA(width × height × 4). */
  albedo: Uint8Array;
  /** surface_source RGBA(R 채널이 부호). 크기가 다르면 가장 가까운 텍셀을 쓴다. */
  source: Uint8Array;
  sourceWidth: number;
  sourceHeight: number;
  facePolicy: AvatarFillFacePolicy;
  /** 채우기 격자(avatarFillGridSize)와 같은 크기의 부위 지도. 없으면 텍스처 전체를 한 부위로 본다. */
  partMap: AvatarUvPartMap | null;
}

export interface AvatarFillResult {
  /** 표시용 RGBA. 알파 255 는 채운 색, 0 은 셰이더가 기본 재질 색을 쓰는 텍셀이다. */
  data: Uint8Array;
  /** 얼굴 미관측(128)에 넣은 단색(0~255). 없으면 null. */
  skinRgb: Rgb | null;
  /** 관측으로 번져 채운 부위, 인접·상하 대체로 채운 부위, 못 채운 부위 수. */
  stats: { seededParts: number; borrowedParts: number; emptyParts: number; ms: number };
}

/** 0~255 값의 중앙값을 256 칸 히스토그램으로 구한다(정렬 없이 O(n)). */
function histogramMedian(
  cells: readonly number[],
  values: Float32Array,
  hist: Uint32Array,
): number {
  hist.fill(0);
  for (const i of cells) hist[Math.min(255, Math.max(0, Math.round(values[i]!)))]!++;
  const half = cells.length / 2;
  let acc = 0;
  for (let v = 0; v < 256; v++) {
    acc += hist[v]!;
    if (acc >= half) return v;
  }
  return 255;
}

function medianRgb(
  cells: readonly number[],
  r: Float32Array,
  g: Float32Array,
  b: Float32Array,
): Rgb {
  const hist = new Uint32Array(256);
  return [
    histogramMedian(cells, r, hist),
    histogramMedian(cells, g, hist),
    histogramMedian(cells, b, hist),
  ];
}

/**
 * 표시용 albedo 를 만든다. 관측(2)·보간(1)·얼굴 관측(130) 텍셀은 원래 색, 미관측(0)·모르는 값은 같은 부위 관측 색으로 채우고,
 * 얼굴 미관측(128 등 얼굴 비트)은 피부 단색으로 채운다.
 */
export function fillAvatarAlbedo(input: AvatarFillInput): AvatarFillResult {
  const started = performance.now();
  const {
    width: w,
    height: h,
    albedo,
    source,
    sourceWidth: sw,
    sourceHeight: sh,
    facePolicy,
  } = input;
  const grid = avatarFillGridSize(w, h);
  const { factor: f, width: lw, height: lh } = grid;
  const n = lw * lh;
  const partMap =
    input.partMap && input.partMap.width === lw && input.partMap.height === lh
      ? input.partMap
      : null;
  const sameSize = sw === w && sh === h;

  // 1) 부호 읽기와 격자 씨앗(관측·보간 몸 텍셀 평균), 얼굴 관측 평균.
  const codes = new Uint8Array(w * h);
  const sumR = new Float32Array(n);
  const sumG = new Float32Array(n);
  const sumB = new Float32Array(n);
  const cnt = new Uint32Array(n);
  const faceCell = new Uint8Array(n);
  const bodyCell = new Uint8Array(n);
  let faceR = 0;
  let faceG = 0;
  let faceB = 0;
  let faceN = 0;
  const useFaceObserved = facePolicy !== 'EXCLUDED';
  const lxs = new Int32Array(w);
  const sxs = new Int32Array(w);
  for (let x = 0; x < w; x++) {
    lxs[x] = (x / f) | 0;
    sxs[x] = sameSize ? x : Math.min(sw - 1, Math.floor(((x + 0.5) * sw) / w));
  }
  for (let y = 0; y < h; y++) {
    const rowL = ((y / f) | 0) * lw;
    const sy = sameSize ? y : Math.min(sh - 1, Math.floor(((y + 0.5) * sh) / h));
    const rowS = sy * sw;
    for (let x = 0; x < w; x++) {
      const code = source[(rowS + sxs[x]!) * 4]!;
      const t = y * w + x;
      codes[t] = code;
      const li = rowL + lxs[x]!;
      if (code & CODE_FACE_BIT) {
        faceCell[li] = 1;
        if (code === CODE_FACE_OBSERVED && useFaceObserved) {
          faceR += albedo[t * 4]!;
          faceG += albedo[t * 4 + 1]!;
          faceB += albedo[t * 4 + 2]!;
          faceN++;
        }
        continue;
      }
      bodyCell[li] = 1;
      if (code === CODE_OBSERVED || code === CODE_INTERPOLATED) {
        sumR[li] += albedo[t * 4]!;
        sumG[li] += albedo[t * 4 + 1]!;
        sumB[li] += albedo[t * 4 + 2]!;
        cnt[li]!++;
      }
    }
  }

  // 2) 격자 칸 부위. 얼굴만 있는 칸은 몸 채우기에서 뺀다(-2).
  const rawPart = new Int32Array(n);
  const part = new Int32Array(n);
  for (let i = 0; i < n; i++) {
    rawPart[i] = partMap ? partMap.part[i]! : 0;
    part[i] = faceCell[i] && !bodyCell[i] ? -2 : rawPart[i]!;
  }
  const colR = new Float32Array(n);
  const colG = new Float32Array(n);
  const colB = new Float32Array(n);
  // 0: 비었음, 1: 관측 씨앗, 2: 채움
  const state = new Uint8Array(n);
  let frontier: number[] = [];
  for (let i = 0; i < n; i++) {
    if (cnt[i]! > 0) {
      colR[i] = sumR[i]! / cnt[i]!;
      colG[i] = sumG[i]! / cnt[i]!;
      colB[i] = sumB[i]! / cnt[i]!;
      state[i] = 1;
      if (part[i]! >= 0) frontier.push(i);
    }
  }

  // 3) 같은 부위 안에서 파면(wave)으로 번진다. 새 칸은 앞 파면까지 채워진 같은 부위 8이웃의 평균이다.
  const queued = new Uint8Array(n);
  const averageNeighbors = (j: number, constrain: boolean): boolean => {
    const x = j % lw;
    const y = (j / lw) | 0;
    let r = 0;
    let g = 0;
    let b = 0;
    let k = 0;
    for (let dy = -1; dy <= 1; dy++) {
      const yy = y + dy;
      if (yy < 0 || yy >= lh) continue;
      for (let dx = -1; dx <= 1; dx++) {
        const xx = x + dx;
        if ((dx === 0 && dy === 0) || xx < 0 || xx >= lw) continue;
        const q = yy * lw + xx;
        if (state[q] === 0 || (constrain && part[q] !== part[j])) continue;
        r += colR[q]!;
        g += colG[q]!;
        b += colB[q]!;
        k++;
      }
    }
    if (k === 0) return false;
    colR[j] = r / k;
    colG[j] = g / k;
    colB[j] = b / k;
    return true;
  };
  while (frontier.length > 0) {
    const next: number[] = [];
    for (const i of frontier) {
      const x = i % lw;
      const y = (i / lw) | 0;
      const p = part[i]!;
      if (x > 0) tryQueue(i - 1, p, next);
      if (x + 1 < lw) tryQueue(i + 1, p, next);
      if (y > 0) tryQueue(i - lw, p, next);
      if (y + 1 < lh) tryQueue(i + lw, p, next);
    }
    for (const j of next) averageNeighbors(j, true);
    for (const j of next) state[j] = 2;
    frontier = next;
  }
  function tryQueue(j: number, p: number, next: number[]) {
    if (state[j] !== 0 || queued[j] || part[j] !== p) return;
    queued[j] = 1;
    next.push(j);
  }

  // 4) 번진 칸만 완화해 계단을 없앤다(씨앗 고정).
  const tmpR = new Float32Array(n);
  const tmpG = new Float32Array(n);
  const tmpB = new Float32Array(n);
  for (let pass = 0; pass < AVATAR_FILL_SMOOTH_PASSES; pass++) {
    tmpR.set(colR);
    tmpG.set(colG);
    tmpB.set(colB);
    for (let i = 0; i < n; i++) {
      if (state[i] !== 2) continue;
      const x = i % lw;
      const y = (i / lw) | 0;
      let r = colR[i]!;
      let g = colG[i]!;
      let b = colB[i]!;
      let k = 1;
      const p = part[i]!;
      const add = (q: number) => {
        if (state[q] === 0 || part[q] !== p) return;
        r += colR[q]!;
        g += colG[q]!;
        b += colB[q]!;
        k++;
      };
      if (x > 0) add(i - 1);
      if (x + 1 < lw) add(i + 1);
      if (y > 0) add(i - lw);
      if (y + 1 < lh) add(i + lw);
      tmpR[i] = r / k;
      tmpG[i] = g / k;
      tmpB[i] = b / k;
    }
    colR.set(tmpR);
    colG.set(tmpG);
    colB.set(tmpB);
  }

  // 5) 부위별 씨앗 모음 → 남은 칸은 부위 중앙값, 관측 없는 부위는 인접 부위·같은 상하 중앙값.
  const partCount = partMap ? partMap.partBand.length : 1;
  const seedsOf: number[][] = Array.from({ length: partCount }, () => []);
  const cellsOf: number[][] = Array.from({ length: partCount }, () => []);
  for (let i = 0; i < n; i++) {
    const p = part[i]!;
    if (p < 0) continue;
    cellsOf[p]!.push(i);
    if (state[i] === 1) seedsOf[p]!.push(i);
  }
  const partMedian: (Rgb | null)[] = seedsOf.map((cells) =>
    cells.length > 0 ? medianRgb(cells, colR, colG, colB) : null,
  );
  const bandMedian: (Rgb | null)[] = [0, 1].map((band) => {
    const cells: number[] = [];
    for (let p = 0; p < partCount; p++) {
      if (partMap && partMap.partBand[p] !== band) continue;
      cells.push(...seedsOf[p]!);
    }
    return cells.length > 0 ? medianRgb(cells, colR, colG, colB) : null;
  });
  const borrowed = (p: number): Rgb | null => {
    if (!partMap) return null;
    // 인접 그래프를 넓이 우선으로 따라가 처음 만나는 관측 부위들(같은 상하 우선)의 씨앗 중앙값.
    const seen = new Set<number>([p]);
    let level = [p];
    while (level.length > 0) {
      const nextLevel: number[] = [];
      for (const q of level) {
        for (const r of partMap.adjacency[q] ?? []) {
          if (seen.has(r)) continue;
          seen.add(r);
          nextLevel.push(r);
        }
      }
      const seeded = nextLevel.filter((r) => seedsOf[r]!.length > 0);
      if (seeded.length > 0) {
        const sameBand = seeded.filter((r) => partMap.partBand[r] === partMap.partBand[p]);
        const pick = sameBand.length > 0 ? sameBand : seeded;
        return medianRgb(
          pick.flatMap((r) => seedsOf[r]!),
          colR,
          colG,
          colB,
        );
      }
      level = nextLevel;
    }
    return bandMedian[partMap.partBand[p]!] ?? null;
  };
  let seededParts = 0;
  let borrowedParts = 0;
  let emptyParts = 0;
  for (let p = 0; p < partCount; p++) {
    const cells = cellsOf[p]!;
    if (cells.length === 0) continue;
    let flat = partMedian[p];
    if (flat) seededParts++;
    else {
      flat = borrowed(p);
      if (flat) borrowedParts++;
      else emptyParts++;
    }
    if (!flat) continue;
    for (const i of cells) {
      if (state[i] !== 0) continue;
      colR[i] = flat[0];
      colG[i] = flat[1];
      colB[i] = flat[2];
      state[i] = 2;
    }
  }

  // 6) UV 섬 밖 빈칸으로 몇 칸 더 번져 이음매 쌍선형 표본이 검정으로 어두워지지 않게 한다.
  for (let wave = 0; wave < AVATAR_FILL_GUTTER_CELLS; wave++) {
    const next: number[] = [];
    for (let i = 0; i < n; i++) {
      if (state[i] !== 0 || part[i] !== -1) continue;
      if (averageNeighbors(i, false)) next.push(i);
    }
    for (const j of next) state[j] = 2;
    if (next.length === 0) break;
  }

  // 7) 얼굴 미관측 단색: 관측 얼굴 평균, 없으면 얼굴 경계 근처(같은 섬) 몸 관측 칸 평균(목 피부).
  let skin: Rgb | null = faceN > 0 ? [faceR / faceN, faceG / faceN, faceB / faceN] : null;
  if (!skin) {
    let r = 0;
    let g = 0;
    let b = 0;
    let k = 0;
    const rad = AVATAR_FILL_NECK_RADIUS_CELLS;
    for (let i = 0; i < n; i++) {
      if (cnt[i] === 0) continue;
      const x = i % lw;
      const y = (i / lw) | 0;
      const island = partMap && rawPart[i]! >= 0 ? partMap.partIsland[rawPart[i]!] : -1;
      let near = false;
      for (let dy = -rad; dy <= rad && !near; dy++) {
        const yy = y + dy;
        if (yy < 0 || yy >= lh) continue;
        for (let dx = -rad; dx <= rad; dx++) {
          const xx = x + dx;
          if (xx < 0 || xx >= lw) continue;
          const q = yy * lw + xx;
          if (!faceCell[q]) continue;
          if (partMap) {
            const qp = rawPart[q]!;
            if (qp < 0 || partMap.partIsland[qp] !== island) continue;
          }
          near = true;
          break;
        }
      }
      if (!near) continue;
      r += sumR[i]!;
      g += sumG[i]!;
      b += sumB[i]!;
      k += cnt[i]!;
    }
    if (k > 0) skin = [r / k, g / k, b / k];
  }
  const skinRgb: Rgb | null = skin
    ? [Math.round(skin[0]), Math.round(skin[1]), Math.round(skin[2])]
    : null;

  // 8) 원래 해상도로 옮긴다. 미관측 몸 텍셀은 같은 부위 격자 칸끼리만 쌍선형 보간한다.
  const out = new Uint8Array(albedo);
  const x0s = new Int32Array(w);
  const x1s = new Int32Array(w);
  const fxs = new Float32Array(w);
  for (let x = 0; x < w; x++) {
    const gx = Math.min(lw - 1, Math.max(0, (x + 0.5) / f - 0.5));
    x0s[x] = Math.floor(gx);
    x1s[x] = Math.min(lw - 1, x0s[x]! + 1);
    fxs[x] = gx - x0s[x]!;
  }
  // 칸별 쌍선형 표본 키: 채워진 칸이면 부위 번호(+3, -2/-1 포함), 빈칸이면 0.
  const tapKey = new Int32Array(n);
  for (let i = 0; i < n; i++) tapKey[i] = state[i] !== 0 ? part[i]! + 3 : 0;
  const skinR = skinRgb ? skinRgb[0] : 0;
  const skinG = skinRgb ? skinRgb[1] : 0;
  const skinB = skinRgb ? skinRgb[2] : 0;
  const skinA = skinRgb ? 255 : 0;
  for (let y = 0; y < h; y++) {
    const rowL = ((y / f) | 0) * lw;
    const gy = Math.min(lh - 1, Math.max(0, (y + 0.5) / f - 0.5));
    const y0 = Math.floor(gy);
    const row0 = y0 * lw;
    const row1 = Math.min(lh - 1, y0 + 1) * lw;
    const fy = gy - y0;
    for (let x = 0; x < w; x++) {
      const t = y * w + x;
      const o = t * 4;
      const code = codes[t]!;
      if (code === CODE_OBSERVED || code === CODE_INTERPOLATED || code === CODE_FACE_OBSERVED) {
        out[o + 3] = 255;
        continue;
      }
      if (code & CODE_FACE_BIT) {
        out[o] = skinR;
        out[o + 1] = skinG;
        out[o + 2] = skinB;
        out[o + 3] = skinA;
        continue;
      }
      const li = rowL + lxs[x]!;
      const own = tapKey[li]!;
      const want = part[li]! >= 0 ? part[li]! + 3 : -1;
      const fx = fxs[x]!;
      const q00 = row0 + x0s[x]!;
      const q01 = row0 + x1s[x]!;
      const q10 = row1 + x0s[x]!;
      const q11 = row1 + x1s[x]!;
      const k00 = tapKey[q00]!;
      const k01 = tapKey[q01]!;
      const k10 = tapKey[q10]!;
      const k11 = tapKey[q11]!;
      const w00 = k00 !== 0 && (want < 0 || k00 === want) ? (1 - fx) * (1 - fy) : 0;
      const w01 = k01 !== 0 && (want < 0 || k01 === want) ? fx * (1 - fy) : 0;
      const w10 = k10 !== 0 && (want < 0 || k10 === want) ? (1 - fx) * fy : 0;
      const w11 = k11 !== 0 && (want < 0 || k11 === want) ? fx * fy : 0;
      const ws = w00 + w01 + w10 + w11;
      if (ws > 0) {
        const inv = 1 / ws;
        out[o] =
          (colR[q00]! * w00 + colR[q01]! * w01 + colR[q10]! * w10 + colR[q11]! * w11) * inv + 0.5;
        out[o + 1] =
          (colG[q00]! * w00 + colG[q01]! * w01 + colG[q10]! * w10 + colG[q11]! * w11) * inv + 0.5;
        out[o + 2] =
          (colB[q00]! * w00 + colB[q01]! * w01 + colB[q10]! * w10 + colB[q11]! * w11) * inv + 0.5;
        out[o + 3] = 255;
      } else if (own !== 0) {
        out[o] = colR[li]! + 0.5;
        out[o + 1] = colG[li]! + 0.5;
        out[o + 2] = colB[li]! + 0.5;
        out[o + 3] = 255;
      } else {
        out[o + 3] = 0;
      }
    }
  }

  return {
    data: out,
    skinRgb,
    stats: { seededParts, borrowedParts, emptyParts, ms: performance.now() - started },
  };
}

/** 채우기에 실패했을 때의 안전한 표시 albedo: 관측·보간·얼굴 관측만 알파 255, 나머지는 알파 0(기본 재질 색). */
export function maskUnfilledAvatarAlbedo(input: AvatarFillInput): Uint8Array {
  const { width: w, height: h, albedo, source, sourceWidth: sw, sourceHeight: sh } = input;
  const out = new Uint8Array(albedo);
  for (let y = 0; y < h; y++) {
    const sy = Math.min(sh - 1, Math.floor(((y + 0.5) * sh) / h));
    for (let x = 0; x < w; x++) {
      const sx = Math.min(sw - 1, Math.floor(((x + 0.5) * sw) / w));
      const code = source[(sy * sw + sx) * 4]!;
      const keep =
        code === CODE_OBSERVED || code === CODE_INTERPOLATED || code === CODE_FACE_OBSERVED;
      out[(y * w + x) * 4 + 3] = keep ? 255 : 0;
    }
  }
  return out;
}
