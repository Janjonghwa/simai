"""EXP-01 텍스처 메시(S15P31A101-298)의 numpy 부품: 텍스처 후보 거르기, 구멍 판정, 텍셀 출처 지도, 확인 지표.

texture_mesh.py(명령 하나로 굽기)가 쓰고, 시험(ai/tests/experiments/static_scene/test_texture_mesh.py)이 작은 합성 장면으로 잰다.
numpy 만 쓴다(Open3D·OpenCV·Docker 없이 시험이 돈다). 설계·정한 수치의 근거는 docs/design/exp01-textured-mesh.md.

출처 값(EXP-02 아바타 표면 출처 지도와 같은 값, contracts avatar_asset surface_source.mask_uri): 2 = OBSERVED(실제 관측 픽셀),
1 = INTERPOLATED(작은 구멍을 둘레 관측으로 보간), 0 = UNOBSERVED(관측 없음, 중립 기본 재질). 3 = ESTIMATED_PLANE·4 = GENERATED 는
값 자리만 예약하고 내지 않는다(같은 평면 복사·확산 inpainting 은 만들지 않는다, AGENTS 규칙 2·7). 얼굴 비트(+128)는 배경에 없다.
겹치는 텍셀은 낮은 값을 남긴다(보간·미관측이 관측으로 승격되지 않게).

좌표: 카메라는 OpenCV(+x 오른쪽, +y 아래, +z 앞) cam2world 4×4, 픽셀 정수 = 픽셀 중심.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

UNOBSERVED, INTERPOLATED, OBSERVED = 0, 1, 2
SURFACE_VALUES = {"OBSERVED": OBSERVED, "INTERPOLATED": INTERPOLATED, "UNOBSERVED": UNOBSERVED}
RESERVED_VALUES = {"ESTIMATED_PLANE": 3, "GENERATED": 4}  # 값 자리만(이 티켓은 내지 않는다)
_UNSET = 255
_EPS = 1e-9  # 경계값(같으면 허용)을 부동소수 오차로 놓치지 않게

# texrecon(mvs-texturing) 데이터 비용 파일: 헤더 줄 "SPT <판> <면 수> <시점 수> <항목 수>" 뒤에 (면 u32, 시점 u16, 비용 f32) 묶음
SPT_DTYPE = np.dtype([("face", "<u4"), ("view", "<u2"), ("cost", "<f4")])
TEXRECON_MAX_HOLE_VERTICES = 100  # generate_texture_patches.cpp MAX_HOLE_NUM_FACES(이름과 달리 구멍 성분의 정점 수와 비교한다)


# --------------------------------------------------------------------------- texrecon 파일


@dataclass
class DataCosts:
    version: str
    faces: int
    views: int
    entries: np.ndarray  # SPT_DTYPE


def read_spt(path: Path) -> DataCosts:
    raw = Path(path).read_bytes()
    nl = raw.index(b"\n")
    head = raw[:nl].decode("ascii").split()
    if len(head) != 5 or head[0] != "SPT":
        raise ValueError(f"{path}: texrecon 데이터 비용 파일(SPT)이 아니다")
    nnz = int(head[4])
    if len(raw) - nl - 1 != nnz * SPT_DTYPE.itemsize:
        raise ValueError(f"{path}: 항목 수 {nnz} 와 파일 크기가 맞지 않는다")
    entries = np.frombuffer(raw, SPT_DTYPE, count=nnz, offset=nl + 1).copy()
    return DataCosts(head[1], int(head[2]), int(head[3]), entries)


def write_spt(path: Path, costs: DataCosts, entries: np.ndarray | None = None) -> None:
    e = costs.entries if entries is None else entries
    with open(path, "wb") as f:
        f.write(f"SPT {costs.version} {costs.faces} {costs.views} {len(e)}\n".encode("ascii"))
        f.write(np.ascontiguousarray(e, dtype=SPT_DTYPE).tobytes())


def read_labeling(path: Path) -> np.ndarray:
    """texrecon _labeling.vec(면마다 size_t). 0 = 어느 시점에서도 고르지 않음, k = k−1 번째 시점(.cam 이름순)."""
    return np.fromfile(path, dtype="<u8").astype(np.int64)


def write_labeling(path: Path, labels: np.ndarray) -> None:
    np.asarray(labels, dtype="<u8").tofile(path)


def cam_file_text(K: np.ndarray, cam2world: np.ndarray, hw: tuple[int, int]) -> str:
    """mve .cam(첫 줄 world→camera t·R, 둘째 줄 f d0 d1 paspect ppx ppy). mve 는 픽셀 = K·X/z − 0.5 라 주점에 0.5 를 더한다.
    영상은 이미 펴져 있어 왜곡 계수는 0 이다."""
    h, w = int(hw[0]), int(hw[1])
    R = np.asarray(cam2world, float)[:3, :3].T
    t = -R @ np.asarray(cam2world, float)[:3, 3]
    K = np.asarray(K, float)
    if abs(K[0, 1]) > 1e-9:
        raise ValueError("기울어진 픽셀(skew)은 mve .cam 으로 쓸 수 없다")
    if w < h:
        raise ValueError("세로 영상은 아직 다루지 않는다(mve 초점 정규화가 달라진다)")
    f = K[0, 0] / w
    vals = list(t) + list(R.reshape(-1))
    return (" ".join(f"{x:.10g}" for x in vals) + "\n"
            + f"{f:.10g} 0 0 {K[1, 1] / K[0, 0]:.10g} {(K[0, 2] + 0.5) / w:.10g} {(K[1, 2] + 0.5) / h:.10g}\n")


def parse_obj(path: Path) -> dict[str, Any]:
    """texrecon 출력 OBJ(v·vt·vn·usemtl·f v/vt/vn)를 읽는다. 반환: V, VT(OBJ 규약, v 위쪽), Fv, Ft(0 기반), 면 재질 번호, 재질 이름,
    재질 → 텍스처 파일(mtl map_Kd)."""
    path = Path(path)
    V, VT, Fv, Ft, Fm, mats, mtllib = [], [], [], [], [], [], None
    cur = -1
    with open(path, encoding="ascii") as f:
        for line in f:
            if line.startswith("v "):
                V.append(line.split()[1:4])
            elif line.startswith("vt "):
                VT.append(line.split()[1:3])
            elif line.startswith("f "):
                parts = [p.split("/") for p in line.split()[1:4]]
                Fv.append([int(p[0]) - 1 for p in parts])
                Ft.append([int(p[1]) - 1 for p in parts])
                Fm.append(cur)
            elif line.startswith("usemtl "):
                name = line.split()[1]
                if name not in mats:
                    mats.append(name)
                cur = mats.index(name)
            elif line.startswith("mtllib "):
                mtllib = line.split(maxsplit=1)[1].strip()
    textures: dict[str, str] = {}
    if mtllib and (path.parent / mtllib).is_file():
        name = None
        for line in (path.parent / mtllib).read_text(encoding="ascii").splitlines():
            if line.startswith("newmtl "):
                name = line.split()[1]
            elif line.startswith("map_Kd ") and name:
                textures[name] = line.split(maxsplit=1)[1].strip()
    return {"V": np.asarray(V, float).reshape(-1, 3), "VT": np.asarray(VT, float).reshape(-1, 2),
            "Fv": np.asarray(Fv, np.int64).reshape(-1, 3), "Ft": np.asarray(Ft, np.int64).reshape(-1, 3),
            "Fm": np.asarray(Fm, np.int64), "materials": mats, "textures": textures}


def face_keys(F: np.ndarray) -> np.ndarray:
    """정점 세 개(순서 무관)로 만든 면 열쇠(정렬한 정점 번호 세 쌍을 하나의 구조 배열로)."""
    s = np.sort(np.asarray(F, np.int64), axis=1)
    return np.ascontiguousarray(s).view([("a", "<i8"), ("b", "<i8"), ("c", "<i8")]).reshape(-1)


def map_faces(F_in: np.ndarray, F_out: np.ndarray) -> np.ndarray:
    """출력 면 → 입력 면 번호(texrecon 은 입력 정점 번호를 그대로 쓴다). 입력에 없는 출력 면이 있으면 ValueError."""
    kin, kout = face_keys(F_in), face_keys(F_out)
    if len(np.unique(kin)) != len(kin):
        raise ValueError("입력 메시에 같은 정점 세 개의 면이 둘 이상 있다(texrecon 이 지워 면 번호가 어긋난다)")
    order = np.argsort(kin)
    pos = np.searchsorted(kin[order], kout)
    pos = np.clip(pos, 0, len(kin) - 1)
    hit = kin[order][pos] == kout
    if not hit.all():
        raise ValueError(f"출력 면 {int((~hit).sum())}개가 입력 메시에 없다")
    return order[pos]


# --------------------------------------------------------------------------- 투영·텍스처 후보 거르기


def project(K: np.ndarray, cam2world: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """World 점(…, 3) → (픽셀 (…, 2), 카메라 z (…))."""
    T = np.asarray(cam2world, float)
    Xc = (np.asarray(X, float) - T[:3, 3]) @ T[:3, :3]
    z = Xc[..., 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        uvw = Xc @ np.asarray(K, float).T
        uv = uvw[..., :2] / uvw[..., 2:3]
    return uv, z


def face_samples(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    """면마다 점 7개(무게중심, 세 꼭짓점, 세 변 중점). 반환 (면 수, 7, 3), 첫 점이 무게중심."""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    return np.stack([(a + b + c) / 3, a, b, c, (a + b) / 2, (b + c) / 2, (c + a) / 2], axis=1)


@dataclass
class TexView:
    """텍스처 소스 한 장. person·edge·depth 는 없을 수 있다(None 이면 그 거르기를 하지 않고 기록에 남긴다)."""

    name: str
    K: np.ndarray  # 텍스처 영상(펴진 원본) 해상도
    cam2world: np.ndarray
    hw: tuple[int, int]
    person: np.ndarray | None = None  # (H, W) True = 사람(텍스처 소스에서 뺀다)
    depth: np.ndarray | None = None  # 이 카메라 깊이 격자(z), 무효 NaN
    depth_valid: np.ndarray | None = None
    K_depth: np.ndarray | None = None
    edge: np.ndarray | None = None  # 깊이 격자 해상도, True = 깊이 경계 띠


def _lookup(mask: np.ndarray, uv: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    x = np.clip(np.rint(uv[..., 0]).astype(np.int64), 0, w - 1)
    y = np.clip(np.rint(uv[..., 1]).astype(np.int64), 0, h - 1)
    return mask[y, x]


def texture_candidates(V: np.ndarray, F: np.ndarray, view: TexView, faces: np.ndarray,
                       occlusion_tol: float) -> tuple[np.ndarray, dict[str, int]]:
    """faces(면 번호들) 중 이 시점에서 텍스처 소스로 쓸 수 있는 면. 이유 순서: 영상 밖·뒤 → 사람 → 깊이 경계 띠 → 가림
    (무게중심이 이 카메라 깊이보다 occlusion_tol(상대) 넘게 뒤 = 메시에 없는 가림체가 앞에 있다). 점 7개 중 하나라도 사람·경계면 뺀다."""
    S = face_samples(V, F[faces])
    uv, z = project(view.K, view.cam2world, S)
    h, w = view.hw
    inside = ((z > 0) & (uv[..., 0] >= 0) & (uv[..., 0] <= w - 1) & (uv[..., 1] >= 0) & (uv[..., 1] <= h - 1)).all(axis=1)
    reasons = {"outside": int((~inside).sum())}
    ok = inside.copy()
    if view.person is not None:
        bad = ok & _lookup(view.person, uv).any(axis=1)
        reasons["person"] = int(bad.sum())
        ok &= ~bad
    if view.edge is not None and view.K_depth is not None:
        uvd, _ = project(view.K_depth, view.cam2world, S)
        bad = ok & _lookup(view.edge, uvd).any(axis=1)
        reasons["depth_edge"] = int(bad.sum())
        ok &= ~bad
    if view.depth is not None and view.K_depth is not None:
        uvd, zd = project(view.K_depth, view.cam2world, S[:, 0])
        hd_, wd_ = view.depth.shape
        x = np.clip(np.rint(uvd[:, 0]).astype(np.int64), 0, wd_ - 1)
        y = np.clip(np.rint(uvd[:, 1]).astype(np.int64), 0, hd_ - 1)
        d = view.depth[y, x]
        dv = view.depth_valid[y, x] if view.depth_valid is not None else np.isfinite(d)
        with np.errstate(invalid="ignore"):
            bad = ok & dv & np.isfinite(d) & (d < zd * (1 - occlusion_tol))
        reasons["occluded"] = int(bad.sum())
        ok &= ~bad
    reasons["kept"] = int(ok.sum())
    return ok, reasons


def filter_costs(costs: DataCosts, V: np.ndarray, F: np.ndarray, views: list[TexView], occlusion_tol: float,
                 exclude: tuple[int, ...] = ()) -> tuple[np.ndarray, dict[str, Any]]:
    """texrecon 데이터 비용에서 쓸 수 없는 (면, 시점) 항목을 뺀다. views 는 texrecon 시점 순서(.cam 이름순)와 같아야 한다.
    exclude 의 시점은 통째로 뺀다(한 카메라 빼고 굽기)."""
    if costs.views != len(views) or costs.faces != len(F):
        raise ValueError(f"데이터 비용 크기({costs.faces} 면, {costs.views} 시점)가 메시·시점({len(F)}, {len(views)})과 다르다")
    keep = np.zeros(len(costs.entries), bool)
    rec: dict[str, Any] = {}
    for i, view in enumerate(views):
        idx = np.nonzero(costs.entries["view"] == i)[0]
        if i in exclude:
            rec[view.name] = {"candidates": int(len(idx)), "excluded_view": True, "kept": 0}
            continue
        ok, reasons = texture_candidates(V, F, view, costs.entries["face"][idx].astype(np.int64), occlusion_tol)
        keep[idx[ok]] = True
        rec[view.name] = {"candidates": int(len(idx)), **reasons}
    return costs.entries[keep], rec


# --------------------------------------------------------------------------- 깊이 격자 보조


def discontinuity_mask(depth: np.ndarray, valid: np.ndarray, jump: float, cut: np.ndarray | None = None) -> np.ndarray:
    """깊이 경계 픽셀: 3×3 이웃(유효한 것만) 깊이 폭이 jump(상대)를 넘거나(run_hybrid_depth.edge_sharpness 와 같은 규칙), 깊이 모델이
    스스로 무효로 낸 픽셀(cut, 예: MapAnything mask_edges 로 지운 경계 띠)과 닿은 픽셀. 신뢰도 하위·사람 마스크로 뺀 픽셀은 cut 이
    아니다(사람은 사람 마스크가 따로 뺀다). 영상 테두리는 경계가 아니다."""
    d = np.where(valid, depth, np.nan).astype(np.float64)
    h, w = d.shape
    pad = np.pad(d, 1, mode="edge")
    cpad = np.pad(np.zeros((h, w), bool) if cut is None else cut.astype(bool), 1, mode="edge")
    lo = np.full((h, w), np.inf)
    hi = np.full((h, w), -np.inf)
    near_cut = np.zeros((h, w), bool)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            n = pad[dy:dy + h, dx:dx + w]
            near_cut |= cpad[dy:dy + h, dx:dx + w]
            lo = np.fmin(lo, n)
            hi = np.fmax(hi, n)
    with np.errstate(invalid="ignore"):
        jumpy = (hi - lo) > jump * lo
    return valid & (jumpy | near_cut)


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """정사각형(2r+1) 팽창. numpy 만."""
    if radius <= 0:
        return mask.copy()
    m = mask.astype(bool)
    out = m.copy()
    h, w = m.shape
    pad = np.pad(m, radius)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out |= pad[dy:dy + h, dx:dx + w]
    return out


# --------------------------------------------------------------------------- 메시 위상·구멍


def edge_table(F: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """면의 세 변(정렬된 정점 쌍)과 그 변의 면 번호. 반환 (변 (3·면 수, 2), 면 번호 (3·면 수))."""
    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    return e, np.tile(np.arange(len(F)), 3)


def face_pairs(F: np.ndarray) -> np.ndarray:
    """변을 공유하는 면 쌍. 비다양체 변(면 3개 이상)은 그 변의 면끼리 모두 잇는다."""
    e, fid = edge_table(F)
    key = e[:, 0] * (int(F.max()) + 1 if len(F) else 1) + e[:, 1]
    order = np.argsort(key, kind="stable")
    key, fid = key[order], fid[order]
    start = np.r_[0, np.nonzero(np.diff(key))[0] + 1] if len(key) else np.zeros(0, int)
    size = np.diff(np.r_[start, len(key)])
    two = start[size == 2]
    pairs = [np.column_stack([fid[two], fid[two + 1]])]
    for s, n in zip(start[size > 2], size[size > 2]):
        fs = fid[s:s + n]
        pairs.append(np.array([(fs[i], fs[j]) for i in range(n) for j in range(i + 1, n)], np.int64))
    return np.concatenate(pairs).astype(np.int64).reshape(-1, 2)


def components(n: int, pairs: np.ndarray, select: np.ndarray) -> np.ndarray:
    """select 인 면끼리만 이은 연결 성분 번호(선택 안 된 면은 −1)."""
    parent = np.arange(n)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    p = pairs[select[pairs[:, 0]] & select[pairs[:, 1]]]
    for a, b in p:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    roots = np.array([find(i) for i in range(n)])
    out = np.full(n, -1, np.int64)
    _, inv = np.unique(roots[select], return_inverse=True)
    out[select] = inv
    return out


def face_areas(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    return 0.5 * np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1)


def _point_segment_dist(P: np.ndarray, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """점 P(n, 3)에서 선분들(m)까지 최소 거리(n)."""
    AB = B - A
    L2 = np.maximum((AB * AB).sum(1), 1e-18)
    AP = P[:, None, :] - A[None]
    t = np.clip((AP * AB[None]).sum(2) / L2[None], 0, 1)
    D = AP - t[..., None] * AB[None]
    return np.sqrt((D * D).sum(2)).min(axis=1)


@dataclass(frozen=True)
class HoleRule:
    """작은 구멍(INTERPOLATED 후보) 기준. 모두 만족해야 한다(근거: 설계 문서 "정한 수치")."""

    max_area_m2: float = 0.0025  # 25 cm²
    max_inradius_m: float = 0.025  # 경계까지 최대 거리 2.5 cm(폭 약 5 cm)
    max_inradius_px: float = 8.0  # 둘레 관측 카메라의 원본 픽셀로도 8 px 이하
    max_vertices: int = TEXRECON_MAX_HOLE_VERTICES


def classify_holes(V: np.ndarray, F: np.ndarray, labels: np.ndarray, views: list[TexView], rule: HoleRule,
                   pairs: np.ndarray | None = None, unit_scale: float = 1.0) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """라벨 0(관측 없음) 면의 연결 성분마다 작은 구멍인지 판정한다. 반환 (면마다 작은 구멍 성분이면 True, 성분 기록).
    둘러싸이지 않은(메시 테두리에 닿은) 성분은 둘레 관측이 없으므로 크기와 상관없이 작은 구멍이 아니다.
    unit_scale = 메시 단위 1 의 미터(World 닮음 변환의 축척). 미터 기준(면적 s², 길이 s)에만 곱하고 픽셀 반경은 단위가 없어 그대로다."""
    pairs = face_pairs(F) if pairs is None else pairs
    unseen = np.asarray(labels) == 0
    comp = components(len(F), pairs, unseen)
    eligible = np.zeros(len(F), bool)
    if not unseen.any():
        return eligible, []
    ncomp = int(comp.max()) + 1
    areas = face_areas(V, F)
    e, fid = edge_table(F)
    key = e[:, 0] * (int(F.max()) + 1) + e[:, 1]
    uk, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    n_seen = np.bincount(inv, weights=(~unseen[fid]).astype(float), minlength=len(uk)).astype(int)
    n_unseen = cnt - n_seen
    # 경계 변: 관측 없는 면이 하나뿐인 변(그 면의 성분 경계). 라벨 있는 면이 없으면 열린 테두리(메시 끝)
    row_b = unseen[fid] & (n_unseen[inv] == 1)
    row_comp = comp[fid]
    n_bound = np.bincount(row_comp[row_b], minlength=ncomp)
    n_open = np.bincount(row_comp[row_b & (n_seen[inv] == 0)], minlength=ncomp)
    c_area = np.bincount(comp[unseen], weights=areas[unseen], minlength=ncomp) * unit_scale ** 2
    c_faces = np.bincount(comp[unseen], minlength=ncomp)
    fv = np.nonzero(unseen)[0]
    cv = np.unique(np.column_stack([np.repeat(comp[fv], 3), F[fv].reshape(-1)]), axis=0)
    c_verts = np.bincount(cv[:, 0], minlength=ncomp)
    enclosed = (n_bound > 0) & (n_open == 0)
    small = enclosed & (c_area <= rule.max_area_m2 * (1 + _EPS)) & (c_verts <= rule.max_vertices)
    labels = np.asarray(labels)
    seen_rows = ~unseen[fid]
    edge_label = np.zeros(len(uk), np.int64)  # 변에 붙은 라벨 있는 면의 라벨(여럿이면 가장 큰 번호, 카메라 픽셀 반경에만 쓴다)
    np.maximum.at(edge_label, inv[seen_rows], labels[fid[seen_rows]])
    f_order = np.argsort(comp, kind="stable")
    f_start = np.searchsorted(comp[f_order], np.arange(ncomp + 1))
    b_rows = np.nonzero(row_b)[0]
    b_order = b_rows[np.argsort(row_comp[b_rows], kind="stable")]
    b_start = np.searchsorted(row_comp[b_order], np.arange(ncomp + 1))
    records = []
    for c in range(ncomp):
        rec: dict[str, Any] = {"component": c, "faces": int(c_faces[c]), "vertices": int(c_verts[c]),
                               "area_m2": float(c_area[c]), "enclosed": bool(enclosed[c]),
                               "inradius_m": None, "inradius_px": None, "eligible": False}
        if small[c]:
            faces = f_order[f_start[c]:f_start[c + 1]]
            brow = b_order[b_start[c]:b_start[c + 1]]
            A, B = V[e[brow, 0]], V[e[brow, 1]]
            P = np.concatenate([V[np.unique(F[faces])], V[F[faces]].mean(axis=1)])
            r_u = float(_point_segment_dist(P, A, B).max())  # 메시 단위
            r_m = r_u * unit_scale
            # 둘레 관측 면(경계 변의 라벨 있는 면)의 카메라들에서 잰 픽셀 반경(가장 큰 값 = 보수적)
            cams = np.unique(edge_label[inv[brow]])
            ctr = V[F[faces]].reshape(-1, 3).mean(axis=0)
            pxs = []
            for k in cams[cams > 0] - 1:
                _, z = project(views[k].K, views[k].cam2world, ctr[None])
                if z[0] > 0:
                    pxs.append(float(views[k].K[0, 0] * r_u / z[0]))
            r_px = max(pxs) if pxs else None
            ok = r_m <= rule.max_inradius_m * (1 + _EPS) and r_px is not None and r_px <= rule.max_inradius_px * (1 + _EPS)
            rec.update({"inradius_m": round(r_m, 5), "inradius_px": None if r_px is None else round(r_px, 2), "eligible": bool(ok)})
            if ok:
                eligible[faces] = True
        records.append(rec)
    return eligible, records


def face_provenance(labels: np.ndarray, eligible: np.ndarray, present: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """면 출처(0/1/2)와 관측 카메라(시점 번호, 관측이 아니면 −1). present = 최종 텍스처 메시에 실제로 들어간 면.
    라벨이 있어도 텍스처에 없으면 미관측, 작은 구멍이어도 texrecon 이 채우지 못했으면(present 아님) 미관측이다."""
    labels = np.asarray(labels)
    cls = np.full(len(labels), UNOBSERVED, np.uint8)
    cam = np.full(len(labels), -1, np.int64)
    obs = (labels > 0) & present
    cls[obs] = OBSERVED
    cam[obs] = labels[obs] - 1
    cls[(labels == 0) & eligible & present] = INTERPOLATED
    return cls, cam


# --------------------------------------------------------------------------- 텍셀 출처 지도


def rasterize_uv(tri_px: np.ndarray, values: np.ndarray, hw: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """UV 삼각형(픽셀 좌표, 정수 = 텍셀 중심)을 칠한다. 텍셀 중심이 삼각형 안(경계 포함)이면 그 면 값.
    values 는 (삼각형,) 또는 (삼각형, 채널) uint8. 반환 (겹치면 가장 낮은 값, 가장 높은 값, 칠한 텍셀), 칠하지 않은 텍셀 값은 0.
    같은 상자 크기의 삼각형끼리 묶어 한 번에 계산한다."""
    h, w = int(hw[0]), int(hw[1])
    T = np.asarray(tri_px, float).reshape(-1, 3, 2)
    vals = np.asarray(values).reshape(len(T), -1).astype(np.uint8)
    C = vals.shape[1]
    lo = np.full((C, h * w), _UNSET, np.uint8)
    hi = np.zeros((C, h * w), np.uint8)
    cov = np.zeros(h * w, bool)
    if not len(T):
        return lo.reshape(C, h, w)[0] * 0, hi.reshape(C, h, w)[0], cov.reshape(h, w)
    x0 = np.clip(np.floor(T[:, :, 0].min(1)).astype(np.int64), 0, w - 1)
    x1 = np.clip(np.ceil(T[:, :, 0].max(1)).astype(np.int64), 0, w - 1)
    y0 = np.clip(np.floor(T[:, :, 1].min(1)).astype(np.int64), 0, h - 1)
    y1 = np.clip(np.ceil(T[:, :, 1].max(1)).astype(np.int64), 0, h - 1)
    (ax, ay), (bx, by), (cx, cy) = (T[:, 0, 0], T[:, 0, 1]), (T[:, 1, 0], T[:, 1, 1]), (T[:, 2, 0], T[:, 2, 1])
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    ok = np.abs(den) > 1e-12
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    keys, group = np.unique(np.column_stack([bh, bw])[ok], axis=0, return_inverse=True)
    tri_ok = np.nonzero(ok)[0]
    for g, (gh, gw) in enumerate(keys):
        idx = tri_ok[group.reshape(-1) == g]
        oy, ox = np.mgrid[0:gh, 0:gw]
        xs = x0[idx, None, None] + ox[None]
        ys = y0[idx, None, None] + oy[None]
        d = den[idx, None, None]
        l1 = ((by[idx, None, None] - cy[idx, None, None]) * (xs - cx[idx, None, None])
              + (cx[idx, None, None] - bx[idx, None, None]) * (ys - cy[idx, None, None])) / d
        l2 = ((cy[idx, None, None] - ay[idx, None, None]) * (xs - cx[idx, None, None])
              + (ax[idx, None, None] - cx[idx, None, None]) * (ys - cy[idx, None, None])) / d
        inside = (l1 >= -1e-9) & (l2 >= -1e-9) & (1 - l1 - l2 >= -1e-9) & (xs < w) & (ys < h)
        if not inside.any():
            continue
        flat = (ys * w + xs)[inside]
        owner = np.broadcast_to(idx[:, None, None], inside.shape)[inside]
        cov[flat] = True
        for c in range(C):
            np.minimum.at(lo[c], flat, vals[owner, c])
            np.maximum.at(hi[c], flat, vals[owner, c])
    lo[lo == _UNSET] = 0
    lo, hi = lo.reshape(C, h, w), hi.reshape(C, h, w)
    if np.asarray(values).ndim == 1:
        return lo[0], hi[0], cov.reshape(h, w)
    return lo, hi, cov.reshape(h, w)


def obj_uv_to_px(uv: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    """OBJ vt(v 위쪽) → 아틀라스 픽셀(정수 = 텍셀 중심, texrecon 규약: 텍스처 좌표 = 패치 픽셀 좌표 / 크기)."""
    h, w = hw
    return np.stack([uv[..., 0] * w, (1.0 - uv[..., 1]) * h], axis=-1)


def provenance_maps(Ft_uv: np.ndarray, cls: np.ndarray, cam: np.ndarray, hw: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """한 아틀라스의 면들(Ft_uv: (면, 3, 2) OBJ vt)로 출처 지도(0/1/2)·관측 카메라 지도(시점 번호 + 1, 모르면 0)·칠한 텍셀을 만든다.
    카메라 지도는 출처가 OBSERVED 이고 겹친 면들의 카메라가 하나일 때만 값이 있다."""
    tri = obj_uv_to_px(Ft_uv, hw)
    camv = np.where(cls == OBSERVED, cam + 1, 0)
    lo, hi, cov = rasterize_uv(tri, np.column_stack([cls, camv]).astype(np.uint8), hw)
    src = lo[0]
    cam_map = np.where((src == OBSERVED) & (lo[1] == hi[1]), lo[1], 0).astype(np.uint8)
    return src.astype(np.uint8), cam_map, cov


def area_ratios(V: np.ndarray, F: np.ndarray, cls: np.ndarray) -> dict[str, float]:
    a = face_areas(V, F)
    tot = float(a.sum()) or 1.0
    return {k: round(float(a[cls == v].sum()) / tot, 5) for k, v in SURFACE_VALUES.items()}


def texel_ratios(maps: list[np.ndarray], covered: list[np.ndarray]) -> dict[str, float | None]:
    """칠한 텍셀(covered) 안에서의 비율. 아틀라스 여백은 세지 않는다."""
    n = sum(int(c.sum()) for c in covered)
    if not n:
        return {k: None for k in SURFACE_VALUES}
    return {k: round(sum(int(((m == v) & c).sum()) for m, c in zip(maps, covered)) / n, 5) for k, v in SURFACE_VALUES.items()}


# --------------------------------------------------------------------------- 렌더·지표


def pinhole_rays(K: np.ndarray, cam2world: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    """픽셀 중심 광선(H, W, 6): 원점 3 + 방향 3(단위 아님, 깊이 z = 1 기준)."""
    h, w = hw
    v, u = np.mgrid[0:h, 0:w].astype(float)
    d = np.stack([u, v, np.ones_like(u)], -1) @ np.linalg.inv(np.asarray(K, float)).T
    T = np.asarray(cam2world, float)
    dirs = d @ T[:3, :3].T
    org = np.broadcast_to(T[:3, 3], dirs.shape)
    return np.concatenate([org, dirs], -1)


def bilinear(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    x = np.clip(x, 0, w - 1)
    y = np.clip(y, 0, h - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    im = img.astype(np.float64)
    if im.ndim == 2:
        im = im[..., None]
    out = (im[y0, x0] * (1 - fx) * (1 - fy) + im[y0, x1] * fx * (1 - fy) + im[y1, x0] * (1 - fx) * fy + im[y1, x1] * fx * fy)
    return out


def gaussian_blur(x: np.ndarray, sigma: float) -> np.ndarray:
    """분리 가우스 흐림(반경 3σ, 가장자리 반사). (H, W) 또는 (H, W, C), float64."""
    r = max(int(np.ceil(3 * sigma)), 1)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    y = np.asarray(x, np.float64)
    for axis in (0, 1):
        pad = [(0, 0)] * y.ndim
        pad[axis] = (r, r)
        win = np.lib.stride_tricks.sliding_window_view(np.pad(y, pad, mode="reflect"), 2 * r + 1, axis=axis)
        y = win @ k
    return y


def masked_blur(x: np.ndarray, mask: np.ndarray, sigma: float) -> np.ndarray:
    """정규화 합성곱: mask 안 픽셀만 가중 평균한 흐림(mask 밖 색이 섞이지 않는다). mask 밖·가중치 0 은 0."""
    m = mask.astype(np.float64)
    xm = np.asarray(x, np.float64) * (m[..., None] if np.ndim(x) == 3 else m)
    num, den = gaussian_blur(xm, sigma), gaussian_blur(m, sigma)
    if num.ndim == 3:
        den = den[..., None]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 1e-12, num / den, 0.0)


def psnr_blurred(a: np.ndarray, b: np.ndarray, mask: np.ndarray, sigma: float) -> float | None:
    """두 그림을 mask 안에서만 정규화 흐림한 뒤 mask 안 PSNR(위치 어긋남과 색 차이를 가르는 보조 지표)."""
    if not mask.any():
        return None
    return psnr(masked_blur(a, mask, sigma), masked_blur(b, mask, sigma), mask)


def psnr(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float | None:
    if not mask.any():
        return None
    d = (a[mask].astype(np.float64) - b[mask].astype(np.float64)) ** 2
    mse = float(d.mean())
    return float("inf") if mse == 0 else round(10 * np.log10(255.0 ** 2 / mse), 3)


def _box(x: np.ndarray, r: int) -> np.ndarray:
    """(2r+1)² 상자 평균(가장자리는 있는 칸만)."""
    c = np.pad(x, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    h, w = x.shape
    y0 = np.clip(np.arange(h) - r, 0, h)
    y1 = np.clip(np.arange(h) + r + 1, 0, h)
    x0 = np.clip(np.arange(w) - r, 0, w)
    x1 = np.clip(np.arange(w) + r + 1, 0, w)
    s = c[y1][:, x1] - c[y0][:, x1] - c[y1][:, x0] + c[y0][:, x0]
    n = (y1 - y0)[:, None] * (x1 - x0)[None]
    return s / n


def ssim(a: np.ndarray, b: np.ndarray, mask: np.ndarray, r: int = 3) -> float | None:
    """밝기(BT.601) SSIM, 7×7 상자 창, mask 안 픽셀 평균. 창이 mask 밖을 섞지 않도록 mask 밖은 두 영상 모두 같은 값(0)으로 둔다."""
    if not mask.any():
        return None

    def lum(x: np.ndarray) -> np.ndarray:
        x = x.astype(np.float64)
        return x if x.ndim == 2 else x[..., 0] * 0.299 + x[..., 1] * 0.587 + x[..., 2] * 0.114

    A, B = np.where(mask, lum(a), 0.0), np.where(mask, lum(b), 0.0)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = _box(A, r), _box(B, r)
    va, vb = _box(A * A, r) - ma * ma, _box(B * B, r) - mb * mb
    cov = _box(A * B, r) - ma * mb
    s = ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))
    inner = mask & ~dilate(~mask, r)  # 창 전체가 mask 안인 픽셀만
    return round(float(s[inner].mean()), 4) if inner.any() else None
