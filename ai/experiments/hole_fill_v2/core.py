"""S15P31A101-532 핵심 순수 함수 모듈.

구성:
1. 색 공간 변환 (`srgb_to_oklab`, `srgb_to_linear`, `linear_to_srgb`)
2. 다중 카메라 외톨이 제거 및 정면성·가장자리 가중 블렌딩 (`select_camera_colors_with_outlier_removal`)
   - 후보 3대 이상: Oklab 색 거리 합이 최소인 메도이드(medoid) 카메라를 기준으로 선택
   - 후보 2대: 이웃 관측 텍셀 중앙색(`neighbor_median_srgb`)을 세 번째 표(3rd vote)로 사용해 메도이드 결정
   - 후보 1대: 바닥 면에서 이웃 관측 중앙색과 과도하게 다른 어두운 외톨이(의자 다리 그림자 등) 감쇠
3. 위상 정점 용접(1 mm) 기반 열린 구멍 경계 거리 계산 (`compute_open_boundary_distances`)
4. 작은 연결 성분(떠다니는 조각) 분석 및 제거 (`filter_small_connected_components`, `sweep_connected_components`)
5. 미관측 텍셀 채우기 변형 (`inpaint_tone_plane`, `feather_observed_filled_boundary`, `compute_surface_source_summary`)
6. 메시 구멍 경계(8 cm) UV 아틀라스 텍셀 및 `COLOR_0` 정점색 소프트 블렌딩 (`blend_atlas_at_hole_boundary`, `blend_unobserved_vertex_colors`)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import scipy.ndimage as ndi
from scipy.spatial import cKDTree

from simai_ai.scene.glb_enhance import linear_to_srgb, srgb_to_linear
from simai_ai.scene.glb_fill import SRC_GRAY, SRC_PROJECTED
from simai_ai.scene.texture_provenance import GENERATED, gaussian_blur

SRC_FILLED = GENERATED  # 4: 미관측 영역 채움 텍셀 (tone / lama / classic)

# Oklab 변환 행렬 (선형 sRGB -> LMS -> Lab)
_OKLAB_M1 = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ],
    dtype=np.float64,
)
_OKLAB_M2 = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ],
    dtype=np.float64,
)


def smoothstep(edge0: float, edge1: float, x: np.ndarray | float) -> np.ndarray:
    """표준 Hermite smoothstep 보간."""
    span = max(float(edge1 - edge0), 1e-12)
    t = np.clip((np.asarray(x, dtype=np.float64) - float(edge0)) / span, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def srgb_to_oklab(srgb: np.ndarray) -> np.ndarray:
    """sRGB `[0, 1]` 배열 `(..., 3)` 을 지각적 균일 색공간 Oklab `(..., 3)` 으로 변환한다."""
    lin = srgb_to_linear(np.clip(np.asarray(srgb, dtype=np.float64), 0.0, 1.0))
    lms = np.maximum(lin @ _OKLAB_M1.T, 0.0) ** (1.0 / 3.0)
    return lms @ _OKLAB_M2.T


def compute_neighbor_observed_median_srgb(
    cand_mask: np.ndarray,
    cand_srgb: np.ndarray,
    cand_geom_w: np.ndarray,
    hw: tuple[int, int],
    fallback_srgb: np.ndarray,
    filter_size_px: int = 21,
) -> np.ndarray:
    """관측 후보가 있는 텍셀들의 초기 가중 평균색으로부터 이웃 관측 텍셀 중앙/평균 기준색 `(N, 3)` 을 구한다."""
    h, w = hw
    n_pts = h * w
    fb = np.clip(np.asarray(fallback_srgb, dtype=np.float64).reshape(3), 0.0, 1.0)
    if cand_mask.shape[1] != n_pts:
        raise ValueError(f"cand_mask 모양 {cand_mask.shape} 이 hw={hw} 와 맞지 않는다")

    w_eff = np.where(cand_mask, np.maximum(cand_geom_w, 1e-6), 0.0)
    w_sum = w_eff.sum(axis=0)
    has_any = w_sum > 0

    init_srgb = np.tile(fb, (n_pts, 1))
    if has_any.any():
        # 후보 카메라들의 초기 중앙값(median)에 가까운 가중 색
        med_c = np.zeros((n_pts, 3), dtype=np.float64)
        for ch in range(3):
            vals = np.where(cand_mask[:, has_any], cand_srgb[:, has_any, ch], np.nan)
            med_c[has_any, ch] = np.nanmedian(vals, axis=0)
        init_srgb[has_any] = med_c[has_any]

    # 관측 마스크 안에서만 정규화 가우스 블러를 수행해 이웃 관측 텍셀 대표색을 구한다
    obs_2d = has_any.reshape(h, w).astype(np.float64)
    img_2d = init_srgb.reshape(h, w, 3)
    sigma = max(2.0, float(filter_size_px) * 0.5)
    num = ndi.gaussian_filter(img_2d * obs_2d[:, :, None], sigma=(sigma, sigma, 0.0))
    den = ndi.gaussian_filter(obs_2d, sigma=sigma)[:, :, None]
    out_2d = np.where(den > 1e-5, num / np.maximum(den, 1e-8), fb[None, None, :])
    return np.clip(out_2d.reshape(n_pts, 3), 0.0, 1.0)


def select_camera_colors_with_outlier_removal(
    cand_mask: np.ndarray,
    cand_srgb: np.ndarray,
    cand_geom_w: np.ndarray,
    neighbor_median_srgb: np.ndarray,
    *,
    sigma_oklab: float = 0.056,
    is_floor: bool = True,
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """다중 카메라 후보 색상에서 외톨이(의자 다리·테이블 잔상 등)를 제거하고 가중 블렌딩한다.

    매개변수:
    - `cand_mask`: `(C, N)` bool — 기하·화면·법선·깊이(5 cm 일치) 게이트를 통과한 카메라 후보 마스크
    - `cand_srgb`: `(C, N, 3)` float64 `[0, 1]` — 노출 보정된 카메라별 샘플 색상
    - `cand_geom_w`: `(C, N)` float64 — 카메라별 정면성·가장자리·깊이·거리 가중치
    - `neighbor_median_srgb`: `(N, 3)` float64 `[0, 1]` — 2대 후보일 때 세 번째 표(3rd vote)로 쓰는 이웃 관측 텍셀 중앙색
    - `sigma_oklab`: Oklab 색 거리 표준편차 $\\sigma$ (12차 보정 겹침 인라이어 p75=0.0506, 전체 쌍 p50=0.0542 근거 0.056)

    반환:
    - `blended_srgb`: `(N, 3)` float64 `[0, 1]`
    - `observed_mask`: `(N,)` bool
    - `stats`: 후보 수 및 외톨이 감쇠 통계
    """
    cand_mask = np.asarray(cand_mask, dtype=bool)
    cand_srgb = np.clip(np.asarray(cand_srgb, dtype=np.float64), 0.0, 1.0)
    cand_geom_w = np.where(cand_mask, np.maximum(np.asarray(cand_geom_w, dtype=np.float64), 0.0), 0.0)
    num_cams, n_pts = cand_mask.shape

    neigh_srgb = np.clip(np.asarray(neighbor_median_srgb, dtype=np.float64), 0.0, 1.0)
    if neigh_srgb.ndim == 1:
        neigh_srgb = np.tile(neigh_srgb.reshape(1, 3), (n_pts, 1))

    n_cand = cand_mask.sum(axis=0)
    has_obs = n_cand >= 1
    out_srgb = neigh_srgb.copy()
    if not has_obs.any():
        return out_srgb, np.zeros(n_pts, dtype=bool), {"obs_texels": 0, "cand_3plus": 0, "cand_2": 0, "cand_1": 0, "outliers_suppressed": 0}

    # 관측 후보가 있는 텍셀만 슬라이싱해 벡터화 연산
    obs_idx = np.flatnonzero(has_obs)
    m_sub = cand_mask[:, obs_idx]  # (C, M)
    c_sub = cand_srgb[:, obs_idx, :]  # (C, M, 3)
    w_sub = cand_geom_w[:, obs_idx]  # (C, M)
    nb_sub = neigh_srgb[obs_idx]  # (M, 3)
    nc_sub = n_cand[obs_idx]  # (M,)

    lab_sub = srgb_to_oklab(c_sub)  # (C, M, 3)
    nb_lab = srgb_to_oklab(nb_sub)  # (M, 3)

    sigma_eff = float(sigma_oklab) if is_floor else float(sigma_oklab) * 2.25
    ref_lab = nb_lab.copy()

    # 쌍별 Oklab 색 거리 행렬: dmat[i, j, m] = ||lab_sub[i, m] - lab_sub[j, m]||_2
    diff_cc = lab_sub[:, None, :, :] - lab_sub[None, :, :, :]  # (C, C, M, 3)
    dmat = np.linalg.norm(diff_cc, axis=-1)  # (C, C, M)
    valid_pair = m_sub[:, None, :] & m_sub[None, :, :]
    dmat = np.where(valid_pair, dmat, 0.0)

    # 각 후보와 이웃 관측 중앙색(3rd vote) 사이의 Oklab 거리: d_nb[i, m]
    d_nb = np.linalg.norm(lab_sub - nb_lab[None, :, :], axis=-1)  # (C, M)

    # (1) 후보 3대 이상: 다른 후보들과의 색 거리 합(+ 미세 타이브레이커)이 최소인 메도이드 선택
    sel_3 = nc_sub >= 3
    if sel_3.any():
        tie_w = 0.35 if is_floor else 0.10
        score_3 = dmat[:, :, sel_3].sum(axis=1) + tie_w * d_nb[:, sel_3]
        score_3 = np.where(m_sub[:, sel_3], score_3, 1e9)
        best_k3 = np.argmin(score_3, axis=0)
        m3_indices = np.flatnonzero(sel_3)
        ref_lab[m3_indices] = lab_sub[best_k3, m3_indices, :]

    # (2) 후보 2대: 이웃 관측 텍셀 중앙색(nb_lab)을 세 번째 표(3rd vote)로 넣어 메도이드 선택
    #     2대 후보 (k1, k2) 사이의 거리 d(k1, k2) 는 대칭이므로, 3표 집합 {k1, k2, nb} 에서
    #     거리 합 S(k_i) = d(k1, k2) + d(k_i, nb) 를 최소화하는 후보는 곧 d(k_i, nb) 가 더 작은 후보!
    sel_2 = nc_sub == 2
    if sel_2.any():
        score_2 = dmat[:, :, sel_2].sum(axis=1) + d_nb[:, sel_2]
        score_2 = np.where(m_sub[:, sel_2], score_2, 1e9)
        best_k2 = np.argmin(score_2, axis=0)
        m2_indices = np.flatnonzero(sel_2)
        ref_lab[m2_indices] = lab_sub[best_k2, m2_indices, :]

    # (3) 후보 1대: 유일한 후보를 기준으로 두되, 바닥에서 이웃 중앙색보다 지나치게 어두운 그림자/다리 잔상은 기준을 보정
    sel_1 = nc_sub == 1
    if sel_1.any():
        best_k1 = np.argmax(m_sub[:, sel_1], axis=0)
        m1_indices = np.flatnonzero(sel_1)
        cand1_lab = lab_sub[best_k1, m1_indices, :]
        if is_floor:
            # 바닥 관측 중앙색보다 밝기(L)가 크게 어두운 외톨이(의자 다리/밑 그림자)는 기준색으로 끌어올림
            dark_diff = nb_lab[m1_indices, 0] - cand1_lab[:, 0]
            dark_w = smoothstep(0.045, 0.110, dark_diff)[:, None]
            ref_lab[m1_indices] = cand1_lab * (1.0 - dark_w * 0.85) + nb_lab[m1_indices] * (dark_w * 0.85)
        else:
            ref_lab[m1_indices] = cand1_lab

    # 기준색(ref_lab)과의 정규화 색 거리 dn = ||lab - ref_lab|| / sigma_eff 로 외톨이 가중치 감쇠
    dist_to_ref = np.linalg.norm(lab_sub - ref_lab[None, :, :], axis=-1)  # (C, M)
    dn = dist_to_ref / max(sigma_eff, 1e-6)
    gate_hi = 1.65 if is_floor else 2.60
    gate_lo = 1.10 if is_floor else 1.75
    g_att = 1.0 - smoothstep(gate_lo, gate_hi, dn)
    att = np.exp(-(dn * dn)) * (g_att * g_att)
    att = np.where(m_sub, att, 0.0)

    outliers_suppressed = int(((m_sub) & (att < 0.25)).sum())

    w_final = w_sub * att
    w_sum = w_final.sum(axis=0)
    # 모든 후보가 감쇠된 경우 가장 높은 기하×감쇠 후보 최소 보존 또는 이웃 중앙색과 블렌딩
    fallback_w = np.where(m_sub, w_sub * np.maximum(att, 1e-3), 0.0)
    use_w = np.where(w_sum[None, :] > 1e-7, w_final, fallback_w)
    use_sum = np.maximum(use_w.sum(axis=0), 1e-12)

    blended_sub = (use_w[:, :, None] * c_sub).sum(axis=0) / use_sum[:, None]

    # 1대 후보 외톨이가 강하게 감쇠된 바닥 텍셀은 이웃 관측 중앙색으로 부드럽게 수렴
    max_att = att.max(axis=0)
    weak_single = (nc_sub == 1) & (max_att < 0.5)
    if weak_single.any():
        alpha_keep = np.clip(max_att[weak_single] * 2.0, 0.15, 1.0)[:, None]
        blended_sub[weak_single] = blended_sub[weak_single] * alpha_keep + nb_sub[weak_single] * (1.0 - alpha_keep)

    out_srgb[obs_idx] = np.clip(blended_sub, 0.0, 1.0)
    stats = {
        "obs_texels": int(obs_idx.size),
        "cand_3plus": int(sel_3.sum()),
        "cand_2": int(sel_2.sum()),
        "cand_1": int(sel_1.sum()),
        "outliers_suppressed": outliers_suppressed,
    }
    return out_srgb, has_obs, stats


def compute_open_boundary_distances(
    V: np.ndarray,
    F: np.ndarray,
    *,
    weld_tol_m: float = 0.001,
    max_dist_m: float = 0.12,
) -> tuple[np.ndarray, dict[str, int]]:
    """정점들을 `weld_tol_m`(기본 1 mm) 격자로 위상 용접한 뒤 열린 구멍 경계까지의 유클리드 거리 `(len(V),)` 를 반환한다.

    근거:
    - UV 아틀라스 경계를 따라 복제된 정점들을 먼저 1 mm 격자로 합쳐야 가짜 UV 이음매가 아닌
      진짜 TSDF 기하 구멍 테두리(공유 삼각형 수가 1개인 간선)만 정확히 검출된다.
    """
    V = np.asarray(V, dtype=np.float64)
    F = np.asarray(F, dtype=np.int64)
    if len(V) == 0 or len(F) == 0:
        return np.full(len(V), max_dist_m, dtype=np.float64), {"open_edges": 0, "boundary_vertices": 0}

    key = np.round(V / max(weld_tol_m, 1e-6)).astype(np.int64)
    _, first_idx, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    welded_V = V[first_idx]
    Fw = inv.reshape(-1)[F]

    # 퇴화 삼각형 제외
    non_deg = (Fw[:, 0] != Fw[:, 1]) & (Fw[:, 1] != Fw[:, 2]) & (Fw[:, 2] != Fw[:, 0])
    Fw = Fw[non_deg]
    if len(Fw) == 0:
        return np.full(len(V), max_dist_m, dtype=np.float64), {"open_edges": 0, "boundary_vertices": 0}

    e01 = np.sort(Fw[:, [0, 1]], axis=1)
    e12 = np.sort(Fw[:, [1, 2]], axis=1)
    e20 = np.sort(Fw[:, [2, 0]], axis=1)
    all_edges = np.concatenate([e01, e12, e20], axis=0)

    n_w = len(welded_V)
    edge_hash = all_edges[:, 0] * n_w + all_edges[:, 1]
    u_hash, edge_first, counts = np.unique(edge_hash, return_index=True, return_counts=True)
    open_mask = counts == 1
    open_edges = all_edges[edge_first[open_mask]]

    if len(open_edges) == 0:
        return np.full(len(V), max_dist_m, dtype=np.float64), {"open_edges": 0, "boundary_vertices": 0}

    b_verts = np.unique(open_edges.reshape(-1))
    pa = welded_V[open_edges[:, 0]]
    pb = welded_V[open_edges[:, 1]]
    mid_pts = 0.5 * (pa + pb)
    sample_pts = np.concatenate([welded_V[b_verts], mid_pts], axis=0)

    tree = cKDTree(sample_pts)
    dists, _ = tree.query(welded_V, k=1, distance_upper_bound=max_dist_m)
    dists = np.where(np.isfinite(dists), dists, max_dist_m)
    dists[b_verts] = 0.0

    vertex_dists = dists[inv.reshape(-1)]
    return vertex_dists.astype(np.float64), {
        "open_edges": int(len(open_edges)),
        "boundary_vertices": int(len(b_verts)),
        "welded_vertices": int(n_w),
    }


def filter_small_connected_components(
    V: np.ndarray,
    F: np.ndarray,
    *,
    min_faces: int = 300,
    min_area_m2: float = 0.25,
    weld_tol_m: float = 0.001,
) -> tuple[np.ndarray, dict[str, Any]]:
    """위상 용접 정점 공유 기준 연결 성분을 분석해 `faces < min_faces` 또는 `area < min_area_m2` 인 떠다니는 조각을 찾는다.

    반환:
    - `keep_face_mask`: `(len(F),)` bool (True = 유지할 면)
    - `report`: 성분 통계 및 제거된 면/면적 비율
    """
    V = np.asarray(V, dtype=np.float64)
    F = np.asarray(F, dtype=np.int64)
    m = len(F)
    if m == 0:
        return np.zeros(0, dtype=bool), {"total_components": 0, "removed_components": 0, "removed_faces": 0, "removed_face_ratio": 0.0, "removed_area_m2": 0.0, "removed_area_ratio": 0.0}

    key = np.round(V / max(weld_tol_m, 1e-6)).astype(np.int64)
    _, first_idx, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    Fw = inv.reshape(-1)[F]
    n_w = len(first_idx)

    parent = np.arange(n_w, dtype=np.int64)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = int(parent[x])
        return x

    for a, b, c in Fw:
        ra, rb, rc = find(int(a)), find(int(b)), find(int(c))
        root = min(ra, rb, rc)
        parent[ra] = parent[rb] = parent[rc] = root

    roots = np.array([find(i) for i in range(n_w)], dtype=np.int64)
    f_root = roots[Fw[:, 0]]
    u_roots, comp_id = np.unique(f_root, return_inverse=True)
    comp_id = comp_id.reshape(-1)

    v0, v1, v2 = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    areas = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    total_area = float(areas.sum())

    comp_faces = np.bincount(comp_id, minlength=len(u_roots))
    comp_areas = np.bincount(comp_id, weights=areas, minlength=len(u_roots))

    drop_comp = (comp_faces < int(min_faces)) | (comp_areas < float(min_area_m2))
    # 가장 큰 주 성분은 항상 보존
    if len(u_roots) > 0:
        largest_c = int(np.argmax(comp_areas))
        drop_comp[largest_c] = False

    drop_face = drop_comp[comp_id]
    keep_face = ~drop_face
    removed_area = float(areas[drop_face].sum())

    return keep_face, {
        "min_faces": int(min_faces),
        "min_area_m2": float(min_area_m2),
        "total_components": int(len(u_roots)),
        "removed_components": int(drop_comp.sum()),
        "kept_components": int((~drop_comp).sum()),
        "total_faces": int(m),
        "removed_faces": int(drop_face.sum()),
        "removed_face_ratio": round(float(drop_face.mean()), 5),
        "total_area_m2": round(total_area, 3),
        "removed_area_m2": round(removed_area, 3),
        "removed_area_ratio": round(removed_area / max(total_area, 1e-12), 5),
    }


def inpaint_tone_plane(
    albedo: np.ndarray,
    inpaint_mask: np.ndarray,
    surface_source: np.ndarray | None = None,
    *,
    texel_m: float = 0.005,
    with_grid: bool = False,
    seed: int = 42,
) -> np.ndarray:
    """변형 (a) `tone`: 평면 관측 텍셀 평균/중앙 톤 + 아주 약한 저주파 얼룩 (+ 선택적 1m/0.5m 격자).

    - 관측 영역(`inpaint_mask == 0`)은 그대로 두고, 미관측 영역(`inpaint_mask > 0`)을
      해당 평면 관측 텍셀의 대표 톤과 완만한 조명 그라데이션 + 미세 저주파 질감으로 채운다.
    """
    albedo_u8 = np.ascontiguousarray(albedo, dtype=np.uint8)
    h, w = albedo_u8.shape[:2]
    target = inpaint_mask > 0
    if not target.any():
        return albedo_u8.copy()

    if surface_source is not None and ((inpaint_mask == 0) & (surface_source == SRC_PROJECTED)).any():
        obs = (inpaint_mask == 0) & (surface_source == SRC_PROJECTED)
    else:
        obs = inpaint_mask == 0

    if obs.any():
        obs_pixels = albedo_u8[obs].astype(np.float64)
        # 상·하위 극단 이상치(반사광, 짙은 그림자)를 제외한 로버스트 평균/중앙 혼합 톤
        med_rgb = np.median(obs_pixels, axis=0)
        mean_rgb = np.mean(obs_pixels, axis=0)
        base_tone = 0.65 * med_rgb + 0.35 * mean_rgb

        # 관측 영역의 거시적 조명 분포를 최근접 + 대구경 가우스 블러로 추출해 구멍 경계 단차를 줄인다
        dist_px, (iy, ix) = ndi.distance_transform_edt(~obs, return_indices=True)
        nearest_rgb = albedo_u8[iy, ix].astype(np.float64)
        sigma_macro = max(16.0, 0.45 / max(texel_m, 1e-4))
        local_field = gaussian_blur(nearest_rgb, min(sigma_macro, 48.0))
        # 경계 근처에서는 local_field 비중을 높이고 깊은 구멍 내부로 갈수록 평면 평균 톤(base_tone)으로 수렴
        decay_px = max(12.0, 0.55 / max(texel_m, 1e-4))
        w_global = np.clip(dist_px / decay_px, 0.15, 0.82)[:, :, None]
        tone_img = local_field * (1.0 - w_global) + base_tone[None, None, :] * w_global
    else:
        base_tone = np.array([110.0, 116.0, 126.0], dtype=np.float64)
        tone_img = np.tile(base_tone[None, None, :], (h, w, 1))

    # 아주 약한 다중 스케일 저주파 얼룩(mottle, ±2.2 RGB 이내)을 더해 플라스틱 같은 인위적 단색 느낌을 없앤다
    rng = np.random.default_rng(seed)
    coarse_h = max(4, h // 16)
    coarse_w = max(4, w // 16)
    noise_coarse = rng.standard_normal((coarse_h, coarse_w)).astype(np.float64)
    noise_coarse_up = ndi.zoom(noise_coarse, (h / coarse_h, w / coarse_w), order=1)[:h, :w]
    mottle_1 = gaussian_blur(noise_coarse_up, 4.0)
    mottle_1 /= max(float(np.std(mottle_1)), 1e-6)

    mid_h = max(8, h // 4)
    mid_w = max(8, w // 4)
    noise_mid = rng.standard_normal((mid_h, mid_w)).astype(np.float64)
    noise_mid_up = ndi.zoom(noise_mid, (h / mid_h, w / mid_w), order=1)[:h, :w]
    mottle_2 = gaussian_blur(noise_mid_up, 2.5)
    mottle_2 /= max(float(np.std(mottle_2)), 1e-6)

    mottle = (1.6 * mottle_1 + 0.8 * mottle_2)[:, :, None]
    tone_img = tone_img + mottle

    if with_grid:
        # 1.0 m 주 격자선 + 0.5 m 보조 격자선 (월드 스케일 texel_m 기준)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
        xm = (xx + 0.5) * texel_m
        ym = (yy + 0.5) * texel_m
        d1_x = np.abs(((xm - 0.5) % 1.0) - 0.5) / texel_m
        d1_y = np.abs(((ym - 0.5) % 1.0) - 0.5) / texel_m
        line_1m = 1.0 - smoothstep(0.0, 1.8, np.minimum(d1_x, d1_y))

        dh_x = np.abs(((xm * 2.0 - 0.5) % 1.0) - 0.5) / (texel_m * 2.0)
        dh_y = np.abs(((ym * 2.0 - 0.5) % 1.0) - 0.5) / (texel_m * 2.0)
        line_half = 1.0 - smoothstep(0.0, 1.5, np.minimum(dh_x, dh_y))

        grid_add = (14.0 * line_1m + 5.0 * line_half)[:, :, None]
        tone_img = tone_img + grid_add

    out = albedo_u8.copy()
    out[target] = np.clip(np.rint(tone_img[target]), 0, 255).astype(np.uint8)
    return out


def feather_observed_filled_boundary(
    obs_albedo: np.ndarray,
    filled_albedo: np.ndarray,
    inpaint_mask: np.ndarray,
    *,
    feather_m: float = 0.04,
    texel_m: float = 0.005,
) -> tuple[np.ndarray, np.ndarray]:
    """관측(`inpaint_mask == 0`) ↔ 채움(`inpaint_mask > 0`) 경계를 `feather_m`(기본 4 cm) 폭으로 부드럽게 페더링한다.

    규칙 2·7 준수:
    - 순수 관측 텍셀(`inpaint_mask == 0`)은 바이트 단위로 절대 바꾸지 않고 `SRC_PROJECTED(3)` 출처를 유지한다.
    - 채움 영역(`inpaint_mask > 0`)의 경계 `feather_m` 안쪽에서 관측 경계 전파색(`harmonic_obs`)으로부터
      변형별 채움색(`filled_albedo`)으로 코사인 가중치(`0 -> 1`)로 매끄럽게 전환하여 경계선 이음매를 없애며,
      채움 영역 전체(`inpaint_mask > 0`)의 출처는 `SRC_FILLED(4)` 로 정확히 기록한다.
    """
    obs_u8 = np.ascontiguousarray(obs_albedo, dtype=np.uint8)
    fill_u8 = np.ascontiguousarray(filled_albedo, dtype=np.uint8)
    target = inpaint_mask > 0
    obs = ~target

    src_map = np.where(obs, SRC_PROJECTED, SRC_FILLED).astype(np.uint8)
    if not target.any() or not obs.any():
        return fill_u8.copy(), src_map

    feather_px = max(2.0, float(feather_m) / max(float(texel_m), 1e-5))
    dist_into_hole, (iy, ix) = ndi.distance_transform_edt(target, return_indices=True)

    # 관측 경계 색을 구멍 안쪽으로 부드럽게 번지게 한 경계 기준색
    nearest_obs = obs_u8[iy, ix].astype(np.float64)
    smooth_obs_edge = gaussian_blur(nearest_obs, max(1.5, feather_px * 0.35))

    t = np.clip(dist_into_hole / feather_px, 0.0, 1.0)
    # 코사인 램프: 구멍 테두리(d=0)에서는 관측 경계색과 자연스럽게 이어지고 feather_px 안쪽에서 100% 채움색으로 수렴
    alpha_fill = 0.30 + 0.70 * (0.5 - 0.5 * np.cos(np.pi * t))
    alpha_fill = alpha_fill[:, :, None]

    blended = smooth_obs_edge * (1.0 - alpha_fill) + fill_u8.astype(np.float64) * alpha_fill
    out = obs_u8.copy()
    out[target] = np.clip(np.rint(blended[target]), 0, 255).astype(np.uint8)
    return out, src_map


def compute_surface_source_summary(
    source_map: np.ndarray,
    variant_key: str,
    texel_m: float,
) -> dict[str, Any]:
    """평면 출처 지도(`source_map`)에서 관측(`observed_projection`)과 채움(`filled_<variant>`) 비율 및 면적을 계산한다."""
    filled_key = f"filled_{variant_key}"
    total_texels = int(source_map.size)
    obs_texels = int((source_map == SRC_PROJECTED).sum())
    fill_texels = int((source_map == SRC_FILLED).sum())
    unobs_texels = int((source_map == SRC_GRAY).sum())
    denom = max(total_texels, 1)
    texel_area_m2 = float(texel_m) * float(texel_m)
    total_area_m2 = total_texels * texel_area_m2

    return {
        "variant": variant_key,
        "filled_source_label": filled_key,
        "source_codes": {
            "observed_projection": int(SRC_PROJECTED),
            filled_key: int(SRC_FILLED),
            "unobserved": int(SRC_GRAY),
        },
        "texel_counts": {
            "total": total_texels,
            "observed_projection": obs_texels,
            filled_key: fill_texels,
            "unobserved": unobs_texels,
        },
        "area_m2": {
            "total": round(total_area_m2, 3),
            "observed_projection": round(obs_texels * texel_area_m2, 3),
            filled_key: round(fill_texels * texel_area_m2, 3),
            "unobserved": round(unobs_texels * texel_area_m2, 3),
        },
        "area_ratios": {
            "observed_projection": round(obs_texels / denom, 4),
            filled_key: round(fill_texels / denom, 4),
            "unobserved": round(unobs_texels / denom, 4),
        },
    }


def _pad_mod8(arr: np.ndarray, mode: str = "reflect") -> tuple[np.ndarray, int, int]:
    """(H, W, ...) 배열을 8의 배수 크기로 패딩하고 (패딩 배열, 원래 H, 원래 W)를 반환한다."""
    h, w = arr.shape[:2]
    ph = (8 - (h % 8)) % 8
    pw = (8 - (w % 8)) % 8
    if ph == 0 and pw == 0:
        return arr, h, w
    pad_spec = [(0, ph), (0, pw)] + [(0, 0)] * (arr.ndim - 2)
    return np.pad(arr, pad_spec, mode=mode), h, w


def inpaint_lama_plane(
    albedo: np.ndarray,
    inpaint_mask: np.ndarray,
    surface_source: np.ndarray | None = None,
    *,
    weights_path: str | Any,
    tile_size: int = 768,
    tile_overlap: int = 192,
    device: str = "auto",
) -> tuple[np.ndarray, dict[str, Any]]:
    """변형 (b) `lama`: LaMa(`big-lama.pt` TorchScript) 큰 마스크 인페인팅.

    특징:
    1. 평면 전체를 축소(`<= 1024 px`)해 전역 LaMa 1차 패스를 먼저 수행함으로써,
       고해상도 타일 분할 시 한 타일 전체가 구멍 내부에 놓여 주변 문맥을 잃는 현상을 방지한다.
    2. 이어서 원래 해상도(`5 mm/텍셀`) 평면을 `tile_size`(기본 768) / `tile_overlap`(기본 192) 타일로 나눠
       코사인 창(`inpaint_tiled`)으로 겹쳐 처리하며, 관측 비율이 부족한 타일에는 1차 전역 패스의 앵커 격자를 주입한다.
    """
    import cv2
    import torch
    from simai_ai.scene.glb_inpaint import _propagate_observed_base, inpaint_tiled

    albedo_u8 = np.ascontiguousarray(albedo, dtype=np.uint8)
    mask_u8 = np.ascontiguousarray(inpaint_mask, dtype=np.uint8)
    target = mask_u8 > 0
    if not target.any():
        return albedo_u8.copy(), {"tiles": 0, "device": "none"}

    if surface_source is not None and ((mask_u8 == 0) & (surface_source == SRC_PROJECTED)).any():
        obs_mask = (mask_u8 == 0) & (surface_source == SRC_PROJECTED)
    else:
        obs_mask = mask_u8 == 0

    # 관측 경계의 얇은 어두운 테두리가 LaMa 로 번지지 않도록 마스크를 2 px 팽창해 추론에 사용
    dilated_target = ndi.binary_dilation(target, iterations=2)
    base_rgb, dist_to_obs = _propagate_observed_base(albedo_u8, obs_mask & ~dilated_target, dilated_target, telea_radius=9)

    if device == "auto":
        dev = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        dev = device if (device != "cuda" or torch.cuda.is_available()) else "cpu"

    model = torch.jit.load(str(weights_path), map_location=dev)
    model.eval()

    def _run_lama_tensor(rgb_np: np.ndarray, m_bool: np.ndarray) -> np.ndarray:
        rgb_pad, oh, ow = _pad_mod8(rgb_np, mode="reflect")
        m_pad, _, _ = _pad_mod8(m_bool.astype(np.uint8), mode="edge")
        img_t = torch.from_numpy(rgb_pad.transpose(2, 0, 1)).float().div(255.0).unsqueeze(0).to(dev)
        msk_t = torch.from_numpy(m_pad[None, None, :, :]).float().to(dev)
        with torch.no_grad():
            pred = model(img_t, msk_t)[0]
        pred_np = pred.clamp(0.0, 1.0).mul(255.0).byte().cpu().numpy().transpose(1, 2, 0)
        return np.ascontiguousarray(pred_np[:oh, :ow])

    h, w = albedo_u8.shape[:2]
    # 1단계: 전역 저해상도(최대 변 768 px) LaMa 추론으로 거시적 구조·톤 일관성 확보
    max_global = 768
    scale_g = min(1.0, float(max_global) / max(h, w))
    if scale_g < 0.95:
        gh = max(8, int(round(h * scale_g / 8.0)) * 8)
        gw = max(8, int(round(w * scale_g / 8.0)) * 8)
        small_rgb = cv2.resize(base_rgb, (gw, gh), interpolation=cv2.INTER_AREA)
        small_m = cv2.resize(dilated_target.astype(np.uint8), (gw, gh), interpolation=cv2.INTER_NEAREST) > 0
        # 전역 패스에서도 마스크 비율이 75%를 넘는 먼 구석에는 조화 전파 바탕(base_rgb)의 격자 앵커를 남겨 암전/색번짐 방지
        dist_small = ndi.distance_transform_edt(small_m)
        far_mask = dist_small > 48
        if far_mask.any():
            yy, xx = np.mgrid[0:gh, 0:gw]
            anchor = far_mask & ((yy % 24 < 3) & (xx % 24 < 3))
            small_m = small_m & ~anchor
        global_pred_small = _run_lama_tensor(small_rgb, small_m)
        global_guide = cv2.resize(global_pred_small, (w, h), interpolation=cv2.INTER_CUBIC)
    else:
        global_guide = base_rgb.copy()

    # 전역 가이드와 조화 전파 기저를 결합해 타일 입력 영상 구성
    seed_img = base_rgb.copy()
    seed_img[dilated_target] = np.clip(
        np.rint(0.65 * global_guide[dilated_target].astype(np.float64) + 0.35 * base_rgb[dilated_target].astype(np.float64)),
        0,
        255,
    ).astype(np.uint8)

    # 2단계: 고해상도 겹침 타일별 LaMa 추론
    def _tile_fn(tile_rgb: np.ndarray, tile_m: np.ndarray, _idx: int) -> np.ndarray:
        tm_bool = tile_m > 0
        if not tm_bool.any():
            return tile_rgb
        th, tw = tm_bool.shape
        # 타일 내 관측 픽셀이 부족(< 35%)할 때는 전역 가이드 앵커 격자를 뚫어 LaMa FFC 가 색조·질감을 안정적으로 합성하게 함
        obs_frac = float((~tm_bool).mean())
        if obs_frac < 0.35:
            tdist = ndi.distance_transform_edt(tm_bool)
            yy, xx = np.mgrid[0:th, 0:tw]
            step_a = 32 if obs_frac < 0.15 else 48
            anchors = (tdist > 24) & ((yy % step_a < 4) & (xx % step_a < 4))
            tm_eff = tm_bool & ~anchors
        else:
            tm_eff = tm_bool
        if not tm_eff.any():
            return tile_rgb
        pred_tile = _run_lama_tensor(tile_rgb, tm_eff)
        # 깊은 구멍 내부에서 타일 간 미세 톤 차이를 줄이기 위해 전역 가이드와 살짝 블렌딩
        out_tile = tile_rgb.copy()
        out_tile[tm_bool] = np.clip(
            np.rint(0.82 * pred_tile[tm_bool].astype(np.float64) + 0.18 * tile_rgb[tm_bool].astype(np.float64)),
            0,
            255,
        ).astype(np.uint8)
        return out_tile

    mask_dil_u8 = dilated_target.astype(np.uint8) * 255
    out_full, n_tiles = inpaint_tiled(
        seed_img,
        mask_dil_u8,
        _tile_fn,
        tile_size=tile_size,
        overlap=tile_overlap,
    )
    res = albedo_u8.copy()
    res[target] = out_full[target]
    return res, {"tiles": int(n_tiles), "device": dev, "tile_size": int(tile_size), "tile_overlap": int(tile_overlap)}


def sample_backing_plane_srgb_at_world_pts(
    P_world: np.ndarray,
    N_world: np.ndarray,
    floor_plane_info: dict[str, Any],
    floor_tex_srgb: np.ndarray,
    wall_planes_info: list[dict[str, Any]],
    wall_texs_srgb: list[np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """3D World 점 `P_world (K, 3)` 과 법선 `N_world (K, 3)` 에 대응하는 하부 받침 평면(바닥 또는 벽 3면)의 구운 색상 `(K, 3)` sRGB `[0, 1]` 과 매칭 마스크 `(K,)` 를 반환한다."""
    from simai_ai.scene.texture_provenance import bilinear

    K_pts = len(P_world)
    out_srgb = np.zeros((K_pts, 3), dtype=np.float64)
    matched = np.zeros(K_pts, dtype=bool)
    if K_pts == 0:
        return out_srgb, matched

    # 1) 바닥 근처 점 판정: z < 0.22 m 이고 |n_z| > 0.40
    is_floor = (P_world[:, 2] < 0.22) & (np.abs(N_world[:, 2]) > 0.40)
    if is_floor.any():
        fp_idx = np.flatnonzero(is_floor)
        P_f = P_world[fp_idx]
        # 바닥 평면 로컬 좌표계: c0(뒤-좌), c1(뒤-우), c3(앞-좌)
        c0 = np.asarray(floor_plane_info["corners_world"][0], dtype=np.float64)
        c1 = np.asarray(floor_plane_info["corners_world"][1], dtype=np.float64)
        c3 = np.asarray(floor_plane_info["corners_world"][3], dtype=np.float64)
        u_vec = c1[:2] - c0[:2]
        v_vec = c3[:2] - c0[:2]
        u_len2 = max(float(np.dot(u_vec, u_vec)), 1e-12)
        v_len2 = max(float(np.dot(v_vec, v_vec)), 1e-12)

        rel = P_f[:, :2] - c0[None, :2]
        u_norm = (rel @ u_vec) / u_len2
        v_norm = (rel @ v_vec) / v_len2
        in_f = (u_norm >= 0.0) & (u_norm <= 1.0) & (v_norm >= 0.0) & (v_norm <= 1.0)
        if in_f.any():
            sub_idx = fp_idx[in_f]
            fh, fw = floor_tex_srgb.shape[:2]
            px = np.clip(u_norm[in_f] * (fw - 1), 0.0, fw - 1.0)
            py = np.clip(v_norm[in_f] * (fh - 1), 0.0, fh - 1.0)
            out_srgb[sub_idx] = bilinear(floor_tex_srgb, px, py) / 255.0
            matched[sub_idx] = True

    # 2) 벽 3면 근처 점 판정: 아직 매칭 안 된 점 중 벽 평면과 거리 < 0.16 m
    rem_mask = ~matched
    if rem_mask.any():
        for wp, w_tex in zip(wall_planes_info, wall_texs_srgb):
            cand_idx = np.flatnonzero(~matched)
            if len(cand_idx) == 0:
                break
            P_w = P_world[cand_idx]
            n2 = np.asarray(wp["n_world_xy"], dtype=np.float64)
            t2 = np.asarray(wp["t_world_xy"], dtype=np.float64)
            d_orig = float(wp["d_world_m"])
            u0, u1 = float(wp["u_range_m"][0]), float(wp["u_range_m"][1])
            z0, z1 = float(wp["z_range_m"][0]), float(wp["z_range_m"][1])

            dist_w = np.abs(P_w[:, :2] @ n2 - d_orig)
            u_val = P_w[:, :2] @ t2
            z_val = P_w[:, 2]
            in_w = (dist_w < 0.16) & (u_val >= u0) & (u_val <= u1) & (z_val >= z0) & (z_val <= z1)
            if in_w.any():
                hit_idx = cand_idx[in_w]
                wh, ww = w_tex.shape[:2]
                u_norm = (u_val[in_w] - u0) / max(u1 - u0, 1e-6)
                # 벽 텍스처 v=0 은 상단(z1), v=1 은 하단(z0)으로 두어 2D 이미지가 정방향으로 보이게 함
                v_norm = (z1 - z_val[in_w]) / max(z1 - z0, 1e-6)
                px = np.clip(u_norm * (ww - 1), 0.0, ww - 1.0)
                py = np.clip(v_norm * (wh - 1), 0.0, wh - 1.0)
                out_srgb[hit_idx] = bilinear(w_tex, px, py) / 255.0
                matched[hit_idx] = True

    return np.clip(out_srgb, 0.0, 1.0), matched


def blend_atlas_at_hole_boundary(
    atlas_u8: np.ndarray,
    prim_pos_world: np.ndarray,
    prim_idx: np.ndarray,
    prim_uv: np.ndarray,
    prim_boundary_dist: np.ndarray,
    floor_plane_info: dict[str, Any],
    floor_tex_srgb: np.ndarray,
    wall_planes_info: list[dict[str, Any]],
    wall_texs_srgb: list[np.ndarray],
    *,
    feather_band_m: float = 0.08,
    max_blend_alpha: float = 0.85,
) -> tuple[np.ndarray, int]:
    """메시 열린 구멍 경계(`d < feather_band_m = 8 cm`)에 걸친 삼각형들의 UV 아틀라스 텍셀을 하부 받침 평면 색으로 부드럽게 섞는다.

    방법 및 근거:
    - glTF 2.0 표준 재질에서 정점색(`COLOR_0`)은 텍스처(`baseColorTexture`)에 **곱셈(`tex * color_0`)**으로만 작용하므로,
      구멍 테두리의 어두운 그림자 텍셀을 밝은 바닥·벽 톤으로 끌어올릴 수 없다.
    - 반면 UV 아틀라스에서 열린 경계 8 cm 이내 삼각형 영역을 래스터라이즈해 각 텍셀의 3D 월드 좌표 $P$ 와
      경계 거리 $d_{\\text{bound}}$ 를 구한 뒤 받침 평면 색 $C_{\\text{plane}}(P)$ 와 볼록 선형 보간(`mix`)하면,
      정점 속성 추가 없이 모든 표준 뷰어(`MeshBasic`/`MeshStandard`)에서 구멍 경계 단차가 매끄럽게 사라진다.
    """
    from simai_ai.scene.glb_enhance import rasterize

    out_atlas = np.ascontiguousarray(atlas_u8, dtype=np.uint8).copy()
    if len(prim_idx) == 0 or prim_uv is None:
        return out_atlas, 0

    tri_d = prim_boundary_dist[prim_idx]  # (T, 3)
    near_edge = tri_d.min(axis=1) < float(feather_band_m)
    if not near_edge.any():
        return out_atlas, 0

    sel_f = np.flatnonzero(near_edge)
    F_sub = prim_idx[sel_f]
    P_tri = prim_pos_world[F_sub]  # (Ts, 3, 3)
    D_tri = prim_boundary_dist[F_sub]  # (Ts, 3)
    UV_tri = prim_uv[F_sub]  # (Ts, 3, 2)

    # 삼각형 법선
    fn = np.cross(P_tri[:, 1] - P_tri[:, 0], P_tri[:, 2] - P_tri[:, 0])
    fn_norm = np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-12)
    N_tri = fn / fn_norm  # (Ts, 3)

    H_a, W_a = out_atlas.shape[:2]
    # glTF UV: (0,0) 왼쪽 위 -> 픽셀 좌표 (u * W_a, v * H_a)
    uv_mod = np.mod(UV_tri, 1.0)
    tri_px = np.stack([uv_mod[:, :, 0] * W_a, uv_mod[:, :, 1] * H_a], axis=2)
    # 작은 거리 우선으로 z-버퍼에 기록
    tid, lam, _ = rasterize(tri_px, D_tri, (H_a, W_a), perspective=False)

    hit = tid >= 0
    if not hit.any():
        return out_atlas, 0

    ys, xs = np.nonzero(hit)
    t_ids = tid[ys, xs]
    wts = lam[ys, xs].astype(np.float64)  # (K, 3)

    d_pix = (D_tri[t_ids] * wts).sum(axis=1)
    in_band = d_pix < float(feather_band_m)
    if not in_band.any():
        return out_atlas, 0

    ys = ys[in_band]
    xs = xs[in_band]
    t_ids = t_ids[in_band]
    wts = wts[in_band]
    d_pix = d_pix[in_band]

    P_pix = (P_tri[t_ids] * wts[:, :, None]).sum(axis=1)  # (K, 3)
    N_pix = N_tri[t_ids]  # (K, 3)

    plane_srgb, matched = sample_backing_plane_srgb_at_world_pts(
        P_pix, N_pix, floor_plane_info, floor_tex_srgb, wall_planes_info, wall_texs_srgb
    )
    if not matched.any():
        return out_atlas, 0

    ys_m = ys[matched]
    xs_m = xs[matched]
    d_m = d_pix[matched]
    target_rgb = plane_srgb[matched] * 255.0

    t_ramp = smoothstep(0.0, float(feather_band_m), d_m)
    alpha = float(max_blend_alpha) * (1.0 - t_ramp)[:, None]

    orig_rgb = out_atlas[ys_m, xs_m].astype(np.float64)
    blended_rgb = orig_rgb * (1.0 - alpha) + target_rgb * alpha
    out_atlas[ys_m, xs_m] = np.clip(np.rint(blended_rgb), 0, 255).astype(np.uint8)
    return out_atlas, int(ys_m.size)


def blend_unobserved_vertex_colors(
    pos_world: np.ndarray,
    idx: np.ndarray,
    col_linear: np.ndarray,
    boundary_dist: np.ndarray,
    floor_plane_info: dict[str, Any],
    floor_tex_srgb: np.ndarray,
    wall_planes_info: list[dict[str, Any]],
    wall_texs_srgb: list[np.ndarray],
    *,
    feather_band_m: float = 0.08,
) -> tuple[np.ndarray, int]:
    """텍스처가 없는 `unobserved_neutral` 프리미티브의 정점색(`COLOR_0`, 선형 RGB)을 열린 구멍 경계(8 cm) 및 바닥 어두운 그림자에서 받침 평면 색으로 부드럽게 맞춘다."""
    out_lin = np.asarray(col_linear, dtype=np.float64).copy()
    if len(pos_world) == 0 or len(idx) == 0:
        return out_lin, 0

    # 정점 법선 근사
    v_norm = np.zeros_like(pos_world, dtype=np.float64)
    p0, p1, p2 = pos_world[idx[:, 0]], pos_world[idx[:, 1]], pos_world[idx[:, 2]]
    fn = np.cross(p1 - p0, p2 - p0)
    np.add.at(v_norm, idx[:, 0], fn)
    np.add.at(v_norm, idx[:, 1], fn)
    np.add.at(v_norm, idx[:, 2], fn)
    n_len = np.maximum(np.linalg.norm(v_norm, axis=1, keepdims=True), 1e-12)
    v_norm /= n_len

    plane_srgb, matched = sample_backing_plane_srgb_at_world_pts(
        pos_world, v_norm, floor_plane_info, floor_tex_srgb, wall_planes_info, wall_texs_srgb
    )
    if not matched.any():
        return out_lin, 0

    m_idx = np.flatnonzero(matched)
    cur_srgb = linear_to_srgb(out_lin[m_idx])
    target_srgb = plane_srgb[m_idx]
    d_m = boundary_dist[m_idx]

    # 열린 구멍 경계(d_m < feather_band_m = 8 cm) 구간만 받침 평면 색으로 부드럽게 페더링한다.
    # (내부 바닥 정점은 12차 보강에서 이미 인접 textured_0000 와 톤이 맞춰져 있으므로 건드리지 않아야 격자 얼룩이 생기지 않는다.)
    in_band = d_m < float(feather_band_m)
    if not in_band.any():
        return out_lin, 0

    t_edge = smoothstep(0.0, float(feather_band_m), d_m)
    w_edge = np.where(in_band, 0.85 * (1.0 - t_edge), 0.0)

    w_blend = w_edge[:, None]
    active = w_blend[:, 0] > 1e-3
    new_srgb = cur_srgb * (1.0 - w_blend) + target_srgb * w_blend
    out_lin[m_idx] = srgb_to_linear(np.clip(new_srgb, 0.0, 1.0))
    return out_lin, int(active.sum())
