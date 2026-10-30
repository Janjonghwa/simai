"""품질 장치의 합성 반례와 비활성 기준선을 비교한다. 실제 인물·영상은 쓰지 않는다."""

from dataclasses import replace

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
torch = pytest.importorskip("torch")

from simai_ai.hmr.appearance import AppearanceAccumulator, AppearanceConfig
from simai_ai.hmr.appearance_quality import CameraColors, blur_weight, fit_weight
from simai_ai.hmr.appearance_stream import appearance_from_env
from simai_ai.hmr.multiview import COCO17_INDEX, project_ideal
from tests.hmr.test_appearance import camera, cube, render, pipeline_scene


def scene(**options):
    vertices, faces, uv, planes = cube()
    cfg = AppearanceConfig(resolution=96, freeze_frames=1, sample_interval_ms=1, **options)
    acc = AppearanceAccumulator(faces, uv, faces, np.zeros(len(faces), bool), config=cfg)
    acc.track(0, "CONFIRMED", 1000)
    return acc, vertices, planes


def test_oversized_mesh_background_bleeding_decreases():
    # 실제 실루엣보다 메시를 약 1 px 크게 둔다. bbox는 배경을 포함하며 분할 마스크는 없다.
    baseline, vertices, planes = scene(silhouette_enabled=False, blur_enabled=False, fit_enabled=False)
    improved, _, _ = scene(blur_enabled=False, fit_enabled=False)
    view = render(camera((3, 2, 1)), planes)
    image = np.full_like(view.image_bgr, [240, 10, 240])
    image[view.mask] = [80, 110, 70]
    view = replace(view, image_bgr=image, mask=None, bbox=[0, 0, 128, 128])
    for acc in (baseline, improved):
        assert acc.accumulate("w", vertices * 1.06, [view], 1000)
    def contamination(acc):
        seen = acc.weights > 0
        return np.average(np.abs(acc.colors[seen] - [70, 110, 80]).mean(axis=1), weights=acc.weights[seen])
    assert contamination(baseline) > 2
    assert contamination(improved) < contamination(baseline) * .2
    assert np.count_nonzero(improved.weights) > np.count_nonzero(baseline.weights) * .5


def test_camera_color_seams_converge_and_bounds_and_common_minimum(caplog):
    caplog.set_level("INFO")
    acc, vertices, planes = scene(fit_enabled=False)
    left = render(camera((2, 3, 1), "a"), planes)
    right = render(camera((-2, 3, 1), "b"), planes)
    expected = np.array([80., 100., 120.])
    left = replace(left, image_bgr=np.full_like(left.image_bgr, expected[::-1]))
    tint = np.array([1.25, .8, 1.1])
    right = replace(right, image_bgr=np.full_like(right.image_bgr, (expected * tint)[::-1]))
    for step in range(30):
        t = 1000 + step
        assert acc.accumulate("w", vertices, [replace(left, capture_time_ms=t), replace(right, capture_time_ms=t)], t)
    gain_a, gain_b = acc.camera_colors.gain(left.camera), acc.camera_colors.gain(right.camera)
    before = np.linalg.norm(expected - expected * tint)
    after = np.linalg.norm(expected * gain_a - expected * tint * gain_b)
    assert after < before * .02
    for gain in (gain_a, gain_b):
        assert np.all((gain >= .7) & (gain <= 1.4))
    # 관측 면 사이 atlas 색 차이도 감소한다. 첫 EMA 관측 오차가 누적되므로 이득보다 천천히 수렴한다.
    off, _, _ = scene(color_enabled=False, blur_enabled=False, fit_enabled=False)
    assert off.accumulate("w", vertices, [left, right], 1000)
    def seam(acc):
        a = acc.colors[(acc.owner // 2 == 0) & (acc.weights > 0)].mean(axis=0)
        b = acc.colors[(acc.owner // 2 == 3) & (acc.weights > 0)].mean(axis=0)
        return np.linalg.norm(a - b)
    assert seam(acc) < seam(off) * .2
    assert "외형 색 이득 camera=" in caplog.text
    state = CameraColors()
    cfg = acc.config
    indices = np.arange(cfg.color_min_common)
    samples = {left.camera: (indices, np.full((len(indices), 3), 200.)),
               right.camera: (indices, np.full((len(indices), 3), 20.))}
    for _ in range(100):
        state.estimate(samples, cfg)
    np.testing.assert_allclose(state.gain(right.camera), 1.4)
    unchanged = state.gain(right.camera).copy()
    state.estimate({k: (i[:10], c[:10]) for k, (i, c) in samples.items()}, cfg)
    np.testing.assert_array_equal(state.gain(right.camera), unchanged)


def test_blur_reduces_contribution_relative_to_camera_history():
    acc, vertices, planes = scene(color_enabled=False, fit_enabled=False)
    cam = camera((3, 2, 1))
    view = render(cam, planes)
    yy, xx = np.indices((128, 128))
    image = np.repeat((((xx // 2 + yy // 2) % 2) * 100 + 60)[..., None], 3, axis=2).astype(np.uint8)
    view = replace(view, image_bgr=image)
    for step in range(3):
        t = 1000 + step
        acc.accumulate("w", vertices, [replace(view, capture_time_ms=t)], t)
    weight_before = acc.weights.copy()
    blurry = replace(view, image_bgr=cv2.GaussianBlur(image, (9, 9), 2), capture_time_ms=1003)
    assert acc.accumulate("w", vertices, [blurry], 1003)
    assert (acc.weights - weight_before).sum() < weight_before.sum() / 3 * .2
    off, _, _ = scene(color_enabled=False, fit_enabled=False, blur_enabled=False)
    assert off.accumulate("w", vertices, [blurry], 1003)
    assert (acc.weights - weight_before).sum() < off.weights.sum() * .2
    # 경계 대비나 무텍스처/표본 부족에서 NaN·0 나눗셈을 만들지 않는다.
    history = []
    assert blur_weight(np.zeros_like(image), np.ones((128, 128), bool), history, acc.config)[0] == 1
    assert blur_weight(image, np.zeros((128, 128), bool), history, acc.config)[0] == .1


def test_fit_rejects_bad_view_and_sparse_is_only_downweighted(caplog):
    caplog.set_level("INFO")
    acc, vertices, planes = scene(blur_enabled=False, color_enabled=False)
    cam = camera((3, 2, 1))
    view = render(cam, planes)
    joints = np.linspace([-.3, -.2, -.3], [.3, .2, .3], 17)
    uv = project_ideal(cam, joints)
    observations = np.column_stack([uv, np.ones(17)]).tolist()
    view = replace(view, keypoints_2d=observations)
    assert fit_weight(view, joints, acc.config) == (1., 0., 17)
    shifted = replace(view, keypoints_2d=np.column_stack([uv + [50, 0], np.ones(17)]).tolist())
    assert not acc.accumulate("w", vertices, [shifted], 1000, joints_world=joints)
    assert not acc.weights.any()
    assert "fit=0.000" in caplog.text
    moderate = replace(view, keypoints_2d=np.column_stack([uv + [24, 0], np.ones(17)]).tolist())
    assert fit_weight(moderate, joints, acc.config)[0] == pytest.approx(.5)
    sparse = replace(shifted, keypoints_2d=shifted.keypoints_2d[:3], capture_time_ms=1001)
    assert fit_weight(sparse, joints, acc.config)[0] == .5
    assert acc.accumulate("w", vertices, [sparse], 1001, joints_world=joints)
    assert fit_weight(view, None, acc.config)[0] == .5
    disabled, _, _ = scene(blur_enabled=False, color_enabled=False, fit_enabled=False)
    assert disabled.accumulate("w", vertices, [shifted], 1000, joints_world=joints)
    assert disabled.weights.sum() == pytest.approx(2 * acc.weights.sum())


def test_quality_state_shared_between_people_and_abort_is_transactional(rig):
    pipeline, state, _, _, args, _ = pipeline_scene(rig)
    pipeline.register("w-2", state, 1000)
    first, second = pipeline.people["w-1"], pipeline.people["w-2"]
    assert first.camera_colors is second.camera_colors is pipeline.camera_colors
    pipeline.retain({"w-2"})
    assert second.camera_colors is pipeline.camera_colors
    acc, vertices, planes = scene(fit_enabled=False)
    views = [render(camera((2, 3, 1), "a"), planes), render(camera((-2, 3, 1), "b"), planes)]
    views[1] = replace(views[1], image_bgr=(views[1].image_bgr * .8).astype(np.uint8))
    # estimate 뒤 admission이 막혀도 공유 이득·선명도·누적 모두 그대로 유지한다.
    # 마지막 gate 호출 수를 정상 회차에서 먼저 센다.
    checks = [0]
    def count():
        checks[0] += 1
        return True
    assert acc.accumulate("w", vertices, views, 1000, admit=count)
    target = checks[0]
    old_colors, old_weights = acc.colors.copy(), acc.weights.copy()
    old_gains = {k: v.copy() for k, v in acc.camera_colors.gains.items()}
    old_sharpness = {k: list(v) for k, v in acc.sharpness.items()}
    checks[0] = 0
    def block_last():
        checks[0] += 1
        return checks[0] < target
    assert not acc.accumulate("w", vertices, [replace(v, capture_time_ms=1001) for v in views], 1001, admit=block_last)
    np.testing.assert_array_equal(acc.colors, old_colors)
    np.testing.assert_array_equal(acc.weights, old_weights)
    assert acc.sharpness == old_sharpness
    for key, value in old_gains.items():
        np.testing.assert_array_equal(acc.camera_colors.gain(key), value)
    assert old_gains and old_sharpness


def test_pipeline_uses_tier0_coco17_and_resets_camera_state_on_calibration(rig):
    pipeline, _, messages, _, args, _ = pipeline_scene(rig)
    person = args[0].people[0]
    person.world_keypoints = np.zeros((70, 3))
    cam = args[4].cameras["cam1"]
    uv = project_ideal(cam, person.world_keypoints[COCO17_INDEX])
    obs = args[1]["tracks"][0]["per_camera"][0]
    obs["keypoints_2d"] = np.column_stack([uv + [50, 0], np.ones(17)]).tolist()
    pipeline.update(*args)
    assert not pipeline.people["w-1"].weights.any() and not messages
    obs["keypoints_2d"] = np.column_stack([uv, np.ones(17)]).tolist()
    args[1]["t"] = args[2]["frames"]["cam1"]["capture_time_ms"] = 1001
    pipeline.update(*args)
    assert pipeline.people["w-1"].weights.any() and len(messages) == 1
    pipeline.camera_colors.gains[cam] = np.array([.8, 1.2, 1.1])
    pipeline.people["w-1"].sharpness[cam] = [123.]
    args[0].calibration_version = "new-calibration"
    # 누적 간격 안에서 호출해도 보정 변경 시 과거 이득·선명도를 폐기한다.
    pipeline.update(*args)
    assert not pipeline.camera_colors.gains and not pipeline.people["w-1"].sharpness


def test_color_estimation_is_robust_and_keeps_reference_gauge():
    cfg = AppearanceConfig()
    a, b = camera((2, 3, 1), "a"), camera((-2, 3, 1), "b")
    state = CameraColors()
    ids = np.arange(100)
    left, right = np.full((100, 3), 100.), np.full((100, 3), 125.)
    right[:20] = 25.  # 20%의 잘못 투영된 색은 중앙값을 움직이지 않는다.
    for _ in range(40):
        state.estimate({a: (ids, left), b: (ids, right)}, cfg)
    np.testing.assert_allclose(state.gain(b), .8, atol=1e-4)
    # b가 최다 관측 기준으로 바뀌어도 이미 보정된 색의 기준은 연속이다.
    prior = state.gain(b).copy()
    state.estimate({a: (ids[:-1], left[:-1]), b: (ids, right)}, cfg)
    np.testing.assert_allclose(state.gain(a), 1., atol=1e-4)
    np.testing.assert_array_equal(state.gain(b), prior)
    for _ in range(100):
        state.estimate({a: (ids, np.full((100, 3), 20.)), b: (ids, np.full((100, 3), 200.))}, cfg)
    np.testing.assert_allclose(state.gain(b), .7)


@pytest.mark.parametrize("options", [dict(silhouette_erode_px=-1), dict(silhouette_fade_px=0),
    dict(color_ema=0), dict(color_min_common=0), dict(color_gain_min=1.1), dict(blur_history=0),
    dict(blur_floor=0), dict(fit_min_joints=18), dict(fit_reject_px=8), dict(fit_full_px=float("nan"))])
def test_quality_config_rejects_invalid_values(options):
    with pytest.raises(ValueError):
        AppearanceConfig(**options)


@pytest.mark.parametrize("name,field", [("SILHOUETTE", "silhouette_enabled"), ("COLOR", "color_enabled"),
                                       ("BLUR", "blur_enabled"), ("FIT", "fit_enabled")])
def test_quality_env_switches_are_independent(name, field):
    env = {"SIMAI_AVATAR_APPEARANCE": "1", f"SIMAI_AVATAR_TEXTURE_{name}": "0"}
    pipeline = appearance_from_env(env, cuda_available=False)
    for option in ("silhouette_enabled", "color_enabled", "blur_enabled", "fit_enabled"):
        assert getattr(pipeline.config, option) == (option != field)
    env[f"SIMAI_AVATAR_TEXTURE_{name}"] = "yes"
    with pytest.raises(ValueError):
        appearance_from_env(env, cuda_available=False)
