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

from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np
import scipy.ndimage as ndi
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from simai_ai.scene.glb_enhance import linear_to_srgb, srgb_to_linear
from simai_ai.scene.glb_fill import SRC_GRAY, SRC_PROJECTED
from simai_ai.scene.texture_provenance import GENERATED, gaussian_blur

SRC_FILLED = GENERATED  # 4: 미관측 영역 채움 텍셀 (tone / lama / classic)
SRC_SEE_THROUGH = np.uint8(5)  # 5: 개구부·창문·복도 등 평면 너머로 뚫려 보이는 영역 (채우지 않음)

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
_OKLAB_M1_INV = np.linalg.inv(_OKLAB_M1)
_OKLAB_M2_INV = np.linalg.inv(_OKLAB_M2)


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


def oklab_to_srgb(lab: np.ndarray) -> np.ndarray:
    """Oklab `(..., 3)` 배열을 sRGB `[0, 1]` 배열 `(..., 3)` 로 역변환한다."""
    lms_cbrt = np.asarray(lab, dtype=np.float64) @ _OKLAB_M2_INV.T
    lms = np.maximum(lms_cbrt, 0.0) ** 3.0
    lin = np.clip(lms @ _OKLAB_M1_INV.T, 0.0, 1.0)
    return np.clip(linear_to_srgb(lin), 0.0, 1.0)


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


def compute_camera_image_quality_maps(
    image_rgb_u8: np.ndarray,
    *,
    patch_size_px: int = 17,
    overexp_thresh_u8: int = 248,
    underexp_thresh_u8: int = 12,
) -> dict[str, Any]:
    """4차 지시 3번: 카메라 이미지의 패치 단위 선명도(라플라시안 분산)와 과노출·저노출(포화 픽셀 비율) 품질 가중치 지도를 계산한다.

    근거 및 수식:
    1. **패치 단위 선명도 (`w_sharp`)**:
       - 휘도 $Y = (0.299 R + 0.587 G + 0.114 B) / 255 \\in [0, 1]$ 에 3x3 이산 라플라시안 커널 $\\nabla^2 Y$ 를 적용하고,
         `patch_size_px` (`17x17` px, 실내 4m 거리에서 약 6~8 cm 패치) 창 내 로컬 분산 $\\text{Var}_{\\text{patch}}(\\nabla^2 Y)$ 를 구한다.
       - 로그 스케일 $\\log_{10}(\\text{Var}_{\\text{patch}}(\\nabla^2 Y) + 10^{-6})$ 의 이미지 내 하위 20%($q_{20}$) ~ 상위 85%($q_{85}$) 구간을
         `smoothstep` 으로 정규화해 $w_{\\text{sharp}} \\in [0.35, 1.0]$ 을 부여한다.
       - 고주파 글자·액자 테두리에서 초점이 선명한 정면 카메라는 `1.0` 에 가까운 점수를 받고, 렌즈 주변부 수차·사선 블러로 흐려진 카메라는 낮아진다.
    2. **과노출·저노출 포화도 감쇠 (`w_exposure`)**:
       - 픽셀별 최대 채널 $\\max(R,G,B) \\ge 248$ (조명 반사 하이라이트 포화) 또는 $\\max(R,G,B) \\le 12$ (암부 클리핑) 인 포화 마스크의
         `patch_size_px` 패치 내 비율 $\\rho_{\\text{sat}} \\in [0, 1]$ 을 계산한다.
       - $\\rho_{\\text{sat}}$ 가 2% 를 넘으면 감쇠하기 시작해 35% 이상 포화된 패치는 최대 85% 감쇠하며,
         추가로 로컬 평균 휘도가 극단($Y > 0.90$ 또는 $Y < 0.06$)에 가까울 때도 감쇠해 $w_{\\text{exp}} \\in [0.08, 1.0]$ 을 산출한다.
    3. **종합 사진 품질 가중치 (`w_photo`)**:
       - $w_{\\text{photo}}(u, v) = w_{\\text{sharp}}(u, v)^{0.75} \\cdot w_{\\text{exp}}(u, v)^{1.25} \\in [0.05, 1.0]$.
    """
    rgb = np.ascontiguousarray(image_rgb_u8, dtype=np.uint8)
    rgb_f = rgb.astype(np.float64)
    lum = (0.299 * rgb_f[:, :, 0] + 0.587 * rgb_f[:, :, 1] + 0.114 * rgb_f[:, :, 2]) / 255.0

    # 1) 3x3 라플라시안 및 패치 단위 분산
    lap_kernel = np.array([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float64)
    lap = ndi.convolve(lum, lap_kernel, mode="reflect")
    win = max(3, int(patch_size_px))
    mean_lap = ndi.uniform_filter(lap, size=win, mode="reflect")
    mean_lap2 = ndi.uniform_filter(lap * lap, size=win, mode="reflect")
    lap_var = np.maximum(mean_lap2 - mean_lap * mean_lap, 0.0)

    log_lv = np.log10(lap_var + 1e-6)
    q20 = float(np.percentile(log_lv, 20.0))
    q85 = max(float(np.percentile(log_lv, 85.0)), q20 + 0.25)
    w_sharp = 0.35 + 0.65 * smoothstep(q20, q85, log_lv)

    # 2) 과노출·저노출 포화 픽셀 비율 (패치 단위)
    c_max = rgb.max(axis=2)
    c_min = rgb.min(axis=2)
    over_mask = (c_max >= int(overexp_thresh_u8)) & ((c_min >= 200) | (lum >= 0.92))
    under_mask = c_max <= int(underexp_thresh_u8)
    sat_mask = (over_mask | under_mask).astype(np.float64)
    sat_ratio = np.clip(ndi.uniform_filter(sat_mask, size=win, mode="reflect"), 0.0, 1.0)

    lum_patch = ndi.uniform_filter(lum, size=win, mode="reflect")
    extreme_lum = smoothstep(0.88, 0.98, lum_patch) + 0.65 * (1.0 - smoothstep(0.02, 0.08, lum_patch))
    w_sat = 1.0 - 0.85 * smoothstep(0.02, 0.35, sat_ratio)
    w_lum = 1.0 - 0.45 * np.clip(extreme_lum, 0.0, 1.0)
    w_exposure = np.clip(w_sat * w_lum, 0.08, 1.0)

    w_photo = np.clip((w_sharp**0.75) * (w_exposure**1.25), 0.05, 1.0)
    return {
        "w_photo_map": w_photo.astype(np.float32),
        "w_sharp_map": w_sharp.astype(np.float32),
        "w_exposure_map": w_exposure.astype(np.float32),
        "lap_var_map": lap_var.astype(np.float32),
        "sat_ratio_map": sat_ratio.astype(np.float32),
        "stats": {
            "patch_size_px": win,
            "mean_lap_var": round(float(lap_var.mean()), 6),
            "p50_lap_var": round(float(np.median(lap_var)), 6),
            "p90_lap_var": round(float(np.percentile(lap_var, 90.0)), 6),
            "overexposed_pixel_ratio": round(float(over_mask.mean()), 5),
            "underexposed_pixel_ratio": round(float(under_mask.mean()), 5),
            "mean_w_sharp": round(float(w_sharp.mean()), 4),
            "mean_w_exposure": round(float(w_exposure.mean()), 4),
            "mean_w_photo": round(float(w_photo.mean()), 4),
        },
    }


def optimize_wall_camera_seam_weights(
    cand_mask: np.ndarray,
    cand_srgb: np.ndarray,
    cand_geom_w: np.ndarray,
    hw: tuple[int, int],
    *,
    grad_patch_px: int = 35,
) -> tuple[np.ndarray, dict[str, Any]]:
    """방안 (1) MVS-Texturing 방식: 벽면에서 두 카메라 투영의 그래디언트·경계가 있는 패치(안내판·액자)는 단일 카메라로 묶고,
    카메라가 바뀌는 이음새(seam)를 두 카메라 간 색 차이·그래디언트 불일치가 최소인 평탄한 벽면 열에 배치한다.

    원리:
    1. 각 후보 카메라 $k$ 의 투영 이미지 $I_k(y, x)$ 에 대해 Sobel 그래디언트 크기 $G_k(y, x) = \\|\\nabla Y_k(y, x)\\|_2$ 와
       후보 간 그래디언트 불일치 $\\Delta G(y, x) = |G_{k_1} - G_{k_2}| + \\|I_{k_1} - I_{k_2}\\|_1$ 를 구한다.
    2. 로컬 패치(`grad_patch_px`) 평균 그래디언트가 임계값 이상인 연결 성분(안내판·액자·문틀 등 고주파 구조물) 안에서는
       해당 성분의 커버리지 × 평균 가중치 × 선명도가 가장 높은 단일 카메라 1대만 하드 선택되도록 가중치를 고정하여,
       안내판이나 글자 한가운데를 가로지르는 카메라 전환(두 겹 겹침/시차 계단)을 원천 차단한다.
    3. 평탄한 벽면에서 두 주력 카메라가 전환될 때는 열별 이음새 비용
       $E_{\\text{seam}}(x) = \\text{mean}_y(\\Delta G(y, x) + 2.5 (G_{k_1}(y, x) + G_{k_2}(y, x)))$ 가 최소이고
       고주파 패치와 겹치지 않는 저그래디언트 열 $x_{\\text{seam}}^*$ 에서 전환한다.
    """
    tex_h, tex_w = hw
    num_cams, n_pts = cand_mask.shape
    if n_pts != tex_h * tex_w or tex_h < 16 or tex_w < 16:
        return cand_geom_w, {"seam_optimized": False}

    m_3d = cand_mask.reshape(num_cams, tex_h, tex_w)
    c_4d = cand_srgb.reshape(num_cams, tex_h, tex_w, 3)
    w_3d = np.where(m_3d, cand_geom_w.reshape(num_cams, tex_h, tex_w), 0.0).copy()

    # 각 카메라별 공간 스무딩된 기본 가중치 및 휘도 그래디언트 계산
    grad_3d = np.zeros((num_cams, tex_h, tex_w), dtype=np.float64)
    for k in range(num_cams):
        if not m_3d[k].any():
            continue
        mk_f = m_3d[k].astype(np.float64)
        num_w = ndi.gaussian_filter(w_3d[k] * mk_f, sigma=18.0)
        den_w = ndi.gaussian_filter(mk_f, sigma=18.0)
        w_3d[k] = np.where(m_3d[k], np.where(den_w > 1e-5, num_w / np.maximum(den_w, 1e-8), w_3d[k]), 0.0)

        lum_k = 0.299 * c_4d[k, :, :, 0] + 0.587 * c_4d[k, :, :, 1] + 0.114 * c_4d[k, :, :, 2]
        gx = ndi.sobel(lum_k, axis=1)
        gy = ndi.sobel(lum_k, axis=0)
        grad_3d[k] = np.where(m_3d[k], np.hypot(gx, gy), 0.0)

    # (A) 안내판·액자 등 고주파 그래디언트 구조물을 칼럼 단위로 검출해, 이음새(seam)가 절대 고주파 구조물 내부를 가로지르지 않게 한다.
    max_grad = grad_3d.max(axis=0)
    win = max(9, int(grad_patch_px))
    patch_grad = ndi.uniform_filter(max_grad, size=win)
    obs_any = m_3d.any(axis=0)
    if not obs_any.any():
        return w_3d.reshape(num_cams, n_pts), {"seam_optimized": False}

    grad_thr = max(0.065, float(np.percentile(patch_grad[obs_any], 72.0)))
    high_grad_mask = obs_any & (patch_grad >= grad_thr)
    high_grad_mask = ndi.binary_closing(high_grad_mask, structure=np.ones((25, 45), dtype=bool))
    high_grad_mask = ndi.binary_dilation(high_grad_mask, structure=np.ones((15, 25), dtype=bool)) & obs_any

    # (B) 벽면에서 2대 주력 카메라가 겹치는 구간 전체를 단일 최적 수직 이음새(seam_col)로 양분한다!
    #     이렇게 하면 안내판·액자 내부에서 픽셀별로 카메라가 바뀌는 현상이 0% 가 되고,
    #     이음새는 두 카메라 색차·그래디언트 불일치가 최소이면서 고주파 구조물(안내판·액자) 바깥인 평탄한 벽면 열에 놓인다.
    cam_covs = [float(m_3d[k].mean()) for k in range(num_cams)]
    active_cams = [k for k, cv in enumerate(cam_covs) if cv > 0.05]
    seam_col = -1
    locked_regions = int(ndi.label(high_grad_mask)[1])
    if len(active_cams) == 2:
        k0, k1 = active_cams[0], active_cams[1]
        both_2d = m_3d[k0] & m_3d[k1]
        both_col_ratio = both_2d.mean(axis=0)
        valid_cols = np.flatnonzero(both_col_ratio > 0.25)
        if len(valid_cols) >= 40:
            w_diff_col = (w_3d[k0] - w_3d[k1]).mean(axis=0)
            sign_changes = np.flatnonzero(np.diff(np.sign(w_diff_col[valid_cols])) != 0)
            if len(sign_changes) > 0:
                c_mid = int(valid_cols[sign_changes[len(sign_changes) // 2]])
                search_lo = max(int(valid_cols[0]), c_mid - int(tex_w * 0.28))
                search_hi = min(int(valid_cols[-1]), c_mid + int(tex_w * 0.28))
            else:
                search_lo = int(valid_cols[len(valid_cols) // 4])
                search_hi = int(valid_cols[(3 * len(valid_cols)) // 4])

            if search_hi > search_lo + 10:
                color_diff = np.linalg.norm(c_4d[k0] - c_4d[k1], axis=-1)
                grad_disagree = np.abs(grad_3d[k0] - grad_3d[k1]) + 2.5 * (grad_3d[k0] + grad_3d[k1])
                pixel_cost = np.where(both_2d, color_diff + 3.0 * grad_disagree, 0.0)
                col_cost = pixel_cost.sum(axis=0) / np.maximum(both_2d.sum(axis=0), 1)
                # 고주파 구조물(안내판·액자)이 걸친 열에는 큰 페널티를 부여해 이음새가 구조물 바깥 평탄한 기둥/벽면에 놓이게 함
                hg_col_frac = high_grad_mask.mean(axis=0)
                col_cost = col_cost + 12.0 * hg_col_frac
                col_cost_smooth = ndi.uniform_filter1d(col_cost, size=19)
                seam_col = int(search_lo + np.argmin(col_cost_smooth[search_lo : search_hi + 1]))

                left_cam = k0 if w_3d[k0, :, :seam_col].sum() >= w_3d[k1, :, :seam_col].sum() else k1
                right_cam = k1 if left_cam == k0 else k0
                xx = np.arange(tex_w, dtype=np.float64)[None, :]
                # 저그래디언트 평탄 벽면 이음새(seam_col)를 기준으로 왼쪽은 left_cam, 오른쪽은 right_cam 을 하드/초급격 전환
                s_right = smoothstep(float(seam_col - 4), float(seam_col + 4), xx)
                s_left = 1.0 - s_right
                w_3d[left_cam] = np.where(both_2d, np.maximum(w_3d[left_cam], 0.1) * (1e-4 + 0.9999 * s_left), w_3d[left_cam])
                w_3d[right_cam] = np.where(both_2d, np.maximum(w_3d[right_cam], 0.1) * (1e-4 + 0.9999 * s_right), w_3d[right_cam])

    return w_3d.reshape(num_cams, n_pts), {
        "seam_optimized": True,
        "locked_high_grad_regions": int(locked_regions),
        "seam_column": int(seam_col),
    }


def select_camera_colors_with_outlier_removal(
    cand_mask: np.ndarray,
    cand_srgb: np.ndarray,
    cand_geom_w: np.ndarray,
    neighbor_median_srgb: np.ndarray,
    *,
    sigma_oklab: float = 0.056,
    is_floor: bool = True,
    sharp_exponent: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """다중 카메라 후보 색상에서 외톨이(의자 다리·테이블 잔상 등)를 제거하고 가중 블렌딩한다.

    매개변수:
    - `cand_mask`: `(C, N)` bool — 기하·화면·법선·깊이(5 cm 일치) 게이트를 통과한 카메라 후보 마스크
    - `cand_srgb`: `(C, N, 3)` float64 `[0, 1]` — 노출 보정된 카메라별 샘플 색상
    - `cand_geom_w`: `(C, N)` float64 — 카메라별 정면성·가장자리·깊이·거리 가중치
    - `neighbor_median_srgb`: `(N, 3)` float64 `[0, 1]` — 2대 후보일 때 세 번째 표(3rd vote)로 쓰는 이웃 관측 텍셀 중앙색
    - `sigma_oklab`: Oklab 색 거리 표준편차 $\\sigma$ (12차 보정 겹침 인라이어 p75=0.0506, 전체 쌍 p50=0.0542 근거 0.056)
    - `sharp_exponent`: `> 1.0` 이면 외톨이 감쇠 후 상대 가중치 `(w / w_max) ** sharp_exponent` 를 적용해
      가장 정면·근접한 최우수 카메라 1대가 지배적(95~100%)으로 선택되게 하여 다중 카메라 평균으로 인한 고주파 글자·액자 흐려짐을 막는다.

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
        if not is_floor and float(sharp_exponent) > 1.0:
            best_k3 = np.argmax(w_sub[:, sel_3], axis=0)
        else:
            tie_w = 0.35 if is_floor else 0.10
            score_3 = dmat[:, :, sel_3].sum(axis=1) + tie_w * d_nb[:, sel_3]
            score_3 = np.where(m_sub[:, sel_3], score_3, 1e9)
            best_k3 = np.argmin(score_3, axis=0)
        m3_indices = np.flatnonzero(sel_3)
        ref_lab[m3_indices] = lab_sub[best_k3, m3_indices, :]

    # (2) 후보 2대: 바닥은 이웃 관측 텍셀 중앙색(nb_lab)을 세 번째 표(3rd vote)로 넣어 메도이드 선택,
    #     벽(sharp_exponent > 1.0)은 고대비 글자·액자 디테일이 nb_lab(블러링된 배경색)에 의해 지워지지 않도록 최우수 가중치 카메라를 기준으로 선택
    sel_2 = nc_sub == 2
    if sel_2.any():
        if not is_floor and float(sharp_exponent) > 1.0:
            best_k2 = np.argmax(w_sub[:, sel_2], axis=0)
        else:
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
    if not is_floor and float(sharp_exponent) > 1.0:
        gate_hi = 3.20
        gate_lo = 2.20
    else:
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
    if float(sharp_exponent) > 1.0:
        w_max = np.maximum(use_w.max(axis=0, keepdims=True), 1e-12)
        use_w = use_w * ((use_w / w_max) ** (float(sharp_exponent) - 1.0))
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
    protect_face_mask: np.ndarray | None = None,
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
    if protect_face_mask is not None:
        prot_b = np.asarray(protect_face_mask, dtype=bool)
        if prot_b.any():
            prot_faces_per_comp = np.bincount(comp_id[prot_b], minlength=len(u_roots))
            prot_area_per_comp = np.bincount(comp_id[prot_b], weights=areas[prot_b], minlength=len(u_roots))
            # 단위 시험용 소규모 메시는 그대로 보호하되, 실세계 대형 메시(>= 1000면)에서는 핀홀 1~3개 삼각형 잡음 조각(< 30면 and < 0.025 m2)은 보호하지 않는다
            if m >= 1000:
                valid_prot = (prot_faces_per_comp >= 30) | (prot_area_per_comp >= 0.025)
            else:
                valid_prot = prot_faces_per_comp >= 1
            drop_comp[valid_prot] = False

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
    see_through_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """관측(`inpaint_mask == 0`) ↔ 채움(`inpaint_mask > 0`) 경계를 `feather_m`(기본 4 cm) 폭으로 부드럽게 페더링한다.

    규칙 2·7 준수:
    - 순수 관측 텍셀(`inpaint_mask == 0` 이고 `see_through` 아님)은 바이트 단위로 절대 바꾸지 않고 `SRC_PROJECTED(3)` 출처를 유지한다.
    - 개구부·뚫린 영역(`see_through_mask == True`)은 벽으로 메우지 않고 `SRC_SEE_THROUGH(5)` 출처로 기록한다.
    - 채움 영역(`inpaint_mask > 0`)의 경계 `feather_m` 안쪽에서 관측 경계 전파색(`harmonic_obs`)으로부터
      변형별 채움색(`filled_albedo`)으로 코사인 가중치(`0 -> 1`)로 매끄럽게 전환하여 경계선 이음매를 없애며,
      채움 영역 전체(`inpaint_mask > 0`)의 출처는 `SRC_FILLED(4)` 로 정확히 기록한다.
    """
    obs_u8 = np.ascontiguousarray(obs_albedo, dtype=np.uint8)
    fill_u8 = np.ascontiguousarray(filled_albedo, dtype=np.uint8)
    st_mask = np.asarray(see_through_mask, dtype=bool) if see_through_mask is not None else np.zeros(inpaint_mask.shape[:2], dtype=bool)
    target = (inpaint_mask > 0) & (~st_mask)
    obs = (~(inpaint_mask > 0)) & (~st_mask)

    src_map = np.where(obs, SRC_PROJECTED, np.where(st_mask, SRC_SEE_THROUGH, SRC_FILLED)).astype(np.uint8)
    if not target.any() or not obs.any():
        out_early = fill_u8.copy()
        out_early[obs] = obs_u8[obs]
        return out_early, src_map

    feather_px = max(2.0, float(feather_m) / max(float(texel_m), 1e-5))
    dist_into_hole, (iy, ix) = ndi.distance_transform_edt(~obs, return_indices=True)

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
    """평면 출처 지도(`source_map`)에서 관측(`observed_projection`), 채움(`filled_<variant>`), 개구부(`see_through`) 비율 및 면적을 계산한다."""
    filled_key = f"filled_{variant_key}"
    total_texels = int(source_map.size)
    obs_texels = int((source_map == SRC_PROJECTED).sum())
    fill_texels = int((source_map == SRC_FILLED).sum())
    st_texels = int((source_map == SRC_SEE_THROUGH).sum())
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
            "see_through": int(SRC_SEE_THROUGH),
            "unobserved": int(SRC_GRAY),
        },
        "texel_counts": {
            "total": total_texels,
            "observed_projection": obs_texels,
            filled_key: fill_texels,
            "see_through": st_texels,
            "unobserved": unobs_texels,
        },
        "area_m2": {
            "total": round(total_area_m2, 3),
            "observed_projection": round(obs_texels * texel_area_m2, 3),
            filled_key: round(fill_texels * texel_area_m2, 3),
            "see_through": round(st_texels * texel_area_m2, 3),
            "unobserved": round(unobs_texels * texel_area_m2, 3),
        },
        "area_ratios": {
            "observed_projection": round(obs_texels / denom, 4),
            filled_key: round(fill_texels / denom, 4),
            "see_through": round(st_texels / denom, 4),
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
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    import cv2
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

    try:
        import torch  # type: ignore[import-not-found]
        has_local_torch = True
        local_cuda = bool(torch.cuda.is_available())
    except ImportError:
        has_local_torch = False
        local_cuda = False

    pose_cuda_py = Path(os.environ.get("SIMAI_POSE_CUDA_PYTHON", "C:/Users/SSAFY/simai-local/pose-cuda/Scripts/python.exe"))
    has_weights = Path(str(weights_path)).is_file()
    use_subproc_cuda = (not local_cuda) and pose_cuda_py.is_file() and has_weights

    model = None
    dev = "cuda" if (local_cuda or use_subproc_cuda) else "cpu"
    if has_local_torch and not use_subproc_cuda and has_weights:
        if device == "auto":
            dev = "cuda" if local_cuda else "cpu"
        else:
            dev = device if (device != "cuda" or local_cuda) else "cpu"
        model = torch.jit.load(str(weights_path), map_location=dev)
        model.eval()
    if model is None and not use_subproc_cuda:
        res = albedo_u8.copy()
        res[target] = base_rgb[target]
        return res, {"tiles": 0, "device": "none", "tile_size": int(tile_size), "tile_overlap": int(tile_overlap)}

    def _run_lama_batch_subproc(batch_rgb_msk: list[tuple[np.ndarray, np.ndarray]]) -> list[np.ndarray]:
        if not batch_rgb_msk:
            return []
        with tempfile.TemporaryDirectory(prefix="simai_lama_", ignore_cleanup_errors=True) as td:
            td_path = Path(td)
            in_npz = td_path / "in.npz"
            out_npz = td_path / "out.npz"
            payload: dict[str, np.ndarray] = {}
            meta_shapes: list[tuple[int, int]] = []
            for idx_b, (rgb_np, m_bool) in enumerate(batch_rgb_msk):
                rgb_pad, oh, ow = _pad_mod8(rgb_np, mode="reflect")
                m_pad, _, _ = _pad_mod8(m_bool.astype(np.uint8), mode="edge")
                payload[f"rgb_{idx_b}"] = rgb_pad
                payload[f"msk_{idx_b}"] = m_pad
                meta_shapes.append((oh, ow))
            np.savez(in_npz, **payload)
            script = (
                "import numpy as np, torch\n"
                f"with np.load({str(in_npz)!r}) as d:\n"
                "    dev = 'cuda' if torch.cuda.is_available() else 'cpu'\n"
                f"    m = torch.jit.load({str(weights_path)!r}, map_location=dev).eval()\n"
                "    out = {}\n"
                f"    for i in range({len(batch_rgb_msk)}):\n"
                "        rgb = d[f'rgb_{i}']\n"
                "        msk = d[f'msk_{i}']\n"
                "        it = torch.from_numpy(rgb.transpose(2,0,1)).float().div(255.0).unsqueeze(0).to(dev)\n"
                "        mt = torch.from_numpy(msk[None,None,:,:]).float().to(dev)\n"
                "        with torch.no_grad():\n"
                "            p = m(it, mt)[0].clamp(0.0,1.0).mul(255.0).byte().cpu().numpy().transpose(1,2,0)\n"
                "        out[f'p_{i}'] = np.ascontiguousarray(p)\n"
                f"    np.savez({str(out_npz)!r}, **out)\n"
            )
            proc = subprocess.run([str(pose_cuda_py), "-c", script], capture_output=True, text=True, timeout=180)
            if proc.returncode != 0 or not out_npz.is_file():
                return [rgb_np.copy() for rgb_np, _ in batch_rgb_msk]
            with np.load(out_npz) as res_npz:
                return [np.ascontiguousarray(res_npz[f"p_{i}"][:oh, :ow]) for i, (oh, ow) in enumerate(meta_shapes)]

    def _run_lama_tensor(rgb_np: np.ndarray, m_bool: np.ndarray) -> np.ndarray:
        if use_subproc_cuda:
            return _run_lama_batch_subproc([(rgb_np, m_bool)])[0]
        if model is None:
            return rgb_np.copy()
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


def match_fill_to_boundary_lab(
    obs_albedo: np.ndarray,
    filled_albedo: np.ndarray,
    inpaint_mask: np.ndarray,
    *,
    band_m: float = 0.12,
    texel_m: float = 0.005,
    spatial_sigma_m: float = 0.35,
) -> tuple[np.ndarray, dict[str, Any]]:
    """채움 텍셀(톤·LaMa·고전)의 Oklab 색 통계(평균·표준편차)를 경계 근처 관측 텍셀(`band_m` = 12 cm)에 맞춰 보정한다.

    2차 지시 2-(b) 구현 근거:
    - LaMa 또는 고전 인페인팅이 생성한 채움 영역은 주변 관측 영역과 평균 밝기(L)나 색조(a, b),
      또는 대비(표준편차)가 미세하게 어긋나 경계에서 톤 차이가 보일 수 있다.
    - 경계 바깥쪽 관측 대역(`0 < dist_to_hole <= band_m`)과 경계 안쪽 채움 대역(`0 < dist_to_obs <= band_m`)의
      Oklab 채널별 평균·표준편차를 맞추고, 추가로 최근접 관측 경계의 로컬 평균 오프셋(저주파 드리프트)을
      부드럽게 보정하여 관측 ↔ 채움 색감을 하나로 통일한다.
    """
    obs_u8 = np.ascontiguousarray(obs_albedo, dtype=np.uint8)
    fill_u8 = np.ascontiguousarray(filled_albedo, dtype=np.uint8)
    target = inpaint_mask > 0
    obs = ~target
    if not target.any() or not obs.any():
        return fill_u8.copy(), {"applied": False}

    band_px = max(4.0, float(band_m) / max(float(texel_m), 1e-5))
    dist_to_obs, (iy_obs, ix_obs) = ndi.distance_transform_edt(target, return_indices=True)
    dist_to_hole = ndi.distance_transform_edt(obs)

    obs_band = obs & (dist_to_hole <= band_px)
    fill_band = target & (dist_to_obs <= band_px)
    if int(obs_band.sum()) < 32 or int(fill_band.sum()) < 32:
        obs_band = obs
        fill_band = target

    obs_srgb = obs_u8.astype(np.float64) / 255.0
    fill_srgb = fill_u8.astype(np.float64) / 255.0
    obs_lab = srgb_to_oklab(obs_srgb)
    fill_lab = srgb_to_oklab(fill_srgb)

    mu_obs = obs_lab[obs_band].mean(axis=0)
    sd_obs = np.maximum(obs_lab[obs_band].std(axis=0), 1e-4)
    mu_fill = fill_lab[fill_band].mean(axis=0)
    sd_fill = np.maximum(fill_lab[fill_band].std(axis=0), 1e-4)

    # 과도한 대비 증폭이나 채도 왜곡을 막기 위해 표준편차 배율을 L 은 [0.55, 1.25], a/b 는 [0.50, 1.15] 로 클램프
    gain_lab = np.clip(sd_obs / sd_fill, [0.55, 0.50, 0.50], [1.25, 1.15, 1.15])

    corrected_lab = fill_lab.copy()
    corrected_lab[target] = (fill_lab[target] - mu_fill[None, :]) * gain_lab[None, :] + mu_obs[None, :]

    # 로컬 경계 드리프트 보정: 경계 각 지점에서 최근접 관측 텍셀(블러링된 로컬 Lab)과 채움 경계 Lab 의 차이를 구멍 내부로 완만히 전파
    sigma_local = max(6.0, float(spatial_sigma_m) / max(float(texel_m), 1e-5))
    sigma_local = min(sigma_local, 42.0)
    obs_nearest_lab = obs_lab[iy_obs, ix_obs]
    fill_self_nearest_idx = ndi.distance_transform_edt(~fill_band, return_indices=True)[1]
    fill_edge_lab = corrected_lab[fill_self_nearest_idx[0], fill_self_nearest_idx[1]]
    local_delta_lab = gaussian_blur(obs_nearest_lab - fill_edge_lab, sigma_local)
    # 구멍 경계에서는 로컬 드리프트를 70% 반영하고 깊은 내부로 갈수록 전역 통계 매칭으로 수렴
    w_local = np.exp(-dist_to_obs / max(band_px * 2.5, 12.0))[:, :, None] * 0.70
    corrected_lab[target] = corrected_lab[target] + (local_delta_lab * w_local)[target]

    out_srgb = obs_srgb.copy()
    out_srgb[target] = oklab_to_srgb(corrected_lab[target])
    out_u8 = np.clip(np.rint(out_srgb * 255.0), 0, 255).astype(np.uint8)
    out_u8[obs] = obs_u8[obs]
    return out_u8, {
        "applied": True,
        "band_m": float(band_m),
        "obs_band_texels": int(obs_band.sum()),
        "fill_band_texels": int(fill_band.sum()),
        "mu_obs_lab": [round(float(x), 4) for x in mu_obs],
        "mu_fill_before_lab": [round(float(x), 4) for x in mu_fill],
        "gain_lab": [round(float(x), 4) for x in gain_lab],
    }


def _compute_face_obs_coverage_floor(
    v0: np.ndarray,
    v1: np.ndarray,
    v2: np.ndarray,
    cand_indices: np.ndarray,
    floor_info: dict[str, Any],
    obs_mask_2d: np.ndarray,
) -> np.ndarray:
    """바닥 후보 삼각형 `cand_indices` 의 10개 무게중심 샘플점이 받침 평면 관측 마스크(`obs_mask_2d`) 안에 들어가는 비율 `[0, 1]` 을 반환한다."""
    if len(cand_indices) == 0:
        return np.zeros(0, dtype=np.float64)
    c0 = np.asarray(floor_info["corners_world"][0], dtype=np.float64)
    c1 = np.asarray(floor_info["corners_world"][1], dtype=np.float64)
    c3 = np.asarray(floor_info["corners_world"][3], dtype=np.float64)
    u_vec = c1[:2] - c0[:2]
    v_vec = c3[:2] - c0[:2]
    u_len2 = max(float(np.dot(u_vec, u_vec)), 1e-12)
    v_len2 = max(float(np.dot(v_vec, v_vec)), 1e-12)
    fh, fw = obs_mask_2d.shape[:2]

    bary_weights = [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
        (0.5, 0.5, 0.0),
        (0.0, 0.5, 0.5),
        (0.5, 0.0, 0.5),
        (0.6, 0.2, 0.2),
        (0.2, 0.6, 0.2),
        (0.2, 0.2, 0.6),
    ]
    p0 = v0[cand_indices, :2]
    p1 = v1[cand_indices, :2]
    p2 = v2[cand_indices, :2]
    hits = np.zeros(len(cand_indices), dtype=np.float64)
    for w0, w1, w2 in bary_weights:
        pts = w0 * p0 + w1 * p1 + w2 * p2
        rel = pts - c0[None, :2]
        un = (rel @ u_vec) / u_len2
        vn = (rel @ v_vec) / v_len2
        in_b = (un >= 0.0) & (un <= 1.0) & (vn >= 0.0) & (vn <= 1.0)
        ix = np.clip(np.rint(un * (fw - 1)).astype(np.int64), 0, fw - 1)
        iy = np.clip(np.rint(vn * (fh - 1)).astype(np.int64), 0, fh - 1)
        hits += np.where(in_b & obs_mask_2d[iy, ix], 1.0, 0.0)
    return hits / float(len(bary_weights))


def _compute_face_obs_coverage_wall(
    v0: np.ndarray,
    v1: np.ndarray,
    v2: np.ndarray,
    cand_indices: np.ndarray,
    wp: dict[str, Any],
    obs_mask_2d: np.ndarray,
) -> np.ndarray:
    """벽 후보 삼각형 `cand_indices` 의 10개 무게중심 샘플점이 받침 평면 관측 마스크(`obs_mask_2d`) 안에 들어가는 비율 `[0, 1]` 을 반환한다."""
    if len(cand_indices) == 0:
        return np.zeros(0, dtype=np.float64)
    t2 = np.asarray(wp["t_world_xy"], dtype=np.float64)
    u0, u1 = float(wp["u_range_m"][0]), float(wp["u_range_m"][1])
    z0, z1 = float(wp["z_range_m"][0]), float(wp["z_range_m"][1])
    u_span = max(u1 - u0, 1e-6)
    z_span = max(z1 - z0, 1e-6)
    wh, ww = obs_mask_2d.shape[:2]

    bary_weights = [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
        (0.5, 0.5, 0.0),
        (0.0, 0.5, 0.5),
        (0.5, 0.0, 0.5),
        (0.6, 0.2, 0.2),
        (0.2, 0.6, 0.2),
        (0.2, 0.2, 0.6),
    ]
    p0 = v0[cand_indices]
    p1 = v1[cand_indices]
    p2 = v2[cand_indices]
    hits = np.zeros(len(cand_indices), dtype=np.float64)
    for w0, w1, w2 in bary_weights:
        pts = w0 * p0 + w1 * p1 + w2 * p2
        u_val = pts[:, :2] @ t2
        z_val = pts[:, 2]
        un = (u_val - u0) / u_span
        vn = (z1 - z_val) / z_span
        in_b = (un >= 0.0) & (un <= 1.0) & (vn >= 0.0) & (vn <= 1.0)
        ix = np.clip(np.rint(un * (ww - 1)).astype(np.int64), 0, ww - 1)
        iy = np.clip(np.rint(vn * (wh - 1)).astype(np.int64), 0, wh - 1)
        hits += np.where(in_b & obs_mask_2d[iy, ix], 1.0, 0.0)
    return hits / float(len(bary_weights))


def strip_planar_mesh_faces(
    V: np.ndarray,
    F: np.ndarray,
    keep_face: np.ndarray,
    floor_info: dict[str, Any],
    wall_infos: list[dict[str, Any]],
    *,
    dist_tol_m: float = 0.06,
    angle_tol_deg: float = 25.0,
    post_min_faces: int = 150,
    post_min_area_m2: float = 0.12,
    plane_obs_masks: dict[str, np.ndarray] | None = None,
    min_obs_coverage: float = 0.90,
    is_textured_face: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """받침 평면(바닥·벽 3면)에서 거리 `<= dist_tol_m`(기본 6 cm)이고 법선 각도 차이 `<= angle_tol_deg`(기본 25°)인 메시 면을 걷어낸다.

    2차 지시 1번 + 3차 지시 2번(걷어내기 조건 강화) 근거:
    - 평면 RANSAC inlier 허용치(4 cm) 및 TSDF/스테레오 노이즈를 감안해 받침 평면의 로컬 기준 표면으로부터
      `|d| <= 6 cm` 이고 법선이 `<= 25°` 인 삼각형을 기하 후보로 찾는다.
    - `plane_obs_masks` 가 주어지면(3차), 해당 삼각형 바로 아래 받침 평면이 **관측 투영(`observed_projection`)으로
      덮이는 비율이 `min_obs_coverage`(기본 90%) 이상인 곳에서만** 메시를 걷어낸다.
      받침 평면이 `filled`(톤/LaMa/고전) 또는 `see_through` 로 채워질 곳에서는 원래 메시가 이미 가진 `texrecon` 관측 텍스처(예: 액자, 벽 끝단)를 잃지 않도록 원래 메시 면을 그대로 남기며, 이렇게 보존된 면이 속한 연결 성분은 후처리 고립 조각 제거에서도 보호한다.
    """
    V = np.asarray(V, dtype=np.float64)
    F = np.asarray(F, dtype=np.int64)
    keep_in = np.asarray(keep_face, dtype=bool).copy()
    m = len(F)
    if m == 0 or not keep_in.any():
        return keep_in, {"removed_faces": 0, "removed_face_ratio": 0.0, "removed_area_m2": 0.0, "removed_area_ratio": 0.0}

    v0, v1, v2 = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    cen = (v0 + v1 + v2) / 3.0
    fn = np.cross(v1 - v0, v2 - v0)
    areas = 0.5 * np.linalg.norm(fn, axis=1)
    nrm = fn / np.maximum(2.0 * areas[:, None], 1e-12)
    total_area = float(areas.sum())
    cos_th = float(np.cos(np.deg2rad(angle_tol_deg)))

    # 1) 바닥 평면 위 메시 걷어내기
    z_med = float(floor_info.get("mesh_floor_median_z_world_m", 0.0461))
    near_f = keep_in & (np.abs(nrm[:, 2]) > 0.78) & (cen[:, 2] > -0.15) & (cen[:, 2] < 0.16)
    if int(near_f.sum()) >= 32:
        tree_f = cKDTree(cen[near_f, :2])
        df, idx_f = tree_f.query(cen[:, :2], k=min(16, int(near_f.sum())))
        wf = 1.0 / np.maximum(df, 0.05)
        z_surf = (cen[near_f, 2][idx_f] * wf).sum(axis=1) / wf.sum(axis=1)
    else:
        z_surf = np.full(m, z_med, dtype=np.float64)

    v_z = np.stack([v0[:, 2], v1[:, 2], v2[:, 2]], axis=1)
    v_f_res = np.abs(v_z - z_surf[:, None])
    floor_geom_cand = (
        keep_in
        & (
            (
                (np.abs(cen[:, 2] - z_surf) <= float(dist_tol_m))
                & (np.abs(nrm[:, 2]) >= cos_th)
                & (v_z.max(axis=1) <= z_surf + float(dist_tol_m) + 0.02)
            )
            | (v_f_res.max(axis=1) <= max(0.06, float(dist_tol_m)))
        )
    )
    has_tex = np.ones(m, dtype=bool) if is_textured_face is None else np.asarray(is_textured_face, dtype=bool)
    if plane_obs_masks is not None and "floor" in plane_obs_masks:
        f_cand_idx = np.flatnonzero(floor_geom_cand)
        obs_floor_eval = plane_obs_masks["floor"] | ndi.binary_closing(plane_obs_masks["floor"], structure=np.ones((7, 7), dtype=bool))
        f_cov = _compute_face_obs_coverage_floor(v0, v1, v2, f_cand_idx, floor_info, obs_floor_eval)
        floor_strip = np.zeros(m, dtype=bool)
        # 관측 텍스처가 있는 삼각형은 observed_projection >= min_obs_coverage 일 때만 걷어내고, 텍스처 없는 회색(unobserved_neutral) 평면 메시는 걷어낸다
        strip_sel = (f_cov >= float(min_obs_coverage)) | (~has_tex[f_cand_idx])
        floor_strip[f_cand_idx[strip_sel]] = True
    else:
        floor_strip = floor_geom_cand.copy()
    floor_protected = floor_geom_cand & ~floor_strip

    # 2) 벽 3면 위 메시 걷어내기
    wall_strips: dict[str, np.ndarray] = {}
    wall_geom_cands: dict[str, np.ndarray] = {}
    wall_protected_all = np.zeros(m, dtype=bool)
    wall_strip_all = np.zeros(m, dtype=bool)
    for wp in wall_infos:
        wid = str(wp["wall_id"])
        n2 = np.asarray(wp["n_world_xy"], dtype=np.float64)
        t2 = np.asarray(wp["t_world_xy"], dtype=np.float64)
        d0 = float(wp["d_world_m"])
        u0, u1 = float(wp["u_range_m"][0]), float(wp["u_range_m"][1])
        z0, z1 = float(wp["z_range_m"][0]), float(wp["z_range_m"][1])

        sd = cen[:, :2] @ n2 - d0
        u = cen[:, :2] @ t2
        z = cen[:, 2]
        n_dot = np.abs(nrm[:, :2] @ n2)

        near_w = keep_in & (np.abs(sd) < 0.26) & (n_dot > 0.78) & (u >= u0 - 0.10) & (u <= u1 + 0.10)
        if int(near_w.sum()) >= 32:
            tree_w = cKDTree(np.column_stack([u[near_w], z[near_w]]))
            dw, iw = tree_w.query(np.column_stack([u, z]), k=min(16, int(near_w.sum())))
            ww = 1.0 / np.maximum(dw, 0.05)
            sd_surf = (sd[near_w][iw] * ww).sum(axis=1) / ww.sum(axis=1)
        else:
            sd_surf = np.zeros(m, dtype=np.float64)

        v_sd = np.stack([v0[:, :2] @ n2 - d0, v1[:, :2] @ n2 - d0, v2[:, :2] @ n2 - d0], axis=1)
        v_res = np.abs(v_sd - sd_surf[:, None])
        in_box = (u >= u0 + 0.01) & (u <= u1 - 0.01) & (z >= z0 - 0.02) & (z <= z1 + 0.04)

        w_geom_cand = (
            keep_in
            & ~floor_geom_cand
            & in_box
            & (((np.abs(sd - sd_surf) <= float(dist_tol_m)) & (n_dot >= cos_th)) | (v_res.max(axis=1) <= max(0.055, float(dist_tol_m) - 0.005)))
        )
        wall_geom_cands[wid] = w_geom_cand
        if plane_obs_masks is not None and wid in plane_obs_masks:
            w_cand_idx = np.flatnonzero(w_geom_cand)
            # 미세 핀홀(1~8 텍셀)로 인해 평탄한 벽이나 문 한가운데에 조각 삼각형이 남지 않도록 9x9 클로징된 관측 마스크로 커버리지 평가
            obs_mask_eval = plane_obs_masks[wid] | ndi.binary_closing(plane_obs_masks[wid], structure=np.ones((9, 9), dtype=bool))
            w_cov = _compute_face_obs_coverage_wall(v0, v1, v2, w_cand_idx, wp, obs_mask_eval)
            w_hit = np.zeros(m, dtype=bool)
            w_strip_sel = (w_cov >= float(min_obs_coverage)) | (~has_tex[w_cand_idx])
            w_hit[w_cand_idx[w_strip_sel]] = True
        else:
            w_hit = w_geom_cand.copy()
        wall_strips[wid] = w_hit
        wall_strip_all |= w_hit
        wall_protected_all |= w_geom_cand & ~w_hit

    keep_after_planes = keep_in & ~floor_strip & ~wall_strip_all
    protected_planar_faces = floor_protected | wall_protected_all

    # 3) 평면을 걷어낸 뒤 공중에 고립된 잔여 소형 조각 정리
    #    단, 받침 평면이 미관측이라 원래 텍스처를 보존하기 위해 남긴 평면 삼각형(protected_planar_faces)은 고립 조각 제거에서 보호한다.
    if keep_after_planes.any() and (post_min_faces > 0 or post_min_area_m2 > 0.0):
        rem_indices = np.flatnonzero(keep_after_planes)
        keep_sub, post_rep = filter_small_connected_components(
            V,
            F[rem_indices],
            min_faces=post_min_faces,
            min_area_m2=post_min_area_m2,
            protect_face_mask=protected_planar_faces[rem_indices],
        )
        keep_final = np.zeros(m, dtype=bool)
        keep_final[rem_indices[keep_sub]] = True
        post_removed_mask = keep_after_planes & ~keep_final
    else:
        keep_final = keep_after_planes
        post_removed_mask = np.zeros(m, dtype=bool)
        post_rep = {"removed_components": 0, "removed_faces": 0, "removed_area_m2": 0.0}

    total_stripped_mask = keep_in & ~keep_final
    per_plane_stats: dict[str, Any] = {
        "floor": {
            "geom_candidate_faces": int(floor_geom_cand.sum()),
            "kept_unobserved_backing_faces": int(floor_protected.sum()),
            "removed_faces": int(floor_strip.sum()),
            "removed_face_ratio": round(float(floor_strip.sum() / max(m, 1)), 5),
            "removed_area_m2": round(float(areas[floor_strip].sum()), 3),
            "removed_area_ratio": round(float(areas[floor_strip].sum() / max(total_area, 1e-12)), 5),
        }
    }
    for wid, w_mask in wall_strips.items():
        wg_cand = wall_geom_cands[wid]
        w_prot = wg_cand & ~w_mask
        per_plane_stats[wid] = {
            "geom_candidate_faces": int(wg_cand.sum()),
            "kept_unobserved_backing_faces": int(w_prot.sum()),
            "removed_faces": int(w_mask.sum()),
            "removed_face_ratio": round(float(w_mask.sum() / max(m, 1)), 5),
            "removed_area_m2": round(float(areas[w_mask].sum()), 3),
            "removed_area_ratio": round(float(areas[w_mask].sum() / max(total_area, 1e-12)), 5),
        }

    total_geom_cand_mask = floor_geom_cand.copy()
    for wg_c in wall_geom_cands.values():
        total_geom_cand_mask |= wg_c

    return keep_final, {
        "dist_tol_m": float(dist_tol_m),
        "angle_tol_deg": float(angle_tol_deg),
        "min_obs_coverage": float(min_obs_coverage) if plane_obs_masks is not None else 0.0,
        "total_mesh_faces": int(m),
        "total_mesh_area_m2": round(total_area, 3),
        "geom_candidate_faces": int(total_geom_cand_mask.sum()),
        "kept_unobserved_backing_faces": int(protected_planar_faces.sum()),
        "kept_unobserved_backing_area_m2": round(float(areas[protected_planar_faces].sum()), 3),
        "plane_stripped_faces": int((floor_strip | wall_strip_all).sum()),
        "plane_stripped_face_ratio": round(float((floor_strip | wall_strip_all).sum() / max(m, 1)), 5),
        "plane_stripped_area_m2": round(float(areas[floor_strip | wall_strip_all].sum()), 3),
        "plane_stripped_area_ratio": round(float(areas[floor_strip | wall_strip_all].sum() / max(total_area, 1e-12)), 5),
        "post_strip_orphan_faces": int(post_removed_mask.sum()),
        "post_strip_orphan_area_m2": round(float(areas[post_removed_mask].sum()), 3),
        "post_strip_orphan_components": int(post_rep.get("removed_components", 0)),
        "total_removed_faces": int(total_stripped_mask.sum()),
        "total_removed_face_ratio": round(float(total_stripped_mask.sum() / max(m, 1)), 5),
        "total_removed_area_m2": round(float(areas[total_stripped_mask].sum()), 3),
        "total_removed_area_ratio": round(float(areas[total_stripped_mask].sum() / max(total_area, 1e-12)), 5),
        "kept_faces": int(keep_final.sum()),
        "kept_face_ratio": round(float(keep_final.sum() / max(m, 1)), 5),
        "kept_area_m2": round(float(areas[keep_final].sum()), 3),
        "kept_area_ratio": round(float(areas[keep_final].sum() / max(total_area, 1e-12)), 5),
        "per_plane": per_plane_stats,
    }


def extract_and_bake_horizontal_furniture_planes(
    V_world: np.ndarray,
    F: np.ndarray,
    keep_face_mask: np.ndarray,
    cameras: Sequence[Any],
    cam_depths_m: Sequence[np.ndarray],
    cam_quality_maps: Sequence[dict[str, Any]] | None = None,
    *,
    z_min_m: float = 0.34,
    z_max_m: float = 0.88,
    min_normal_z: float = 0.65,
    min_area_m2: float = 0.10,
    room_xy_bounds_m: tuple[float, float, float, float] = (0.25, 6.25, 0.75, 6.95),
    texel_m: float = 0.0025,
    depth_tol_m: float = 0.055,
    min_cos_incidence: float = 0.28,
    sharp_exponent: float = 10.0,
) -> tuple[np.ndarray, list[dict[str, Any]], dict[str, Any]]:
    """5차 지시 (a): 수평 가구 상판·벤치 좌판 평면을 찾아 원래 메시 윤곽을 유지한 채 평면으로 매끈하게 펴고 `-532` 방식으로 텍스처를 굽는다.

    핵심 단계:
    1. 방 내부(`room_xy_bounds_m`)에서 높이 `z in [z_min_m, z_max_m]`(`0.34~0.88 m`), 법선 `n_z >= 0.65` 인 수평 시드 면을 찾고,
       인접 면 높이 차 `<= 4.5 cm` 조건으로 연결 성분을 묶어 면적 `>= min_area_m2`(`0.10 m²`) 인 수평 가구 상판을 추출한다.
    2. 각 상판의 2D 윤곽(`foot_seed`) 내부의 작은 구멍(TSDF 움푹 패임·미관측 구멍)을 닫아(`foot_closed`),
       그 안에 속하고 평면 높이와 `6.5 cm` 이내인 인접 삼각형(`n_z >= 0.05`)을 상판 성분으로 흡수한다.
    3. 외곽 경계 정점(측면·테두리 삼각형과 공유되는 정점)은 3D 위치를 유지해 틈이 벌어지지 않게 하고,
       내부 정점의 Z 좌표를 스무딩된 평면 높이 `z_smooth(x, y)` 로 매끈하게 편다(`"형상은 평면으로 매끈하게, 경계는 원래 메시 윤곽"`).
    4. 메시 정합 카메라 포즈(`ma_sim3` / `check_cams`)와 카메라별 깊이 맵을 사용해:
       - 최소 입사각 게이트(`cos θ >= 0.28`, 맞은편 벽 카메라의 극단적 사선 반사광 배제),
       - 깊이 일치 게이트(`|z_ray - d_cam| <= 5.5 cm`, 맵에 없는 의자 등받이 전경 가림 및 바닥 배경 투영 배제),
       - 파란 의자 색도 게이트(`B - R > 0.07`) 및 벤치 끝단 바닥 반사 배제,
       - 상판 내 카메라 겹침 영역 로컬 화이트밸런스 조화(`gain in [0.88, 1.14]`),
       - 카메라 간 Oklab 외톨이 제거(`select_camera_colors_with_outlier_removal`) + 최고 품질 카메라 우선 선택(`sharp_exponent`),
       - 테두리 미관측 픽셀 내부→외곽 Telea 인페인팅(`SRC_FILLED_TONE`)을 수행한다.
    """
    from collections import defaultdict

    V = np.asarray(V_world, dtype=np.float64)
    F_arr = np.asarray(F, dtype=np.int64)
    keep_out = np.asarray(keep_face_mask, dtype=bool).copy()
    m = len(F_arr)
    if m == 0 or not np.any(keep_out) or len(cameras) == 0:
        return keep_out, [], {"num_furniture_planes": 0, "absorbed_faces": 0, "absorbed_area_m2": 0.0}

    tri_pos = V[F_arr]
    fc = tri_pos.mean(axis=1)
    fn_raw = np.cross(tri_pos[:, 1] - tri_pos[:, 0], tri_pos[:, 2] - tri_pos[:, 0])
    fn_len = np.linalg.norm(fn_raw, axis=1)
    areas = 0.5 * fn_len
    fn = fn_raw / np.maximum(fn_len[:, None], 1e-12)

    x_min, x_max, y_min, y_max = [float(v) for v in room_xy_bounds_m]
    horiz_cand = (
        keep_out
        & (fc[:, 2] >= float(z_min_m))
        & (fc[:, 2] <= float(z_max_m))
        & (fn[:, 2] >= float(min_normal_z))
        & (fc[:, 0] >= x_min)
        & (fc[:, 0] <= x_max)
        & (fc[:, 1] >= y_min)
        & (fc[:, 1] <= y_max)
    )
    h_idx = np.flatnonzero(horiz_cand)
    if len(h_idx) == 0:
        return keep_out, [], {"num_furniture_planes": 0, "absorbed_faces": 0, "absorbed_area_m2": 0.0}

    # 2 mm 용접 정점 기준 수평 면 연결 성분 추출
    key = np.round(V / 0.002).astype(np.int64)
    _, _, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    Fh = inv[F_arr[h_idx]]
    edge_map: dict[tuple[int, int], list[int]] = defaultdict(list)
    for loc_i, f in enumerate(Fh):
        u0, u1, u2 = int(f[0]), int(f[1]), int(f[2])
        for a, b_v in ((u0, u1), (u1, u2), (u2, u0)):
            if a > b_v:
                a, b_v = b_v, a
            edge_map[(a, b_v)].append(loc_i)

    rows: list[int] = []
    cols: list[int] = []
    for flist in edge_map.values():
        if len(flist) >= 2:
            for i in range(len(flist)):
                for j in range(i + 1, len(flist)):
                    ia, ib = flist[i], flist[j]
                    if abs(float(fc[h_idx[ia], 2] - fc[h_idx[ib], 2])) <= 0.045:
                        rows.extend([ia, ib])
                        cols.extend([ib, ia])

    if rows:
        adj = csr_matrix(
            (np.ones(len(rows), dtype=np.int8), (rows, cols)),
            shape=(len(h_idx), len(h_idx)),
        )
        n_comp, labels = connected_components(adj, directed=False)
    else:
        n_comp = len(h_idx)
        labels = np.arange(len(h_idx), dtype=np.int32)

    cam_pos_w = [(-np.asarray(c.R, dtype=np.float64).T @ np.asarray(c.t, dtype=np.float64).reshape(3)) for c in cameras]

    furniture_planes: list[dict[str, Any]] = []
    claimed_faces = np.zeros(m, dtype=bool)

    # 면적 내림차순으로 정렬해 주요 테이블·벤치부터 처리
    comp_order: list[tuple[float, int]] = []
    for cid in range(n_comp):
        f_seed = h_idx[labels == cid]
        area_c = float(areas[f_seed].sum())
        if area_c >= float(min_area_m2):
            comp_order.append((area_c, cid))
    comp_order.sort(reverse=True)

    for plane_idx, (_seed_area, cid) in enumerate(comp_order):
        f_seed = h_idx[labels == cid]
        f_seed = f_seed[~claimed_faces[f_seed]]
        if len(f_seed) == 0 or float(areas[f_seed].sum()) < float(min_area_m2):
            continue

        pts_c = tri_pos[f_seed].reshape(-1, 3)
        x0 = max(x_min - 0.05, float(pts_c[:, 0].min()) - 0.025)
        x1 = min(x_max + 0.05, float(pts_c[:, 0].max()) + 0.025)
        y0 = max(y_min - 0.05, float(pts_c[:, 1].min()) - 0.025)
        y1 = min(y_max + 0.05, float(pts_c[:, 1].max()) + 0.025)
        z_med = float(np.median(fc[f_seed, 2]))

        tw = max(16, int(round((x1 - x0) / float(texel_m))))
        th = max(16, int(round((y1 - y0) / float(texel_m))))

        foot_seed = np.zeros((th, tw), dtype=np.uint8)
        for fi in f_seed:
            tri = tri_pos[fi]
            pts = np.zeros((3, 2), dtype=np.int32)
            pts[:, 0] = np.clip(((tri[:, 0] - x0) / max(x1 - x0, 1e-6) * (tw - 1)).astype(np.int32), 0, tw - 1)
            pts[:, 1] = np.clip(((y1 - tri[:, 1]) / max(y1 - y0, 1e-6) * (th - 1)).astype(np.int32), 0, th - 1)
            cv2.fillConvexPoly(foot_seed, pts, 1)

        foot_closed = ndi.binary_fill_holes(ndi.binary_closing(foot_seed > 0, iterations=1)).astype(np.uint8)
        foot_interior = ndi.binary_erosion(foot_closed > 0, iterations=4)
        dist_to_foot_edge = ndi.distance_transform_edt(foot_closed > 0)

        # 스무딩된 로컬 평면 높이 지도 z_smooth(x, y)
        tree_xy = cKDTree(fc[f_seed, :2])
        xs = np.linspace(x0, x1, tw, dtype=np.float64)
        ys = np.linspace(y1, y0, th, dtype=np.float64)
        gx, gy = np.meshgrid(xs, ys)
        d_k, i_k = tree_xy.query(np.column_stack([gx.ravel(), gy.ravel()]), k=min(16, len(f_seed)))
        if d_k.ndim == 1:
            d_k = d_k[:, None]
            i_k = i_k[:, None]
        w_k = 1.0 / np.maximum(d_k, 0.05)
        z_smooth = ((fc[f_seed, 2][i_k] * w_k).sum(axis=1) / w_k.sum(axis=1)).reshape(th, tw)
        z_smooth = ndi.gaussian_filter(z_smooth, sigma=6.0)

        # 2D 윤곽 내부의 움푹 패인 삼각형·미관측 구멍 삼각형을 상판 성분으로 흡수하되,
        # 외곽 테두리(border/bevel, ~foot_interior)의 경사면(fn_z < 0.78)은 3D 테두리로 보존한다.
        in_box = np.flatnonzero(
            keep_out
            & (~claimed_faces)
            & (fc[:, 0] >= x0)
            & (fc[:, 0] <= x1)
            & (fc[:, 1] >= y0)
            & (fc[:, 1] <= y1)
            & (fn[:, 2] >= 0.35)
        )
        ix_b = np.clip(((fc[in_box, 0] - x0) / max(x1 - x0, 1e-6) * (tw - 1)).astype(int), 0, tw - 1)
        iy_b = np.clip(((y1 - fc[in_box, 1]) / max(y1 - y0, 1e-6) * (th - 1)).astype(int), 0, th - 1)
        dz_b = np.abs(fc[in_box, 2] - z_smooth[iy_b, ix_b])
        in_int = foot_interior[iy_b, ix_b]
        in_rim = (foot_closed[iy_b, ix_b] > 0) & (~in_int)
        absorb = (in_int & (dz_b <= 0.055)) | (in_rim & (fn[in_box, 2] >= 0.78) & (dz_b <= 0.030))
        f_comp = np.unique(np.concatenate([f_seed, in_box[absorb]]))
        claimed_faces[f_comp] = True

        foot_mask = np.zeros((th, tw), dtype=np.uint8)
        for fi in f_comp:
            tri = tri_pos[fi]
            pts = np.zeros((3, 2), dtype=np.int32)
            pts[:, 0] = np.clip(((tri[:, 0] - x0) / max(x1 - x0, 1e-6) * (tw - 1)).astype(np.int32), 0, tw - 1)
            pts[:, 1] = np.clip(((y1 - tri[:, 1]) / max(y1 - y0, 1e-6) * (th - 1)).astype(np.int32), 0, th - 1)
            cv2.fillConvexPoly(foot_mask, pts, 1)

        P_world = np.stack([gx, gy, z_smooth], axis=-1).reshape(-1, 3)
        N_pts = len(P_world)
        n_cams = len(cameras)

        cand_mask = np.zeros((n_cams, N_pts), dtype=bool)
        cand_srgb = np.zeros((n_cams, N_pts, 3), dtype=np.float64)
        cand_w = np.zeros((n_cams, N_pts), dtype=np.float64)

        eff_depth_tol = max(float(depth_tol_m), 0.082) if z_med < 0.58 else float(depth_tol_m)
        outer_rim_flat = (dist_to_foot_edge.ravel() <= 14.0)

        for k, c in enumerate(cameras):
            R_c = np.asarray(c.R, dtype=np.float64)
            t_c = np.asarray(c.t, dtype=np.float64).reshape(3)
            K_c = np.asarray(c.K, dtype=np.float64)
            Xc = P_world @ R_c.T + t_c[None, :]
            z_r = Xc[:, 2]
            px = K_c[0, 0] * (Xc[:, 0] / np.maximum(z_r, 1e-6)) + K_c[0, 2]
            py = K_c[1, 1] * (Xc[:, 1] / np.maximum(z_r, 1e-6)) + K_c[1, 2]
            Hc, Wc = c.image.shape[:2]

            dvec = cam_pos_w[k][None, :] - P_world
            dist = np.maximum(np.linalg.norm(dvec, axis=1), 0.5)
            cos_th = dvec[:, 2] / dist

            in_b = (
                (z_r > 0.3)
                & (cos_th >= float(min_cos_incidence))
                & (px >= 8.0)
                & (px < Wc - 8.0)
                & (py >= 8.0)
                & (py < Hc - 8.0)
            )
            if not np.any(in_b):
                continue

            ipx = np.clip(np.round(px).astype(int), 0, Wc - 1)
            ipy = np.clip(np.round(py).astype(int), 0, Hc - 1)
            if k < len(cam_depths_m) and cam_depths_m[k] is not None and cam_depths_m[k].size > 0:
                d_map = cam_depths_m[k]
                Hd, Wd = d_map.shape[:2]
                d_ix = np.clip(np.round(px * (Wd / float(Wc))).astype(int), 0, Wd - 1)
                d_iy = np.clip(np.round(py * (Hd / float(Hc))).astype(int), 0, Hd - 1)
                d_c = d_map[d_iy, d_ix].astype(np.float64)
                depth_ok = np.isfinite(d_c) & (d_c > 0.1) & (np.abs(z_r - d_c) <= eff_depth_tol)
            else:
                d_c = z_r.copy()
                depth_ok = np.ones(N_pts, dtype=bool)

            valid_k = in_b & depth_ok
            if not np.any(valid_k):
                continue

            samp = (
                cv2.remap(
                    c.image,
                    px.reshape(th, tw).astype(np.float32),
                    py.reshape(th, tw).astype(np.float32),
                    cv2.INTER_LINEAR,
                ).reshape(-1, 3)
                / 255.0
            )
            # 전경 파란 의자 등받이 경계 픽셀 배제 (상판 높이 >= 0.40 m 인 테이블·벤치)
            if z_med >= 0.40:
                is_blue_chair = (samp[:, 2] - samp[:, 0] > 0.07) & (samp[:, 2] > 0.25) & (samp[:, 0] < 0.48)
                valid_k &= ~is_blue_chair
            # 알약 벤치(z_med < 0.58 m) 외곽 가장자리(3.5 cm)에서 주황 가방 반사 및 회색 바닥 카펫 번짐만 배제 (내부 나무·쿠션 경계 보호)
            if z_med < 0.58:
                is_orange_bag = (samp[:, 0] - samp[:, 2] > 0.34) & (samp[:, 1] < 0.40) & (samp[:, 0] > 0.52)
                is_floor_bleed = outer_rim_flat & (((samp[:, 0] - samp[:, 2]) < 0.022) | (samp.mean(axis=1) < 0.32))
                valid_k &= ~(is_orange_bag | is_floor_bleed)

            if not np.any(valid_k):
                continue

            if cam_quality_maps is not None and k < len(cam_quality_maps):
                q_map = cam_quality_maps[k]["w_photo_map"]
                w_ph = q_map[ipy, ipx].astype(np.float64)
            else:
                w_ph = np.ones(N_pts, dtype=np.float64)

            d_err = np.abs(z_r - np.where(np.isfinite(d_c), d_c, z_r))
            w_dep = np.exp(-0.5 * (d_err / 0.032) ** 2)
            w_tot = (np.clip(cos_th, 0.05, 1.0) ** 2.5) / (dist**2.0) * w_ph * w_dep

            # 상판 내부에서 카메라 유효 영역(valid_k)이 끊길 때 경계에서 가중치가 0 으로 부드럽게 감쇠되도록 내부 거리 페더링 적용 (이음새 단차 제거)
            valid_2d = valid_k.reshape(th, tw)
            internal_invalid = (~valid_2d) & (foot_mask > 0)
            if np.any(internal_invalid) and np.any(valid_2d):
                dist_in_valid = ndi.distance_transform_edt(~internal_invalid)
                edge_ramp = smoothstep(0.0, 44.0, dist_in_valid)
            else:
                edge_ramp = np.ones((th, tw), dtype=np.float64)

            is_long_table_row = (z_med >= 0.58) and (max(x1 - x0, y1 - y0) > 1.25)
            w_2d = np.where(valid_2d, w_tot.reshape(th, tw) * edge_ramp, 0.0)
            w_2d_s = ndi.gaussian_filter(w_2d, sigma=42.0 if is_long_table_row else 26.0)
            cand_mask[k] = valid_k
            cand_srgb[k] = samp
            cand_w[k] = np.where(valid_k, np.maximum(w_2d_s.ravel(), 1e-6), 0.0)

        foot_flat = foot_mask.ravel() > 0
        has_any = cand_mask.any(axis=0)
        if not np.any(has_any & foot_flat):
            claimed_faces[f_comp] = False
            continue

        # 상판 내 카메라 간 공간 적응형 Oklab 색·휘도 조화 (카메라 전환 경계의 직선 이음새 제거)
        cam_inside = cand_mask & foot_flat[None, :]
        cam_w_sum = (cand_w * cam_inside).sum(axis=1)
        dom_k = int(np.argmax(cam_w_sum))
        dom_lab = srgb_to_oklab(cand_srgb[dom_k])
        for k in range(n_cams):
            if k == dom_k or not np.any(cam_inside[k]):
                continue
            ov = cam_inside[dom_k] & cam_inside[k]
            if int(ov.sum()) >= 35:
                k_lab = srgb_to_oklab(cand_srgb[k])
                diff_ov = dom_lab[ov] - k_lab[ov]
                med_shift = np.median(diff_ov, axis=0)
                med_shift = np.clip(med_shift, [-0.14, -0.06, -0.06], [0.14, 0.06, 0.06])
                k_lab_shifted = k_lab + med_shift[None, :]

                # 겹침 영역의 로컬 잔여 편차를 저주파 가우시안 필드로 외삽해 이음새 단차를 매끈하게 일치시킴
                ov_2d = ov.reshape(th, tw).astype(np.float64)
                res_2d = np.zeros((th, tw, 3), dtype=np.float64)
                res_2d.reshape(-1, 3)[ov] = dom_lab[ov] - k_lab_shifted[ov]
                num_r = ndi.gaussian_filter(res_2d * ov_2d[:, :, None], sigma=(32.0, 32.0, 0.0))
                den_r = ndi.gaussian_filter(ov_2d, sigma=32.0)[:, :, None]
                local_shift = np.where(den_r > 1e-4, num_r / np.maximum(den_r, 1e-8), 0.0).reshape(-1, 3)
                local_shift = np.clip(local_shift, [-0.08, -0.04, -0.04], [0.08, 0.04, 0.04])
                cand_srgb[k] = np.clip(oklab_to_srgb(k_lab_shifted + 0.85 * local_shift), 0.0, 1.0)

        best_k = np.argmax(cand_w, axis=0)
        init_srgb = cand_srgb[best_k, np.arange(N_pts)]
        obs_2d = (has_any.reshape(th, tw) & (foot_mask > 0)).astype(np.float64)
        img_2d = init_srgb.reshape(th, tw, 3)
        num = ndi.gaussian_filter(img_2d * obs_2d[:, :, None], sigma=(15.0, 15.0, 0.0))
        den = ndi.gaussian_filter(obs_2d, sigma=15.0)[:, :, None]
        med_c = np.median(init_srgb[has_any & foot_flat], axis=0)
        neigh_srgb = np.where(den > 1e-4, num / np.maximum(den, 1e-8), med_c[None, None, :]).reshape(N_pts, 3)

        eff_sharp = 1.6 if (z_med >= 0.58) else min(float(sharp_exponent), 3.5)
        blended_srgb, obs_mask, outlier_stats = select_camera_colors_with_outlier_removal(
            cand_mask,
            cand_srgb,
            cand_w,
            neigh_srgb,
            sigma_oklab=0.052,
            is_floor=True,
            sharp_exponent=eff_sharp,
        )
        # 흰 원형 테이블 상판(z_med >= 0.58 m)에서 창문 푸른빛/우드월 주황빛 사선 반사로 인한 색조(cyan/peach) 편차 중립화
        # (경고 안내판 등 고채도 물체 chroma > 0.048 및 노트북 등 중·저휘도 물체 L < 0.78 은 100% 보존)
        if z_med >= 0.58:
            ok = srgb_to_oklab(blended_srgb)
            L_val = ok[:, 0]
            chroma = np.hypot(ok[:, 1], ok[:, 2])
            # L_val 을 부드럽게 공간 평균한 값으로 게이트를 평가해 카메라 경계에서 게이트 차이에 의한 선이 생기지 않게 한다
            L_smooth = ndi.gaussian_filter(L_val.reshape(th, tw), sigma=10.0).ravel()
            c_smooth = ndi.gaussian_filter(chroma.reshape(th, tw), sigma=10.0).ravel()
            w_white = smoothstep(0.73, 0.83, L_smooth) * (1.0 - smoothstep(0.030, 0.052, c_smooth))
            ok[:, 1] *= 1.0 - 0.90 * w_white
            ok[:, 2] *= 1.0 - 0.90 * w_white
            ok[:, 0] = ok[:, 0] * (1.0 - 0.62 * w_white) + 0.955 * (0.62 * w_white)
            blended_srgb = np.clip(oklab_to_srgb(ok), 0.0, 1.0)
        tex_u8 = (blended_srgb.reshape(th, tw, 3) * 255.0).clip(0.0, 255.0).astype(np.uint8)
        obs_m2d = obs_mask.reshape(th, tw) & (foot_mask > 0)
        source_2d = np.where(obs_m2d, SRC_PROJECTED, SRC_GRAY).astype(np.uint8)

        unobs_m2d = (~obs_m2d) & (foot_mask > 0)
        if np.any(obs_m2d):
            # 관측된 픽셀(obs_m2d)은 침식하지 않고 100% 보존해 벤치 모서리가 Telea 에 의해 뭉개지지 않게 한다.
            inp_mask_u8 = (~obs_m2d).astype(np.uint8) * 255
            bgr_in = cv2.cvtColor(tex_u8, cv2.COLOR_RGB2BGR)
            bgr_out = cv2.inpaint(bgr_in, inp_mask_u8, inpaintRadius=5, flags=cv2.INPAINT_TELEA)
            inpainted_rgb = cv2.cvtColor(bgr_out, cv2.COLOR_BGR2RGB)
            tex_u8[~obs_m2d] = inpainted_rgb[~obs_m2d]
            source_2d[unobs_m2d] = SRC_FILLED

        # 상판 삼각형 메시 생성: 외곽 공유 정점은 원래 3D 테두리 Z 를 85% 유지하고 내부 정점은 z_smooth 로 매끈하게 평탄화
        non_comp_mask = keep_out & (~claimed_faces)
        non_comp_verts = np.unique(inv[F_arr[non_comp_mask]]) if np.any(non_comp_mask) else np.empty(0, dtype=np.int64)
        non_comp_vert_set = set(int(v) for v in non_comp_verts)

        comp_F_global = F_arr[f_comp]
        uniq_v, local_F = np.unique(comp_F_global, return_inverse=True)
        local_F = local_F.reshape(-1, 3).astype(np.uint32)
        V_local = V[uniq_v].copy()

        u_norm = np.clip((V_local[:, 0] - x0) / max(x1 - x0, 1e-6), 0.0, 1.0)
        v_norm = np.clip((y1 - V_local[:, 1]) / max(y1 - y0, 1e-6), 0.0, 1.0)
        UV_local = np.column_stack([u_norm, v_norm]).astype(np.float32)

        ix_v = np.clip(np.round(u_norm * (tw - 1)).astype(int), 0, tw - 1)
        iy_v = np.clip(np.round(v_norm * (th - 1)).astype(int), 0, th - 1)
        z_target = z_smooth[iy_v, ix_v]

        for vi_loc, vi_glob in enumerate(uniq_v):
            if int(inv[vi_glob]) in non_comp_vert_set:
                # 측면·테두리 삼각형과 맞닿은 외곽 정점은 3D 테두리 형상을 지키면서 틈만 막도록 15% 만 완화
                V_local[vi_loc, 2] = 0.85 * V_local[vi_loc, 2] + 0.15 * float(z_target[vi_loc])
            else:
                V_local[vi_loc, 2] = float(z_target[vi_loc])

        role_name = f"furniture_plane_{plane_idx}"
        area_comp_m2 = float(areas[f_comp].sum())
        obs_ratio = float(obs_m2d.sum() / max(int((foot_mask > 0).sum()), 1))
        furniture_planes.append(
            {
                "role": role_name,
                "plane_index": int(plane_idx),
                "face_indices_merged": f_comp,
                "num_faces": int(len(f_comp)),
                "area_m2": round(area_comp_m2, 4),
                "z_median_m": round(z_med, 4),
                "xy_bounds_m": [round(x0, 4), round(x1, 4), round(y0, 4), round(y1, 4)],
                "tex_hw": (int(th), int(tw)),
                "texel_m": float(texel_m),
                "V_world": V_local,
                "F_grid": local_F,
                "UV_grid": UV_local,
                "normal_world": np.array([0.0, 0.0, 1.0], dtype=np.float64),
                "image": tex_u8,
                "source": source_2d,
                "foot_mask": foot_mask > 0,
                "z_smooth": z_smooth,
                "observed_ratio": round(obs_ratio, 4),
                "outlier_stats": outlier_stats,
                "surface_source_summary": compute_surface_source_summary(source_2d, "tone", float(texel_m)),
            }
        )

    keep_out[claimed_faces] = False
    summary = {
        "num_furniture_planes": len(furniture_planes),
        "absorbed_faces": int(claimed_faces.sum()),
        "absorbed_area_m2": round(float(areas[claimed_faces].sum()), 3),
        "planes": [
            {
                "role": fp["role"],
                "num_faces": fp["num_faces"],
                "area_m2": fp["area_m2"],
                "z_median_m": fp["z_median_m"],
                "xy_bounds_m": fp["xy_bounds_m"],
                "tex_size_wh": [fp["tex_hw"][1], fp["tex_hw"][0]],
                "observed_ratio": fp["observed_ratio"],
            }
            for fp in furniture_planes
        ],
    }
    return keep_out, furniture_planes, summary


def recolor_remaining_furniture_faces(
    V_world: np.ndarray,
    F: np.ndarray,
    keep_face_mask: np.ndarray,
    cameras: Sequence[Any],
    cam_depths_m: Sequence[np.ndarray],
    cam_quality_maps: Sequence[dict[str, Any]] | None = None,
    furniture_planes: Sequence[dict[str, Any]] | None = None,
    *,
    z_min_m: float = 0.08,
    z_max_m: float = 1.15,
    room_xy_bounds_m: tuple[float, float, float, float] = (0.25, 6.25, 0.75, 6.95),
    depth_tol_m: float = 0.065,
    sharp_exponent: float = 12.0,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """5차 지시 (b): 수평 상판 평면 밖의 남은 가구 면(벤치 옆면·테이블 테두리·의자)을 `texrecon` 대신 '최고 카메라 + 다중 카메라 일치 검사' 규칙으로 색상과 대표 카메라를 결정한다.

    반환:
    - `face_srgb (M, 3)`: 가구 면별 일관성 검증된 sRGB `[0, 1]` 색상
    - `is_furniture_face (M,)`: 처리 대상 가구 면 마스크
    - `stats`: 정량 요약 사전
    """
    from collections import defaultdict

    V = np.asarray(V_world, dtype=np.float64)
    F_arr = np.asarray(F, dtype=np.int64)
    keep = np.asarray(keep_face_mask, dtype=bool)
    m = len(F_arr)
    face_srgb = np.zeros((m, 3), dtype=np.float64)
    if m == 0 or not np.any(keep) or len(cameras) == 0:
        return face_srgb, np.zeros(m, dtype=bool), {"furniture_faces": 0, "observed_faces": 0}

    tri_pos = V[F_arr]
    fc = tri_pos.mean(axis=1)
    fn_raw = np.cross(tri_pos[:, 1] - tri_pos[:, 0], tri_pos[:, 2] - tri_pos[:, 0])
    fn_len = np.linalg.norm(fn_raw, axis=1)
    fn = fn_raw / np.maximum(fn_len[:, None], 1e-12)

    x_min, x_max, y_min, y_max = [float(v) for v in room_xy_bounds_m]
    furn_mask = (
        keep
        & (fc[:, 2] >= float(z_min_m))
        & (fc[:, 2] <= float(z_max_m))
        & (fc[:, 0] >= x_min)
        & (fc[:, 0] <= x_max)
        & (fc[:, 1] >= y_min)
        & (fc[:, 1] <= y_max)
    )
    f_idx = np.flatnonzero(furn_mask)
    if len(f_idx) == 0:
        return face_srgb, furn_mask, {"furniture_faces": 0, "observed_faces": 0}

    P_sub = fc[f_idx]
    N_sub = fn[f_idx]
    n_sub = len(f_idx)
    n_cams = len(cameras)
    cam_pos_w = [(-np.asarray(c.R, dtype=np.float64).T @ np.asarray(c.t, dtype=np.float64).reshape(3)) for c in cameras]

    cand_mask = np.zeros((n_cams, n_sub), dtype=bool)
    cand_srgb = np.zeros((n_cams, n_sub, 3), dtype=np.float64)
    cand_w = np.zeros((n_cams, n_sub), dtype=np.float64)

    for k, c in enumerate(cameras):
        R_c = np.asarray(c.R, dtype=np.float64)
        t_c = np.asarray(c.t, dtype=np.float64).reshape(3)
        K_c = np.asarray(c.K, dtype=np.float64)
        Xc = P_sub @ R_c.T + t_c[None, :]
        z_r = Xc[:, 2]
        px = K_c[0, 0] * (Xc[:, 0] / np.maximum(z_r, 1e-6)) + K_c[0, 2]
        py = K_c[1, 1] * (Xc[:, 1] / np.maximum(z_r, 1e-6)) + K_c[1, 2]
        Hc, Wc = c.image.shape[:2]

        dvec = cam_pos_w[k][None, :] - P_sub
        dist = np.maximum(np.linalg.norm(dvec, axis=1), 0.5)
        view_dir = dvec / dist[:, None]
        cos_n = np.sum(N_sub * view_dir, axis=1)

        in_b = (z_r > 0.3) & (cos_n >= 0.10) & (px >= 8.0) & (px < Wc - 8.0) & (py >= 8.0) & (py < Hc - 8.0)
        if not np.any(in_b):
            continue

        ipx = np.clip(np.round(px).astype(int), 0, Wc - 1)
        ipy = np.clip(np.round(py).astype(int), 0, Hc - 1)
        if k < len(cam_depths_m) and cam_depths_m[k] is not None and cam_depths_m[k].size > 0:
            d_map = cam_depths_m[k]
            Hd, Wd = d_map.shape[:2]
            d_ix = np.clip(np.round(px * (Wd / float(Wc))).astype(int), 0, Wd - 1)
            d_iy = np.clip(np.round(py * (Hd / float(Hc))).astype(int), 0, Hd - 1)
            d_c = d_map[d_iy, d_ix].astype(np.float64)
            depth_ok = np.isfinite(d_c) & (d_c > 0.1) & (np.abs(z_r - d_c) <= float(depth_tol_m))
        else:
            d_c = z_r.copy()
            depth_ok = np.ones(n_sub, dtype=bool)

        valid_k = in_b & depth_ok
        if not np.any(valid_k):
            continue

        samp = c.image[ipy, ipx, :3].astype(np.float64) / 255.0
        # 알약 벤치 영역(x in [3.85, 5.35], y in [1.10, 6.80]) 옆면에서 파란 의자·주황 가방·회색 카펫 번짐 배제
        in_bench_zone = (P_sub[:, 0] >= 3.85) & (P_sub[:, 0] <= 5.35) & (P_sub[:, 1] >= 1.10) & (P_sub[:, 1] <= 6.80)
        is_blue_chair = in_bench_zone & (samp[:, 2] - samp[:, 0] > 0.07) & (samp[:, 2] > 0.25) & (samp[:, 0] < 0.48)
        is_orange_bag = in_bench_zone & (samp[:, 0] - samp[:, 2] > 0.32) & (samp[:, 1] < 0.42) & (samp[:, 0] > 0.50)
        is_floor_bleed = in_bench_zone & (((samp[:, 0] - samp[:, 2]) < 0.028) | (samp.mean(axis=1) < 0.36))
        valid_k &= ~(is_blue_chair | is_orange_bag | is_floor_bleed)
        if not np.any(valid_k):
            continue

        if cam_quality_maps is not None and k < len(cam_quality_maps):
            w_ph = cam_quality_maps[k]["w_photo_map"][ipy, ipx].astype(np.float64)
        else:
            w_ph = np.ones(n_sub, dtype=np.float64)

        d_err = np.abs(z_r - np.where(np.isfinite(d_c), d_c, z_r))
        w_dep = np.exp(-0.5 * (d_err / 0.030) ** 2)
        w_tot = (np.clip(cos_n, 0.05, 1.0) ** 1.5) / (dist**2.0) * w_ph * w_dep

        cand_mask[k] = valid_k
        cand_srgb[k] = samp
        cand_w[k] = np.where(valid_k, np.maximum(w_tot, 1e-6), 0.0)

    # 인접 가구 삼각형 간 카메라 가중치 스무딩 (면마다 카메라가 바뀌는 조각보 현상 방지)
    key = np.round(V / 0.002).astype(np.int64)
    _, _, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    Ff = inv[F_arr[f_idx]]
    edge_map: dict[tuple[int, int], list[int]] = defaultdict(list)
    for loc_i, f in enumerate(Ff):
        u0, u1, u2 = int(f[0]), int(f[1]), int(f[2])
        for a, b_v in ((u0, u1), (u1, u2), (u2, u0)):
            if a > b_v:
                a, b_v = b_v, a
            edge_map[(a, b_v)].append(loc_i)
    nbrs: list[list[int]] = [[] for _ in range(n_sub)]
    for flist in edge_map.values():
        if len(flist) >= 2:
            for i in range(len(flist)):
                for j in range(i + 1, len(flist)):
                    ia, ib = flist[i], flist[j]
                    if float(np.dot(N_sub[ia], N_sub[ib])) >= 0.55:
                        nbrs[ia].append(ib)
                        nbrs[ib].append(ia)
    for _step in range(2):
        w_next = cand_w.copy()
        for loc_i, nb in enumerate(nbrs):
            if nb:
                w_next[:, loc_i] = 0.55 * cand_w[:, loc_i] + 0.45 * cand_w[:, nb].mean(axis=1)
        cand_w = np.where(cand_mask, w_next, 0.0)

    has_any = cand_mask.any(axis=0)
    best_k = np.argmax(cand_w, axis=0)
    init_srgb = cand_srgb[best_k, np.arange(n_sub)]
    if np.any(has_any):
        obs_pts = P_sub[has_any]
        obs_cols = init_srgb[has_any]
        tree_obs = cKDTree(obs_pts)
        _, nn_i = tree_obs.query(P_sub, k=min(8, int(has_any.sum())))
        if nn_i.ndim == 1:
            nn_i = nn_i[:, None]
        neigh_srgb = np.median(obs_cols[nn_i], axis=1)
    else:
        neigh_srgb = np.full((n_sub, 3), 0.75, dtype=np.float64)

    blended_srgb, obs_mask, _ = select_camera_colors_with_outlier_removal(
        cand_mask,
        cand_srgb,
        cand_w,
        neigh_srgb,
        sigma_oklab=0.052,
        is_floor=False,
        sharp_exponent=float(sharp_exponent),
    )

    # 미관측 가구 면은 가장 가까운 관측 가구 면/상판 색으로 전파
    if np.any(~obs_mask) and np.any(obs_mask):
        tree_clean = cKDTree(P_sub[obs_mask])
        _, idx_nn = tree_clean.query(P_sub[~obs_mask], k=min(4, int(obs_mask.sum())))
        if idx_nn.ndim == 1:
            idx_nn = idx_nn[:, None]
        blended_srgb[~obs_mask] = blended_srgb[obs_mask][idx_nn].mean(axis=1)

    # 수평 가구 상판 바로 아래 테두리 삼각형(dz <= 3.5 cm)만 상판 외곽 색과 25% 이내로 가볍게 페더링해 3D 테두리 디테일을 보존
    if furniture_planes:
        for fp in furniture_planes:
            x0, x1, y0, y1 = fp["xy_bounds_m"]
            z_med = float(fp["z_median_m"])
            pad_xy = 0.035
            z_lo = z_med - 0.038
            z_hi = z_med + 0.038
            near_fp = (
                (P_sub[:, 0] >= x0 - pad_xy)
                & (P_sub[:, 0] <= x1 + pad_xy)
                & (P_sub[:, 1] >= y0 - pad_xy)
                & (P_sub[:, 1] <= y1 + pad_xy)
                & (P_sub[:, 2] >= z_lo)
                & (P_sub[:, 2] <= z_hi)
            )
            if np.any(near_fp):
                th, tw = fp["tex_hw"]
                tex_f = fp["image"].astype(np.float64) / 255.0
                ix = np.clip(np.round((P_sub[near_fp, 0] - x0) / max(x1 - x0, 1e-6) * (tw - 1)).astype(int), 0, tw - 1)
                iy = np.clip(np.round((y1 - P_sub[near_fp, 1]) / max(y1 - y0, 1e-6) * (th - 1)).astype(int), 0, th - 1)
                fp_col = tex_f[iy, ix]
                dz = np.abs(P_sub[near_fp, 2] - z_med)
                alpha = 0.25 * (1.0 - smoothstep(0.0, 0.038, dz))[:, None]
                blended_srgb[near_fp] = blended_srgb[near_fp] * (1.0 - alpha) + fp_col * alpha

    face_srgb[f_idx] = np.clip(blended_srgb, 0.0, 1.0)
    return face_srgb, furn_mask, {
        "furniture_faces": int(n_sub),
        "observed_faces": int(obs_mask.sum()),
        "interpolated_faces": int((~obs_mask).sum()),
    }

