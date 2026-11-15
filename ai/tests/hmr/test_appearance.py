"""합성 상자·해석적 ray/평면 렌더러로 관측 픽셀 복원과 출처를 확인한다(CPU)."""

import copy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
torch = pytest.importorskip("torch")

from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.geometry.rig import RigCamera
from simai_ai.hmr.appearance import AppearanceAccumulator, AppearanceConfig, AppearanceView
from simai_ai.hmr.appearance_stream import (
    AppearancePipeline, appearance_from_env, face_faces_from_rig, posed_world_vertices,
)
from simai_ai.hmr.gltf_export import build_skinned_glb, read_glb, accessor_array
from simai_ai.hmr.rig import PersonRig
from simai_ai.hmr.appearance_report import comparison, save_result
from simai_ai.providers.contract import schema_errors


def camera(center, name="cam1", distortion=("NONE", ())):
    c = np.asarray(center, float)
    f = -c / np.linalg.norm(c)
    r = np.cross(f, [0., 0., 1.])
    r /= np.linalg.norm(r)
    R = np.stack([r, np.cross(f, r), f])
    K = [[120., 0., 64.], [0., 120., 64.], [0., 0., 1.]]
    return RigCamera(name, PinholeCamera(K, R.tolist(), (-R @ c).tolist()), (128, 128), *distortion)


def cube():
    vertices, faces, uv, planes = [], [], [], []
    for i, n in enumerate(np.r_[np.eye(3), -np.eye(3)]):
        a = np.array([0., 1., 0.]) if abs(n[0]) else np.array([1., 0., 0.])
        b = np.cross(n, a)
        corners = np.array([[-.5, -.5], [.5, -.5], [.5, .5], [-.5, .5]])
        vertices.extend(n * .5 + corners[:, :1] * a + corners[:, 1:] * b)
        faces.extend([[4 * i, 4 * i + 1, 4 * i + 2], [4 * i, 4 * i + 2, 4 * i + 3]])
        # 3×2 atlas. 각 면의 UV는 MHR 아래 원점이다.
        uv.extend((corners + .5 + [i % 3, i // 3]) / [3, 2])
        planes.append((n, a, b))
    return np.array(vertices), np.array(faces), np.array(uv), planes


def known_color(face, u, v):
    return np.stack([30 + face * 16 + u * 60, 40 + v * 100, 50 + face * 12 + u * 20], axis=-1)


def render(cam, planes):
    """학습/누적 래스터화와 독립인 광선-유한 평면 교차식. 실제 파일 입력 없음."""
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
        color[keep] = known_color(i, u[keep], v[keep])
        nearest[keep] = distance[keep]
    image = np.rint(color).astype(np.uint8).reshape(128, 128, 3)[..., ::-1].copy()
    return AppearanceView(cam, image, 1000, mask=np.isfinite(nearest).reshape(128, 128))


def accumulator(*, policy="OBSERVED", resolution=48, freeze=1, seam=0):
    vertices, faces, uv, planes = cube()
    cfg = AppearanceConfig(resolution=resolution, freeze_frames=freeze, seam_width=seam,
                           face_policy=policy, sample_interval_ms=1, publish_interval_ms=100)
    face = np.zeros(len(faces), bool)
    face[2:4] = True  # +Y를 합성 얼굴 영역으로 삼는다.
    acc = AppearanceAccumulator(faces, uv, faces, face, config=cfg)
    acc.track(0, "CONFIRMED", 1000)
    return acc, vertices, planes


def decode(asset, *, rgb=False):
    a = cv2.imdecode(np.frombuffer(asset.data, np.uint8), cv2.IMREAD_UNCHANGED)
    return a[..., ::-1] if rgb else a


@pytest.mark.parametrize("centers", [[(3, 2, 1), (-3, 2, 1)], [(3, 2, 1), (-3, 2, 1), (-3, -2, 1), (3, -2, 1)]])
def test_multiview_known_texture_error_and_unseen_surface(centers):
    acc, vertices, planes = accumulator()
    views = [render(camera(c, f"cam{i}"), planes) for i, c in enumerate(centers)]
    assert acc.accumulate("w-1", vertices, views, 1000)
    seen = acc.weights > 0
    face = acc.owner[seen] // 2
    bary = acc.bary[seen]
    local_corners = np.tile([[0, 0], [1, 0], [1, 1], [0, 1]], (6, 1))
    local = np.einsum("ni,nij->nj", bary, local_corners[acc.faces[acc.owner[seen]]])
    expected = known_color(face, local[:, 0], local[:, 1])
    assert np.mean(np.abs(acc.colors[seen] - expected)) < 2.
    assert np.any(seen & (acc.owner // 2 == 0))  # +X는 첫 카메라에서만 보인다.
    assert not np.any(seen & (acc.owner // 2 == 5))  # 바닥 면은 어느 카메라에도 안 보인다.
    result = acc.result()
    assert sum(result.ratios.values()) == pytest.approx(1)
    assert sum(result.face_ratios.values()) == pytest.approx(1)
    assert decode(result.albedo, rgb=True).shape == (48, 48, 3)
    mask = decode(result.surface).ravel()
    assert np.all(mask[seen & (acc.owner // 2 == 0)] == 2)
    assert np.all(mask[acc.owner // 2 == 5] == 0)


@pytest.mark.parametrize("distortion", [("OPENCV_PINHOLE", (-.25, .07, .002, -.002)),
                                         ("OPENCV_FISHEYE", (.01, -.001, 0., 0.))])
def test_lens_distortion_color_alignment(distortion):
    acc, vertices, planes = accumulator()
    view = render(camera((3, 2, 1), distortion=distortion), planes)
    assert acc.accumulate("w-1", vertices, [view], 1000)
    seen = acc.weights > 0
    assert seen.sum() > 100
    fi = acc.owner[seen]
    local = np.tile([[0, 0], [1, 0], [1, 1], [0, 1]], (6, 1))
    xy = np.einsum("ni,nij->nj", acc.bary[seen], local[acc.faces[fi]])
    assert np.mean(np.abs(acc.colors[seen] - known_color(fi // 2, xy[:, 0], xy[:, 1]))) < 2


def test_other_person_z_buffer_masks_and_screen_bounds():
    acc, vertices, planes = accumulator()
    cam = camera((3, 2, 1))
    view = render(cam, planes)
    # 가림막 상자를 카메라 쪽으로 1m 이동하면 뒤 사람의 관측량이 줄어든다.
    occluder = vertices + cam.center / np.linalg.norm(cam.center)
    acc.accumulate("w-1", vertices, [view], 1000, occluders=[(occluder, acc.faces)])
    occluded = np.count_nonzero(acc.weights)
    baseline, _, _ = accumulator()
    baseline.accumulate("w-1", vertices, [view], 1000)
    assert occluded < np.count_nonzero(baseline.weights) * .3
    empty = replace(view, mask=np.zeros((128, 128), np.uint8))
    blank, _, _ = accumulator()
    assert not blank.accumulate("w-1", vertices, [empty], 1000)
    assert not blank.weights.any()
    assert not blank.accumulate("w-1", vertices + [100, 0, 0], [view], 1001)
    assert not blank.weights.any()
    outside_box = replace(view, mask=None, bbox=[-100, -100, -50, -50], capture_time_ms=1002)
    assert not blank.accumulate("w-1", vertices, [outside_box], 1002)
    assert not blank.weights.any()


def test_outliers_weighted_average_and_gate_abort_are_transactional(caplog):
    caplog.set_level("INFO")
    acc, vertices, planes = accumulator()
    view = render(camera((3, 2, 1)), planes)
    acc.accumulate("w-1", vertices, [view], 1000)
    colors, weights = acc.colors.copy(), acc.weights.copy()
    changed = replace(view, image_bgr=np.clip(view.image_bgr.astype(int) + 10, 0, 255).astype(np.uint8), capture_time_ms=1001)
    acc.accumulate("w-1", vertices, [changed], 1001)
    np.testing.assert_allclose(acc.colors[weights > 0], colors[weights > 0] + 5, atol=1e-5)
    colors = acc.colors.copy()
    bad = replace(view, image_bgr=np.full_like(view.image_bgr, 255), capture_time_ms=1002)
    assert not acc.accumulate("w-1", vertices, [bad], 1002)
    np.testing.assert_array_equal(acc.colors, colors)
    checks = [0]
    def during_raster():
        checks[0] += 1
        return checks[0] < 5
    assert not acc.accumulate("w-1", vertices, [replace(changed, capture_time_ms=1004)], 1004, admit=during_raster)
    np.testing.assert_array_equal(acc.colors, colors)
    checks = iter([True, True, False])
    assert not acc.accumulate("w-1", vertices, [replace(changed, capture_time_ms=1003)], 1003, admit=lambda: next(checks))
    np.testing.assert_array_equal(acc.colors, colors)
    assert "cpu_ms=" in caplog.text and "wall_ms=" in caplog.text and "rejected=" in caplog.text


def test_identity_freeze_30_frames_revision_and_new_id():
    acc, vertices, planes = accumulator(freeze=30)
    view = render(camera((3, 2, 1)), planes)
    for t in range(1001, 1029):
        assert not acc.track(0, "CONFIRMED", t)
    assert acc.track(0, "CONFIRMED", 1029)
    assert acc.accumulate("w-1", vertices, [replace(view, capture_time_ms=1029)], 1029)
    result = acc.result()
    acc.confirm(result, 1029)
    old = acc.weights.copy()
    assert not acc.track(0, "IDENTITY_UNCERTAIN", 1030)
    assert not acc.accumulate("w-1", vertices, [view], 1030)
    np.testing.assert_array_equal(acc.weights, old)
    assert acc.last_confirmed is result
    for t in range(1031, 1060):
        assert not acc.track(0, "CONFIRMED", t)
    assert acc.track(0, "CONFIRMED", 1060)
    assert not acc.track(1, "CONFIRMED", 1061)
    assert not acc.weights.any() and acc.last_confirmed is result
    # 같은 프레임 중복 관측은 안정 프레임 수를 늘리지 않는다.
    for _ in range(40):
        assert not acc.track(1, "CONFIRMED", 1061)
    for t in range(1062, 1090):
        assert not acc.track(1, "CONFIRMED", t)
    assert acc.track(1, "CONFIRMED", 1090)
    new, _, _ = accumulator()
    assert new.last_confirmed is None and not new.weights.any()


@pytest.mark.parametrize("policy", ["OBSERVED", "BLURRED", "EXCLUDED"])
def test_face_policies_never_interpolate_or_invent_face(policy):
    acc, vertices, planes = accumulator(policy=policy, seam=2)
    view = render(camera((3, 2, 1)), planes)
    acc.accumulate("w-1", vertices, [view], 1000)
    result = acc.result()
    mask = decode(result.surface).ravel()
    rgb = decode(result.albedo, rgb=True).reshape(-1, 3)
    assert set(np.unique(mask)) <= {0, 1, 2, 128, 130}
    assert not np.any(mask == 129)
    assert np.all(rgb[mask == 128] == 0)
    if policy == "EXCLUDED":
        assert result.face_ratios == {"OBSERVED": 0., "UNOBSERVED": 1.}
        assert not np.any(mask == 130)
    else:
        assert np.any(mask == 130)
        if policy == "OBSERVED":
            np.testing.assert_array_equal(rgb[mask == 130], np.rint(acc.colors[mask == 130]).astype(np.uint8))
        else:
            assert np.any(rgb[mask == 130] != np.rint(acc.colors[mask == 130]).astype(np.uint8))


def test_seam_fill_width_and_face_no_129():
    acc, _, _ = accumulator(resolution=48, seam=2)
    acc.colors[:] = [90, 80, 70]
    # 몸과 얼굴에 관측 텍셀 한 개씩만 둔다.
    body = np.flatnonzero(acc.valid & ~acc.face)[200]
    face = np.flatnonzero(acc.face)[100]
    acc.weights[[body, face]] = 1
    mask = decode(acc.result().surface)
    y, x = divmod(body, 48)
    iy, ix = np.where(mask == 1)
    assert len(iy) > 0 and np.all(np.abs(iy - y) + np.abs(ix - x) <= 2)
    assert 129 not in mask
    assert mask.ravel()[face] == 130


def test_sync_and_upload_schedule():
    acc, vertices, planes = accumulator()
    view = render(camera((3, 2, 1)), planes)
    assert not acc.accumulate("w-1", vertices, [replace(view, capture_time_ms=900)], 1000)
    assert not acc.accumulate("w-1", vertices, [replace(view, capture_time_ms=970), replace(view, capture_time_ms=1030)], 1000)
    assert acc.accumulate("w-1", vertices, [view], 1000)
    assert acc.due(1000)
    acc.confirm(acc.result(), 1000)
    assert not acc.due(2000)  # 새 관측 없으면 시간만 지나도 게시하지 않음.
    assert acc.accumulate("w-1", vertices, [replace(view, capture_time_ms=1010)], 1010)
    assert not acc.due(1010)
    assert acc.due(1100)


def pipeline_scene(rig, policy="OBSERVED"):
    vertices, faces, uv, planes = cube()
    rig = replace(rig, faces=faces, texcoords=uv, texcoord_faces=faces,
                  base_shape_cm=vertices * 100, shape_vectors_cm=np.zeros((45, len(vertices), 3)),
                  skin_joints=np.ones((len(vertices), 4), int), skin_weights=np.tile([1., 0, 0, 0], (len(vertices), 1)))
    J = rig.joint_count
    baked = PersonRig(rig, vertices, np.zeros((J, 3)), np.tile([0., 0., 0., 1.], (J, 1)),
                      np.ones(J), np.tile(np.eye(4), (J, 1, 1)))
    example = json.loads((Path(__file__).parents[3] / "contracts/examples/avatar_asset.textured.json").read_text(encoding="utf-8"))
    asset = example["payload"]
    asset.update(world_id="w-1", appearance_version=0, previous_appearance_version=None, identity_revision=0,
                 texture=None, surface_source={"mask_uri": None, "ratios": {"OBSERVED": 0., "INTERPOLATED": 0., "UNOBSERVED": 1.},
                                               "face_ratios": {"OBSERVED": 0., "UNOBSERVED": 1.}})
    state = SimpleNamespace(baked=baked, asset=asset, revision=0, stable=30, version=0, high_version=0, history={0: copy.deepcopy(asset)})
    region = {"asset_sha256": rig.asset_sha256, "face_indices": [2, 3]}
    pipeline = AppearancePipeline(AppearanceConfig(resolution=48, freeze_frames=1, seam_width=1,
        sample_interval_ms=1, publish_interval_ms=100, face_policy=policy), face_region=region)
    pipeline.register("w-1", state, 1000)
    cam = camera((3, 2, 1))
    view = render(cam, planes)
    person = SimpleNamespace(world_id="w-1", pelvis_xyz=[0., 0., 0.], root_rotation=[0., 0., 0.],
                             joint_rotations=[[0., 0., 0.]] * J, candidates={cam.camera_id: True},
                             representative_camera=cam.camera_id)
    frame = SimpleNamespace(people=[person], calibration_version=asset["calibration_version"])
    track = {"world_id": "w-1", "identity_revision": 0, "identity_confidence": "CONFIRMED", "per_camera": [
        {"camera_id": cam.camera_id, "frame_ref": {"frame_sequence": 1}, "bbox": [0, 0, 128, 128], "mask": view.mask}]}
    doc = {"t": 1000, "tracks": [track], "input_provenance": asset["input_provenance"]}
    snapshot = {"frames": {cam.camera_id: {"frame_ref": {"frame_sequence": 1}, "capture_time_ms": 1000}}}
    messages, uploaded = [], []
    sender = SimpleNamespace(epoch=asset["stream_epoch"], asset=lambda a: messages.append(copy.deepcopy(a)))
    def upload(wid, epoch, version, name, data):
        uploaded.append((version, name, data))
        return f"s3://avatars/{wid}/e{epoch}/v{version}/{name}"
    args = (frame, doc, snapshot, {cam.camera_id: view.image_bgr}, SimpleNamespace(cameras={cam.camera_id: cam}),
            {"w-1": state}, SimpleNamespace(png=upload), sender, lambda: 1000, lambda: True)
    return pipeline, state, messages, uploaded, args, example


@pytest.mark.parametrize("policy", ["OBSERVED", "BLURRED", "EXCLUDED"])
def test_pipeline_uploads_schema_valid_versions_and_retries_identical_png(rig, policy):
    pipeline, state, messages, uploaded, args, example = pipeline_scene(rig, policy)
    original_upload = args[6].png
    def flaky(*params):
        uri = original_upload(*params)
        if len(uploaded) == 2:
            raise OSError("PUT 응답 유실")
        return uri
    args[6].png = flaky
    with pytest.raises(OSError):
        pipeline.update(*args)
    assert state.version == 0 and not messages
    args[1]["t"] += 10
    pipeline.update(*args)
    assert len(messages) == 1 and state.version == 1
    assert uploaded[0][2] is uploaded[2][2] and uploaded[1][2] is uploaded[3][2]
    for asset in messages:
        example["payload"] = asset
        assert schema_errors(example, "avatar_asset") == []
        assert sum(asset["surface_source"]["ratios"].values()) == pytest.approx(1)
        assert asset["previous_appearance_version"] == 0
    # 시간 조건으로 다음 버전을 발행하고 이전 사전은 변경하지 않는다.
    previous = copy.deepcopy(messages[0])
    args[1]["t"] = 1200
    args[2]["frames"]["cam1"]["capture_time_ms"] = 1200
    pipeline.update(*args)
    assert state.version == 2 and state.asset["previous_appearance_version"] == 1
    assert messages[0] == previous and state.history[1] == previous


def test_pipeline_uncertain_blocks_pending_publication_and_new_id_has_no_state(rig):
    pipeline, state, messages, uploaded, args, _ = pipeline_scene(rig)
    original_upload = args[6].png
    args[6].png = lambda *params: (_ for _ in ()).throw(OSError("failure"))
    with pytest.raises(OSError):
        pipeline.update(*args)
    assert pipeline.pending
    tr = args[1]["tracks"][0]
    tr["identity_confidence"] = "IDENTITY_UNCERTAIN"
    pipeline.track(tr, 1001)
    assert not pipeline.pending
    pipeline.update(*args)
    assert state.version == 0 and not messages
    pipeline.track({**tr, "world_id": "w-split"}, 1002)
    assert "w-split" not in pipeline.people
    tr["identity_confidence"] = "CONFIRMED"
    pipeline.track(tr, 1003)  # 이 시험의 freeze_frames=1.
    args[1]["t"] = 1003
    args[2]["frames"]["cam1"]["capture_time_ms"] = 1003
    args[6].png = original_upload
    pipeline.update(*args)
    assert state.version == 2 and state.asset["previous_appearance_version"] == 0
    assert all(version == 2 for version, _, _ in uploaded)


def test_face_region_env_validation_and_lbs_uses_tier0_pelvis(rig):
    assert appearance_from_env({}, cuda_available=False) is None  # 기본 auto: CUDA 없으면 끔
    on = appearance_from_env({}, cuda_available=True)  # 기본 auto: CUDA 있으면 GPU 누적
    assert on is not None and on.config.device == "cuda"
    assert appearance_from_env({"SIMAI_AVATAR_APPEARANCE": "1"}, cuda_available=False).config.device == "cpu"
    assert appearance_from_env({"SIMAI_AVATAR_APPEARANCE": "0", "SIMAI_AVATAR_FACE_REGION": "does-not-exist"},
                               cuda_available=True) is None
    with pytest.raises(ValueError):
        appearance_from_env({"SIMAI_AVATAR_APPEARANCE": "yes"}, cuda_available=True)
    with pytest.raises(ValueError):
        AppearanceConfig(seam_width=3)
    with pytest.raises(ValueError, match="얼굴 영역 없음"):
        face_faces_from_rig(rig)
    with pytest.raises(ValueError, match="해시"):
        face_faces_from_rig(rig, {"asset_sha256": "wrong"})
    names = list(rig.joint_names)
    names[2] = "head"
    assert face_faces_from_rig(replace(rig, joint_names=tuple(names))).any()
    pipeline, state, _, _, args, _ = pipeline_scene(rig)
    person = args[0].people[0]
    expected, _, _, _ = cube()
    np.testing.assert_allclose(posed_world_vertices(state.baked, person), expected)
    person.pelvis_xyz = [2., 3., 4.]
    np.testing.assert_allclose(posed_world_vertices(state.baked, person), expected + [2, 3, 4])
    person.pelvis_xyz = None
    assert posed_world_vertices(state.baked, person) is None
    # 실제 내보낸 TEXCOORD_0와 PNG 텍셀 좌표가 같은 v 뒤집기를 쓰는지 확인.
    glb = build_skinned_glb(state.baked)
    document, binary = read_glb(glb.data)
    attributes = document["meshes"][0]["primitives"][0]["attributes"]
    exported_uv = accessor_array(document, binary, attributes["TEXCOORD_0"])
    original_uv = state.baked.rig.texcoords.copy()
    original_uv[:, 1] = 1 - original_uv[:, 1]
    np.testing.assert_allclose(exported_uv, original_uv, atol=1e-7)


def test_diagnostic_files_and_before_after_comparison(rig, tmp_path):
    pipeline, state, messages, uploaded, args, _ = pipeline_scene(rig)
    pipeline.output_dir = tmp_path
    pipeline.update(*args)
    before = tmp_path / "w-1" / f'e{args[7].epoch}' / "v1"
    assert (before / "albedo.png").read_bytes() == uploaded[0][2].data
    assert (before / "surface_source.png").read_bytes() == uploaded[1][2].data
    result = pipeline.people["w-1"].last_confirmed
    assert save_result(tmp_path, state.asset, result) == before
    with pytest.raises(ValueError, match="덮어쓰기"):
        save_result(tmp_path, {**state.asset, "model_version": "other"}, result)
    args[1]["t"] = 1200
    args[2]["frames"]["cam1"]["capture_time_ms"] = 1200
    pipeline.update(*args)
    after = before.parent / "v2"
    output = tmp_path / "comparison.png"
    comparison(before, after, output)
    figure = cv2.imdecode(np.frombuffer(output.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert figure.shape == (136, 96, 3)


def test_pipeline_pure_merge_keeps_texture_accumulation_and_labels_new_version_with_new_revision(rig):
    """S15P31A101-661: 순수 병합이면 포즈 루프의 revision_base 가 그대로라 누적을 잇고, 새 버전은 새 차수로 적는다."""
    pipeline, state, messages, uploaded, args, _ = pipeline_scene(rig)
    acc = pipeline.people["w-1"]
    state.revision, state.revision_base = 1, 0
    tr = {**args[1]["tracks"][0], "identity_revision": 1, "aliases": ["w-9"]}
    pipeline.track(tr, 1001, identity_key=0)
    assert pipeline.people["w-1"] is acc and acc.revision == 0 and acc.ready
    pipeline.update(*args)
    assert messages and messages[-1]["identity_revision"] == 1 and messages[-1]["appearance_version"] == 1
    pipeline.track({**tr, "identity_revision": 2}, 1002)   # 분할·그 밖의 차수 상승: 트랙 차수로 누적을 비운다
    assert acc.revision == 2 and acc.stable == 1


# ---- S15P31A101-743 외형 승계(표시 전용) ----

def test_merge_absorb_unions_observed_texels_with_quality_weighted_average():
    acc, vertices, planes = accumulator()
    other, _, _ = accumulator()
    assert acc.accumulate("w-1", vertices, [render(camera((3, 2, 1)), planes)], 1000)
    assert other.accumulate("w-2", vertices, [render(camera((-3, 2, 1), "cam2"), planes)], 1000)
    mine, theirs = acc.weights > 0, other.weights > 0
    both, only_theirs = mine & theirs, theirs & ~mine
    assert both.any() and only_theirs.any()
    expected = (acc.weights[both, None] * acc.colors[both] + other.weights[both, None] * other.colors[both]) / (
        acc.weights[both] + other.weights[both])[:, None]
    expected_weights = acc.weights[both] + other.weights[both]
    before = other.colors.copy()
    assert acc.absorb(other.snapshot(), inherit=False)
    np.testing.assert_allclose(acc.colors[both], expected, rtol=1e-5)
    np.testing.assert_allclose(acc.weights[both], expected_weights, rtol=1e-6)
    np.testing.assert_array_equal(acc.colors[only_theirs], other.colors[only_theirs])
    assert not acc.inherited.any() and acc.carry_publish and acc.dirty and acc.due(1000)
    np.testing.assert_array_equal(other.colors, before)   # 주는 쪽 배열은 바꾸지 않는다


def test_inherited_texels_fill_only_unobserved_and_first_own_observation_overwrites():
    acc, vertices, planes = accumulator()
    donor, _, _ = accumulator()
    view = render(camera((3, 2, 1)), planes)
    assert acc.accumulate("w-7", vertices, [view], 1000)
    own = acc.weights > 0
    own_colors = acc.colors.copy()
    wrong = replace(render(camera((-3, 2, 1), "cam2"), planes), capture_time_ms=1000)
    wrong = replace(wrong, image_bgr=np.full_like(wrong.image_bgr, 250))   # 다른 사람 옷 색(거의 흰색)
    assert donor.accumulate("w-1", vertices, [wrong], 1000)
    donor_seen = donor.weights > 0
    assert acc.absorb(donor.snapshot(), inherit=True)
    np.testing.assert_array_equal(acc.colors[own], own_colors[own])                 # 자기 관측 유지
    assert np.array_equal(acc.inherited, donor_seen & ~own) and acc.inherited.any()
    assert acc.inherited_ratio() > 0
    # 새 관측은 이어받은 텍셀과 색이 크게 달라도(이상치 기준 80 넘음) 평균하지 않고 덮는다.
    true_view = replace(render(camera((-3, 2, 1), "cam2"), planes), capture_time_ms=1010)
    assert acc.accumulate("w-7", vertices, [true_view], 1010)
    fresh = acc.weights > 0
    reference, _, _ = accumulator()
    reference.accumulate("w-x", vertices, [true_view], 1010)
    overwritten = (reference.weights > 0) & donor_seen & ~own
    assert overwritten.any() and not acc.inherited[overwritten].any()
    np.testing.assert_allclose(acc.colors[overwritten], reference.colors[overwritten], atol=1e-3)
    assert fresh.sum() >= own.sum()


def test_reregister_after_shape_rebake_keeps_texels_and_revision_reset_clears_them(rig):
    pipeline, state, messages, uploaded, args, _ = pipeline_scene(rig)
    pipeline.update(*args)
    acc = pipeline.people["w-1"]
    seen = acc.weights.copy()
    assert seen.any()
    state.baked = replace(state.baked)          # 최종 체형으로 다시 구움 → 다시 등록
    pipeline.register("w-1", state, 1001)
    np.testing.assert_array_equal(pipeline.people["w-1"].weights, seen)
    state.revision_base = 1                     # 처음부터 다시 쌓기(분할 등)
    pipeline.register("w-1", state, 1002)
    assert not pipeline.people["w-1"].weights.any()


def test_pipeline_publishes_carried_texels_once_before_stable_frames(rig):
    pipeline, state, messages, uploaded, args, _ = pipeline_scene(rig)
    donor_pipeline, _, _, _, donor_args, _ = pipeline_scene(rig)
    donor_pipeline.update(*donor_args)
    snapshot = donor_pipeline.snapshot("w-1")
    assert snapshot.weights.any()
    acc = pipeline.people["w-1"]
    acc.stable, acc.ready = 0, False              # 이어받은 직후: 이 ID 는 아직 안정 프레임 전
    pipeline.carry_in("w-1", snapshot, inherit=True)
    pipeline.apply_carry("w-1")
    assert pipeline.carry_events == {"merged": 0, "inherited": 1} and acc.carry_publish
    args[1]["t"] += 5
    pipeline.update(*args)
    assert len(messages) == 1 and messages[0]["texture"] is not None and not acc.carry_publish
    accumulated = acc.weights.copy()
    args[1]["t"] += 5
    pipeline.update(*args)
    assert len(messages) == 1                      # 다시 내지 않고, 안정 전이라 새 관측도 쌓지 않는다
    np.testing.assert_array_equal(acc.weights, accumulated)
