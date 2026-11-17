"""best 텍셀 합치기(S15P31A101-789)를 합성 상자로 확인한다(CPU). 실제 인물·영상은 쓰지 않는다."""

from dataclasses import replace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
torch = pytest.importorskip("torch")

from simai_ai.hmr.appearance import AppearanceAccumulator, AppearanceConfig, AppearanceView, fuse_best
from simai_ai.hmr.appearance_stream import appearance_from_env
from simai_ai.hmr.multiview import project_ideal
from tests.hmr.test_appearance import camera, cube

CENTERS = [(3, 2, 1), (-3, 2, 1), (-3, -2, 1), (3, -2, 1)]
JOINTS = np.linspace([-.3, -.2, -.3], [.3, .2, .3], 17)   # 상자 안 17점(맞춤 가중용 합성 COCO17)
LOCAL = np.tile([[0, 0], [1, 0], [1, 1], [0, 1]], (6, 1))


def checker(face, u, v):
    """면마다 4×4 체크무늬. 경계가 많아 어긋남·흐림이 섞이면 바로 번진다."""
    on = ((np.floor(u * 4) + np.floor(v * 4)) % 2).astype(bool)
    face = np.asarray(face)[..., None]
    dark = np.array([40., 60., 50.]) + face * 6
    light = np.array([210., 190., 160.]) - face * 6
    return np.where(on[..., None], light, dark)


def render(cam, planes, pattern=checker, t=1000):
    yy, xx = np.indices((128, 128))
    norm, ok = cam.normalize_with_mask(np.column_stack([xx.ravel(), yy.ravel()]))
    rays = np.column_stack([norm, np.ones(len(norm))]) @ cam.R
    nearest, color = np.full(len(norm), np.inf), np.zeros((len(norm), 3))
    for i, (n, a, b) in enumerate(planes):
        denom = rays @ n
        with np.errstate(divide="ignore", invalid="ignore"):
            distance = (.5 - cam.center @ n) / denom
        point = cam.center + rays * distance[:, None]
        u, v = point @ a + .5, point @ b + .5
        keep = ok & (denom < 0) & (distance > 0) & (distance < nearest) & (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1)
        color[keep] = pattern(i, u[keep], v[keep])
        nearest[keep] = distance[keep]
    image = np.rint(color).astype(np.uint8).reshape(128, 128, 3)[..., ::-1].copy()
    keypoints = np.column_stack([project_ideal(cam, JOINTS), np.ones(17)])
    return AppearanceView(cam, image, t, mask=np.isfinite(nearest).reshape(128, 128), keypoints_2d=keypoints.tolist())


def degrade(view, dx, dy, sigma, t):
    """메시와 영상이 (dx, dy) px 어긋난 관측. 영상·마스크·2D 관절이 함께 움직인다(Tier 0 관절은 영상을 따른다)."""
    image = np.roll(view.image_bgr, (dy, dx), axis=(0, 1))
    mask = np.roll(view.mask, (dy, dx), axis=(0, 1))
    if dy:
        (image[:dy] if dy > 0 else image[dy:])[:] = 0
        (mask[:dy] if dy > 0 else mask[dy:])[:] = False
    if dx:
        (image[:, :dx] if dx > 0 else image[:, dx:])[:] = 0
        (mask[:, :dx] if dx > 0 else mask[:, dx:])[:] = False
    if sigma:
        image = cv2.GaussianBlur(image, (0, 0), sigma)
    keypoints = np.asarray(view.keypoints_2d, float)
    keypoints[:, :2] += [dx, dy]
    return replace(view, image_bgr=image, mask=mask, keypoints_2d=keypoints.tolist(), capture_time_ms=t)


def scene(fusion="best", resolution=96, **options):
    vertices, faces, uv, planes = cube()
    cfg = AppearanceConfig(resolution=resolution, freeze_frames=1, sample_interval_ms=1, seam_width=0,
                           fusion=fusion, **options)
    acc = AppearanceAccumulator(faces, uv, faces, np.zeros(len(faces), bool), config=cfg)
    acc.track(0, "CONFIRMED", 1000)
    return acc, vertices, planes


def truth(acc, pattern=checker):
    seen = acc.weights > 0
    fi = acc.owner[seen]
    xy = np.einsum("ni,nij->nj", acc.bary[seen], LOCAL[acc.faces[fi]])
    return seen, pattern(fi // 2, xy[:, 0], xy[:, 1])


def error(acc, pattern=checker):
    seen, expected = truth(acc, pattern)
    return float(np.mean(np.abs(acc.colors[seen] - expected)))


def sharpness(acc):
    """UV 영상의 관측 텍셀 기울기 크기 평균(같은 면 안 이웃만). 번질수록 작다."""
    n = acc.config.resolution
    rgb = acc.colors.reshape(n, n, 3)
    seen = (acc.weights > 0).reshape(n, n)
    face = (acc.owner // 2).reshape(n, n)
    gx = np.abs(np.diff(rgb, axis=1)).sum(axis=2)[(seen[:, 1:] & seen[:, :-1]) & (face[:, 1:] == face[:, :-1])]
    gy = np.abs(np.diff(rgb, axis=0)).sum(axis=2)[(seen[1:] & seen[:-1]) & (face[1:] == face[:-1])]
    return float(np.mean(np.r_[gx, gy]))


def _misregistered_run(seed, steps=24):
    """카메라 4대 × steps 회차. 회차마다 카메라별로 메시와 영상이 0~12 px 어긋나고(2D 관절도 같이) 4번에 1번꼴로 흐리다."""
    rng = np.random.default_rng(seed)
    accs = {name: scene(name)[0] for name in ("best", "mean")}
    _, vertices, planes = scene()
    views = [render(camera(c, f"cam{i}"), planes) for i, c in enumerate(CENTERS)]
    for step in range(steps):
        t = 1000 + step
        batch = []
        for view in views:
            shift = rng.choice([0, 1, 2, 4, 6, 9, 12], p=[.1, .15, .15, .2, .15, .15, .1])
            angle = rng.uniform(0, 2 * np.pi)
            dx, dy = int(round(shift * np.cos(angle))), int(round(shift * np.sin(angle)))
            sigma = 2.5 if rng.random() < .25 else 0
            batch.append(degrade(view, dx, dy, sigma, t))
        for acc in accs.values():
            acc.accumulate("w", vertices, batch, t, joints_world=JOINTS)
    return accs


def test_best_keeps_edges_sharper_than_mean_under_misregistration_and_blur():
    """같은 입력에서 best 가 mean 보다 정답(체크무늬) 오차가 작고 UV 기울기가 선명하다. 시드 3개 평균으로 본다.

    mean 은 첫 관측이 어긋나면 체크 경계 텍셀이 이상치 거절로 굳어 시드마다 오차가 크게 흔들린다(설계 문서 합성 표).
    """
    errors, sharp, coverage = {"best": [], "mean": []}, {"best": [], "mean": []}, {"best": [], "mean": []}
    for seed in (0, 1, 2):
        for name, acc in _misregistered_run(seed).items():
            errors[name].append(error(acc))
            sharp[name].append(sharpness(acc))
            coverage[name].append(np.count_nonzero(acc.weights))
    # 관측 영역은 같다(best 가 관측을 버려 덮는 면적을 줄이지 않는다)
    assert coverage["best"] == coverage["mean"]
    e_best, e_mean = np.mean(errors["best"]), np.mean(errors["mean"])
    s_best, s_mean = np.mean(sharp["best"]), np.mean(sharp["mean"])
    assert e_best < .75 * e_mean, (errors, sharp)
    assert s_best > 1.1 * s_mean, (errors, sharp)


def test_wrong_sharp_first_observation_is_replaced_not_locked():
    """처음 들어온 틀린 선명 관측(색 차 > outlier_rgb)이 뒤의 일관된 바른 관측(조금 낮은 품질)에 밀려난다.

    mean 은 기존대로 고착된다(바른 관측을 이상치로 계속 버림) — best 로 바꾼 이유다.
    """
    flat = lambda face, u, v: np.broadcast_to(np.array([60., 140., 90.]), np.shape(u) + (3,))
    wrong = lambda face, u, v: np.broadcast_to(np.array([230., 30., 220.]), np.shape(u) + (3,))
    results = {}
    for fusion in ("best", "mean"):
        acc, vertices, planes = scene(fusion, resolution=48)
        cam = camera(CENTERS[0], "cam0")
        assert acc.accumulate("w", vertices, [render(cam, planes, wrong, 1000)], 1000, joints_world=JOINTS)
        right = render(cam, planes, flat)
        # 바른 관측은 2D 관절이 16 px 어긋나 맞춤 가중 0.75(세기 0.32배)
        keypoints = np.asarray(right.keypoints_2d)
        keypoints[:, 0] += 16
        right = replace(right, keypoints_2d=keypoints.tolist())
        for step in range(1, 8):
            acc.accumulate("w", vertices, [replace(right, capture_time_ms=1000 + step)], 1000 + step,
                           joints_world=JOINTS)
        results[fusion] = error(acc, flat)
    assert results["best"] < 1.
    assert results["mean"] > 100   # 기존 동작: 고착


def test_single_wrong_observation_after_good_ones_does_not_take_over_and_sporadic_ones_fade():
    flat = lambda face, u, v: np.broadcast_to(np.array([60., 140., 90.]), np.shape(u) + (3,))
    wrong = lambda face, u, v: np.broadcast_to(np.array([230., 30., 220.]), np.shape(u) + (3,))
    acc, vertices, planes = scene("best", resolution=48)
    cam = camera(CENTERS[0], "cam0")
    good, bad = render(cam, planes, flat), render(cam, planes, wrong)
    t = 1000
    for _ in range(3):
        acc.accumulate("w", vertices, [replace(good, capture_time_ms=t)], t, joints_world=JOINTS)
        t += 1
    # 좋은 관측 사이사이 틀린 관측 한 번씩: 거절 압력이 좋은 관측마다 줄어 쌓이지 않는다
    for _ in range(10):
        assert not acc.accumulate("w", vertices, [replace(bad, capture_time_ms=t)], t, joints_world=JOINTS)
        acc.accumulate("w", vertices, [replace(good, capture_time_ms=t + 1)], t + 1, joints_world=JOINTS)
        t += 2
    assert error(acc, flat) < 1e-3
    seen = acc.weights > 0
    assert (acc.rejects[seen] < acc.weights[seen]).all()


def test_mean_switch_keeps_old_formula_and_best_sharpens_quality():
    """같은 텍셀에 품질 1.0(색 X)과 0.5(색 X+40) 관측: mean 은 기존 식 w 가중, best 는 w^4 가중."""
    base = lambda face, u, v: np.broadcast_to(np.array([80., 100., 120.]), np.shape(u) + (3,))
    plus = lambda face, u, v: np.broadcast_to(np.array([120., 140., 160.]), np.shape(u) + (3,))
    for fusion, expected in (("mean", 40 * .5 / 1.5), ("best", 40 * (1 / 16) / (1 + 1 / 16))):
        acc, vertices, planes = scene(fusion, resolution=48, blur_enabled=False, silhouette_enabled=False,
                                      color_enabled=False)
        cam = camera(CENTERS[0], "cam0")
        first = render(cam, planes, base)
        assert acc.accumulate("w", vertices, [first], 1000, joints_world=JOINTS)
        w_first = acc.weights.copy()
        second = replace(render(cam, planes, plus), keypoints_2d=first.keypoints_2d[:3], capture_time_ms=1001)
        assert acc.accumulate("w", vertices, [second], 1001, joints_world=JOINTS)   # 관절 3개 → 맞춤 0.5
        seen = w_first > 0
        np.testing.assert_allclose(acc.colors[seen] - [80., 100., 120.], expected, atol=1e-3)
        if fusion == "mean":
            np.testing.assert_allclose(acc.weights[seen], 1.5 * w_first[seen], rtol=1e-5)
            assert not acc.peaks.any() and not acc.rejects.any()   # mean 은 best 상태를 건드리지 않는다
        else:
            np.testing.assert_allclose(acc.peaks[seen], w_first[seen], rtol=1e-6)


def test_env_switch():
    assert appearance_from_env({"SIMAI_AVATAR_APPEARANCE": "1"}, cuda_available=False).config.fusion == "best"
    cfg = appearance_from_env({"SIMAI_AVATAR_APPEARANCE": "1", "SIMAI_AVATAR_TEXTURE_FUSION": "mean"},
                              cuda_available=False).config
    assert cfg.fusion == "mean"
    with pytest.raises(ValueError):
        appearance_from_env({"SIMAI_AVATAR_APPEARANCE": "1", "SIMAI_AVATAR_TEXTURE_FUSION": "max"},
                            cuda_available=False)
    for bad in ({"fusion": "max"}, {"fusion_power": .5}, {"fusion_keep": 0}, {"fusion_reject_decay": 1.}):
        with pytest.raises(ValueError):
            AppearanceConfig(**bad)


# ---- -743 이어받기·병합과 best 상태 ----

def _two(fusion="best"):
    acc, vertices, planes = scene(fusion, resolution=48)
    other, _, _ = scene(fusion, resolution=48)
    return acc, other, vertices, planes


def test_best_merge_agreeing_texels_mix_capped_mass_and_keep_higher_peak():
    acc, other, vertices, planes = _two()
    t = 1000
    for _ in range(6):   # 이쪽은 같은 카메라로 여러 번: 질량이 k·최고 세기로 묶인다
        acc.accumulate("w-1", vertices, [render(camera(CENTERS[0], "cam0"), planes, t=t)], t, joints_world=JOINTS)
        t += 1
    assert other.accumulate("w-2", vertices, [render(camera(CENTERS[1], "cam1"), planes)], 1000, joints_world=JOINTS)
    mine, theirs = acc.weights > 0, other.weights > 0
    both, only_theirs = mine & theirs, theirs & ~mine
    assert both.any() and only_theirs.any()
    keep = acc.config.fusion_keep
    assert (acc.weights[mine] <= keep * acc.peaks[mine] * (1 + 1e-6) + acc.peaks[mine]).all()
    held_a = np.minimum(acc.weights[both], keep * acc.peaks[both])
    held_b = np.minimum(other.weights[both], keep * other.peaks[both])
    # 체크무늬 경계 텍셀은 두 카메라가 다른 칸 색으로 볼 수 있다: 색이 어긋나면 질량 큰 쪽을 통째로
    agree = np.abs(acc.colors[both] - other.colors[both]).max(axis=1) <= acc.config.outlier_rgb
    theirs_win = ~agree & (held_b > held_a)
    assert agree.any()
    mixed = (held_a[:, None] * acc.colors[both] + held_b[:, None] * other.colors[both]) / (held_a + held_b)[:, None]
    expected = np.where(agree[:, None], mixed, np.where(theirs_win[:, None], other.colors[both], acc.colors[both]))
    expected_weights = np.where(agree, held_a + held_b, np.where(theirs_win, held_b, held_a))
    peaks = np.where(agree, np.maximum(acc.peaks[both], other.peaks[both]),
                     np.where(theirs_win, other.peaks[both], acc.peaks[both]))
    before = other.colors.copy()
    assert acc.absorb(other.snapshot(), inherit=False)
    np.testing.assert_allclose(acc.colors[both], expected, rtol=1e-5)
    np.testing.assert_allclose(acc.weights[both], expected_weights, rtol=1e-6)
    np.testing.assert_array_equal(acc.peaks[both], peaks)
    np.testing.assert_array_equal(acc.colors[only_theirs], other.colors[only_theirs])
    np.testing.assert_array_equal(acc.peaks[only_theirs], other.peaks[only_theirs])
    assert not acc.inherited.any() and acc.carry_publish and acc.dirty
    np.testing.assert_array_equal(other.colors, before)


def test_best_merge_disagreeing_texels_take_stronger_side_whole_instead_of_blending():
    wrong = lambda face, u, v: np.broadcast_to(np.array([230., 30., 220.]), np.shape(u) + (3,))
    acc, other, vertices, planes = _two()
    cam = camera(CENTERS[0], "cam0")
    assert acc.accumulate("w-1", vertices, [render(cam, planes)], 1000, joints_world=JOINTS)
    for t in (1000, 1001, 1002):   # 상대는 같은 품질 관측 3번: 질량이 커서 이긴다
        other.accumulate("w-2", vertices, [render(cam, planes, wrong, t)], t, joints_world=JOINTS)
    both = (acc.weights > 0) & (other.weights > 0)
    assert both.any()
    assert acc.absorb(other.snapshot(), inherit=False)
    np.testing.assert_array_equal(acc.colors[both], other.colors[both])   # 섞지 않고 통째로
    np.testing.assert_array_equal(acc.weights[both], other.weights[both])
    assert not acc.rejects[both].any()
    # 반대로 약한 쪽을 합치면 이쪽이 그대로
    weak, _, _ = scene("best", resolution=48)
    weak.accumulate("w-3", vertices, [render(cam, planes)], 1000, joints_world=JOINTS)   # 이제 이쪽(틀린 색)과 어긋나고 약하다
    kept = acc.colors.copy()
    acc.absorb(weak.snapshot(), inherit=False)
    np.testing.assert_array_equal(acc.colors[both], kept[both])


def test_best_inherit_fills_unobserved_and_first_own_observation_overwrites_with_fresh_state():
    wrong = lambda face, u, v: np.broadcast_to(np.array([250., 250., 250.]), np.shape(u) + (3,))
    acc, donor, vertices, planes = _two()
    assert acc.accumulate("w-7", vertices, [render(camera(CENTERS[0], "cam0"), planes)], 1000, joints_world=JOINTS)
    own = acc.weights > 0
    own_state = acc.colors.copy(), acc.peaks.copy()
    side = camera(CENTERS[1], "cam1")
    for t in (1000, 1001, 1002, 1003):   # 주는 쪽 질량이 커도 이어받은 텍셀은 첫 자기 관측이 덮는다
        donor.accumulate("w-1", vertices, [render(side, planes, wrong, t)], t, joints_world=JOINTS)
    donor_seen = donor.weights > 0
    assert acc.absorb(donor.snapshot(), inherit=True)
    np.testing.assert_array_equal(acc.colors[own], own_state[0][own])
    np.testing.assert_array_equal(acc.peaks[own], own_state[1][own])
    assert np.array_equal(acc.inherited, donor_seen & ~own) and acc.inherited.any()
    assert acc.accumulate("w-7", vertices, [render(side, planes, t=1010)], 1010, joints_world=JOINTS)
    reference, _, _ = scene("best", resolution=48)
    reference.accumulate("w-x", vertices, [render(side, planes, t=1010)], 1010, joints_world=JOINTS)
    overwritten = (reference.weights > 0) & donor_seen & ~own
    assert overwritten.any() and not acc.inherited[overwritten].any()
    np.testing.assert_allclose(acc.colors[overwritten], reference.colors[overwritten], atol=1e-3)
    np.testing.assert_allclose(acc.weights[overwritten], reference.weights[overwritten], rtol=1e-6)
    np.testing.assert_allclose(acc.peaks[overwritten], reference.peaks[overwritten], rtol=1e-6)


def test_snapshot_without_peaks_and_revision_reset_and_adopt():
    acc, other, vertices, planes = _two()
    assert other.accumulate("w-2", vertices, [render(camera(CENTERS[1], "cam1"), planes)], 1000, joints_world=JOINTS)
    legacy = replace(other.snapshot(), peaks=None)   # 최고 세기를 모르는 사본: 질량을 최고 세기로 본다
    assert acc.absorb(legacy, inherit=False)
    seen = acc.weights > 0
    np.testing.assert_array_equal(acc.peaks[seen], acc.weights[seen])
    peaks = acc.peaks
    fresh, _, _ = scene("best", resolution=48)
    assert fresh.adopt(acc) and fresh.peaks is peaks
    acc.track(1, "CONFIRMED", 2000)   # revision 변경: best 상태도 새 배열로 비운다(사본은 그대로)
    assert not acc.peaks.any() and not acc.rejects.any() and peaks.any()


def test_gate_abort_keeps_best_state():
    acc, vertices, planes = scene("best", resolution=48)
    view = render(camera(CENTERS[0], "cam0"), planes)
    assert acc.accumulate("w", vertices, [view], 1000, joints_world=JOINTS)
    state = acc.colors.copy(), acc.weights.copy(), acc.peaks.copy(), acc.rejects.copy()
    checks = iter([True, True, False])
    assert not acc.accumulate("w", vertices, [replace(view, capture_time_ms=1001)], 1001, joints_world=JOINTS,
                              admit=lambda: next(checks, False))
    for before, after in zip(state, (acc.colors, acc.weights, acc.peaks, acc.rejects)):
        np.testing.assert_array_equal(before, after)


# ---- 같은 식: torch 구현 = 텍셀별 스칼라 참조, CPU = CUDA ----

def _reference(C, W, P, R, I, idx, w, obs, cfg):
    C, W, P, R, I = C.copy(), W.copy(), P.copy(), R.copy(), I.copy()
    for j, i in enumerate(idx):
        s = float(np.float32(w[j]) ** np.float32(cfg.fusion_power))   # torch 와 같은 float32 거듭제곱(0 으로 내려감 포함)
        if s <= 0:
            continue
        if I[i] or W[i] <= 0:
            C[i], W[i], P[i], R[i] = obs[j], s, s, 0
        elif np.abs(C[i] - obs[j]).max() <= cfg.outlier_rgb:
            peak = max(P[i], s)
            held = min(W[i], cfg.fusion_keep * peak)
            C[i] = (held * C[i] + s * obs[j]) / (held + s)
            W[i], P[i], R[i] = held + s, peak, R[i] * cfg.fusion_reject_decay
        elif R[i] + s > min(W[i], cfg.fusion_keep * P[i]):
            C[i], W[i], P[i], R[i] = obs[j], s, s, 0
        else:
            R[i] += s
        I[i] = False
    return C, W, P, R, I


def _random_state(seed, n=4000):
    rng = np.random.default_rng(seed)
    C = rng.uniform(0, 255, (n, 3)).astype(np.float32)
    W = np.where(rng.random(n) < .3, 0, rng.uniform(0, 2, n)).astype(np.float32)
    P = np.where(W > 0, np.minimum(W, rng.uniform(.01, 1, n)), 0).astype(np.float32)
    R = np.where(W > 0, rng.uniform(0, 1, n) * W, 0).astype(np.float32)
    I = (rng.random(n) < .1) & (W > 0)
    idx = rng.choice(n, n // 2, replace=False)
    w = rng.uniform(0, 1, len(idx)).astype(np.float32)
    w[:5] = 1e-12   # 거듭제곱이 0 으로 내려가는 가중
    near = C[idx] + rng.uniform(-60, 60, (len(idx), 3))
    far = rng.uniform(0, 255, (len(idx), 3))
    obs = np.where(rng.random((len(idx), 1)) < .5, near, far).clip(0, 255).astype(np.float32)
    return C, W, P, R, I, idx, w, obs


def _run_torch(state, cfg, device):
    C, W, P, R, I, idx, w, obs = state
    t = lambda a: torch.as_tensor(a.copy(), device=device)
    tensors = [t(C), t(W), t(P), t(R), t(I)]
    counts = fuse_best(*tensors, t(idx), t(w), t(obs), cfg)
    return [x.cpu().numpy() for x in tensors], counts


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_fuse_best_torch_matches_scalar_reference(seed):
    cfg = AppearanceConfig()
    state = _random_state(seed)
    got, (accepted, rejected, takeover) = _run_torch(state, cfg, "cpu")
    want = _reference(*state, cfg)
    for a, b in zip(got[:4], want[:4]):
        np.testing.assert_allclose(a, b, rtol=2e-5, atol=1e-4)
    np.testing.assert_array_equal(got[4], want[4])
    assert accepted > 0 and rejected > 0 and takeover > 0
    assert accepted + rejected == len(state[5]) - 5


@pytest.mark.gpu
def test_fuse_best_cuda_matches_cpu():
    if not torch.cuda.is_available():
        pytest.skip("CUDA 없음")
    cfg = AppearanceConfig()
    state = _random_state(5)
    cpu, cpu_counts = _run_torch(state, cfg, "cpu")
    gpu, gpu_counts = _run_torch(state, cfg, "cuda")
    assert cpu_counts == gpu_counts
    for a, b in zip(cpu, gpu):
        np.testing.assert_allclose(a, b, rtol=1e-5, atol=1e-4)
