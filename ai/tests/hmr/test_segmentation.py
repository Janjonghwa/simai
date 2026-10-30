"""가짜 seg·합성 영상으로 프롬프트·역사상·폴백을 검증한다. 제품 성능 지표가 아니다."""

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
torch = pytest.importorskip("torch")

from simai_ai.calibration.intrinsics.estimate import WeightsRef
from simai_ai.hmr.prompt import HmrImageMap, SegInstance
from simai_ai.hmr.runtime import FastBodyModel
from simai_ai.hmr.segmentation import AvatarSegmentation, YoloPersonSegmenter, segmentation_from_env
from tests.hmr.test_appearance import pipeline_scene
from tests.hmr.test_avatar_loop import harness, snapshot


def inputs(distorted=False):
    imap = HmrImageMap.from_intrinsics("cam", [[45., 0, 32.], [0, 45., 24.], [0, 0, 1]],
        (64, 48), "OPENCV_PINHOLE" if distorted else "NONE", (-.3, .05, .001, -.002) if distorted else ())
    imap = replace(imap, K_used=imap.camera.K)
    image = np.full((48, 64, 3), [10, 20, 30], np.uint8)
    jobs = [({"world_id": "w-1", "identity_revision": 2},
             {"camera_id": "cam", "bbox": [12., 8., 50., 40.]})]
    rig = SimpleNamespace(cameras={"cam": imap.camera}, calibration_version="cal-1")
    return imap, image, jobs, rig


class FakeSeg:
    def __init__(self, instances=None, failure=None):
        self.instances, self.failure, self.calls = instances, failure, []

    def predict(self, images):
        self.calls.append(images)
        if self.failure:
            raise self.failure
        return [self.instances or [] for image in images]


def matched_seg(imap, jobs):
    mask = np.zeros(imap.size[::-1], bool)
    mask[10:38, 14:48] = True
    return FakeSeg([SegInstance(imap.bbox(jobs[0][1]["bbox"]), .9, mask)])


def test_batch_prepares_same_hmr_image_and_original_mask_without_mutating_inputs(caplog):
    caplog.set_level("INFO")
    imap, image, jobs, rig = inputs(distorted=True)
    seg = matched_seg(imap, jobs)
    pipeline = AvatarSegmentation(seg)
    before = copy.deepcopy(jobs)
    result = pipeline.prepare(jobs, {"cam": image}, rig)[("w-1", "cam")]
    assert jobs == before and len(seg.calls) == 1
    assert result.image is seg.calls[0][0]
    np.testing.assert_array_equal(result.image, imap.prepare_image(image))
    assert result.prompt.mask is not None
    assert result.original_mask.shape == (48, 64)
    assert "cameras=1 instances=1 matched=1" in caplog.text and "elapsed_ms=" in caplog.text
    # 역변환 표는 같은 보정 버전에서 재사용하고 버전 변경 때 비운다.
    cached = pipeline._maps["cam"]
    pipeline.prepare(jobs, {"cam": image}, rig)
    assert pipeline._maps["cam"] is cached
    rig.calibration_version = "cal-2"
    pipeline.prepare(jobs, {"cam": image}, rig)
    assert pipeline._maps["cam"] is not cached


@pytest.mark.parametrize("distorted", [False, True])
def test_mask_inverse_matches_independent_opencv_points_with_new_intrinsics(distorted):
    imap, image, jobs, rig = inputs(distorted)
    # 별도 K·출력 크기·영상 밖을 포함한다. 원본에서 HMR 방향으로 표본을 읽어 구멍을 만들지 않는다.
    K_used = imap.camera.K.copy()
    K_used[:2] *= .8
    K_used[0, 2] -= 8
    imap = replace(imap, K_used=K_used, image_size=(48, 38))
    yy, xx = np.indices((38, 48))
    mask = ((xx // 3 + yy // 4) % 2 == 0)
    raw = imap.mask_to_original(mask)
    yy, xx = np.indices((48, 64))
    pixels = np.stack([xx, yy], axis=-1).astype(float).reshape(-1, 1, 2)
    dist = np.asarray(imap.camera.distortion_coeffs) if distorted else np.zeros(4)
    criteria = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 100, 1e-12)
    if hasattr(cv2, "undistortPointsIter"):
        mapped = cv2.undistortPointsIter(pixels, imap.camera.K, dist, None, K_used, criteria)
    else:  # OpenCV 5는 criteria 인자를 undistortPoints로 합쳤다.
        mapped = cv2.undistortPoints(pixels, imap.camera.K, dist, R=None, P=K_used, criteria=criteria)
    mapped = mapped.reshape(48, 64, 2)
    nearest = np.rint(mapped).astype(int)
    valid = (nearest[..., 0] >= 0) & (nearest[..., 0] < 48) & (nearest[..., 1] >= 0) & (nearest[..., 1] < 38)
    expected = np.zeros((48, 64), bool)
    expected[valid] = mask[nearest[..., 1][valid], nearest[..., 0][valid]]
    np.testing.assert_array_equal(raw, expected)
    assert (~valid).any()
    with pytest.raises(ValueError, match="해상도"):
        imap.mask_to_original(np.zeros((2, 2), bool))


def test_low_iou_is_bbox_only_and_one_instance_cannot_be_shared():
    imap, image, jobs, rig = inputs()
    mask = np.ones((48, 64), bool)
    seg = FakeSeg([SegInstance((0., 0., 5., 5.), .99, mask)])
    pipeline = AvatarSegmentation(seg)
    result = pipeline.prepare(jobs, {"cam": image}, rig)
    assert result[("w-1", "cam")].prompt.mask is None
    assert result[("w-1", "cam")].original_mask is None
    jobs.append(({"world_id": "w-2", "identity_revision": 0}, copy.deepcopy(jobs[0][1])))
    seg.instances = [SegInstance(tuple(jobs[0][1]["bbox"]), .9, mask)]
    result = pipeline.prepare(jobs, {"cam": image}, rig)
    assert sum(p.prompt.mask is not None for p in result.values()) == 1
    jobs.append(copy.deepcopy(jobs[0]))
    result = pipeline.prepare(jobs, {"cam": image}, rig)
    assert ("w-1", "cam") not in result


@pytest.mark.parametrize("failure", [RuntimeError("synthetic OOM"), FileNotFoundError("missing weights")])
def test_failure_warns_once_and_does_not_retry_every_frame(failure, caplog):
    imap, image, jobs, rig = inputs()
    seg = FakeSeg(failure=failure)
    pipeline = AvatarSegmentation(seg)
    for _ in range(3):
        assert pipeline.prepare(jobs, {"cam": image}, rig) == {}
    assert len(seg.calls) == 1 and pipeline.failed
    assert caplog.text.count("bbox만 사용") == 1
    assert "synthetic OOM" not in caplog.text


def test_wrong_mask_shape_falls_back_without_resizing_letterbox(caplog):
    imap, image, jobs, rig = inputs()
    pipeline = AvatarSegmentation(FakeSeg([SegInstance((12, 8, 50, 40), .9, np.ones((24, 32), bool))]))
    assert pipeline.prepare(jobs, {"cam": image}, rig) == {}
    assert pipeline.failed and "bbox만 사용" in caplog.text


def test_env_auto_off_on_and_manifest_hash(monkeypatch, caplog):
    assert segmentation_from_env({}, cuda_available=False) is None
    # 끄면 CUDA 검사·가중치·모델을 건드리지 않는다.
    monkeypatch.setattr(torch.cuda, "is_available", lambda: pytest.fail("CUDA를 확인하면 안 됨"))
    assert segmentation_from_env({"SIMAI_AVATAR_SEG": "0", "SIMAI_AVATAR_SEG_WEIGHTS": "missing"}) is None
    auto = segmentation_from_env({}, cuda_available=True, device="cuda:2")
    assert auto.device == auto.segmenter.device == "cuda:2"
    forced = segmentation_from_env({"SIMAI_AVATAR_SEG": "1"}, cuda_available=False)
    assert forced.device == "cpu" and forced.segmenter._model is None
    with pytest.raises(ValueError, match="auto, 0, 1"):
        segmentation_from_env({"SIMAI_AVATAR_SEG": "yes"})
    manifest = json.loads((Path(__file__).parents[3] / "deploy/weights.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["weights"] if e["name"] == "yolo26x-seg")
    assert entry["sets"] == ["avatar", "exp02"]
    assert entry["env"] == "SIMAI_AVATAR_SEG_WEIGHTS"
    assert auto.segmenter.weights.sha256 == "sha256:" + entry["sha256"]
    override = segmentation_from_env({"SIMAI_AVATAR_SEG_WEIGHTS": "local.pt",
        "SIMAI_AVATAR_SEG_WEIGHTS_SHA256": "sha256:" + "a" * 64}, cuda_available=True)
    assert override.segmenter.weights == WeightsRef("local.pt", "sha256:" + "a" * 64)
    assert segmentation_from_env({"SIMAI_AVATAR_SEG_WEIGHTS_SHA256": "invalid"}, cuda_available=True) is None
    assert "bbox만 사용" in caplog.text


def test_yolo_is_lazy_verifies_hash_then_batches_people_only(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.pt"
    path.write_bytes(b"synthetic weights, not a torch checkpoint")
    weights = WeightsRef(str(path), "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
    loaded, calls = [], []
    def predict(images, **kwargs):
        calls.append(kwargs)
        out = []
        for image in images:
            h, w = image.shape[:2]
            out.append(SimpleNamespace(masks=SimpleNamespace(data=torch.ones((2, h, w))), boxes=SimpleNamespace(
                xyxy=torch.tensor([[1, 2, w-1, h-1], [0, 0, 2, 2]]), conf=torch.tensor([.9, .8]), cls=torch.tensor([0, 1]))))
        return out
    def load(path, task):
        loaded.append((path, task))
        return SimpleNamespace(predict=predict)
    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=load))
    seg = YoloPersonSegmenter(weights, device="cpu")
    assert not loaded
    images = [np.zeros((48, 64, 3), np.uint8), np.zeros((38, 48, 3), np.uint8)]
    batch = seg.predict(images)
    assert len(loaded) == 1 and len(calls) == 1 and len(batch) == 2
    assert all(len(items) == 1 for items in batch)
    assert batch[0][0].mask.shape == (48, 64) and batch[1][0].mask.shape == (38, 48)
    assert calls[0]["retina_masks"] and calls[0]["classes"] == [0] and calls[0]["device"] == "cpu"
    seg.predict(images)
    assert len(loaded) == 1
    bad = YoloPersonSegmenter(WeightsRef(str(path), "sha256:" + "0" * 64))
    with pytest.raises(ValueError):
        bad.predict(images)
    assert len(loaded) == 1


@pytest.mark.parametrize("matched", [True, False])
def test_runtime_passes_mask_only_when_matched_and_reuses_prepared_bgr(rig, matched):
    imap, image, jobs, camera_rig = inputs()
    seg = matched_seg(imap, jobs) if matched else FakeSeg()
    prepared = AvatarSegmentation(seg).prepare(jobs, {"cam": image}, camera_rig)[("w-1", "cam")]
    adapter = FastBodyModel.__new__(FastBodyModel)
    adapter.rig = rig
    calls = []
    def process(rgb, **kwargs):
        np.testing.assert_array_equal(rgb[0, 0], [30, 20, 10])
        calls.append(kwargs)
        return [{"mhr_model_params": np.zeros(204), "shape_params": np.zeros(2),
                 "pred_keypoints_3d": np.zeros((70, 3)), "pred_cam_t": np.zeros(3)}]
    adapter.estimator = SimpleNamespace(process_one_image=process)
    adapter.infer(image, jobs[0][1], jobs[0][0], imap.camera, prepared=prepared)
    assert ("masks" in calls[0]) == matched
    if matched:
        assert calls[0]["masks"].shape == (1, 48, 64) and calls[0]["masks"].dtype == np.uint8
        np.testing.assert_array_equal(calls[0]["masks"][0], prepared.prompt.mask)
    np.testing.assert_allclose(calls[0]["bboxes"][0], prepared.prompt.bbox, atol=1e-5)
    np.testing.assert_array_equal(image[0, 0], [10, 20, 30])


@pytest.mark.parametrize("failure", [False, True])
def test_loop_batches_four_cameras_once_reuses_masks_and_continues_on_seg_failure(rig, failure):
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=1)
    def full_snapshot(t):
        doc, _ = snapshot(t)
        images = {}
        for cam in doc["calibration"]["cameras"]:
            cam["distortion"] = {"model": "NONE", "coeffs": []}
            w, h = cam["image_size"]
            images[cam["camera_id"]] = np.zeros((h, w, 3), np.uint8)
        return doc, images
    source.item = full_snapshot(clock[0])
    calls = []
    class Seg:
        def predict(self, images):
            calls.append(images)
            if failure:
                raise RuntimeError("synthetic seg failure")
            observations = source.item[0]["world"]["tracks"][0]["per_camera"]
            return [[SegInstance(tuple(o["bbox"]), .9, np.ones(img.shape[:2], bool))]
                    for img, o in zip(images, observations)]
    loop.segmentation = AvatarSegmentation(Seg())
    received = []
    infer = model.infer
    def infer_with_prompt(image, obs, track, cam, *, prepared=None):
        received.append(prepared)
        return infer(image, obs, track, cam)
    model.infer = infer_with_prompt
    appearance_masks = []
    loop.appearance = SimpleNamespace(retain=lambda *_: None, track=lambda *_: None, register=lambda *_: None,
        config=SimpleNamespace(face_policy="OBSERVED"), update=lambda *args: appearance_masks.append(args[-1]))
    before = copy.deepcopy(source.item[0])
    loop.step()
    sender.tick()
    assert len(calls) == 1 and len(calls[0]) == 4 and model.calls == 4 and uploads
    assert source.item[0] == before
    assert any(m["msg_type"] == "avatar_pose_batch" for m in sock.messages)
    if failure:
        assert all(p is None for p in received) and appearance_masks == [{}]
    else:
        assert len(appearance_masks[0]) == 4 and all(p.prompt.mask is not None for p in received)
        for cid in source.item[1]:
            assert appearance_masks[0][("w-1", cid)].all()
    loop.step()  # 같은 촬영 시각은 seg도 반복하지 않는다.
    assert len(calls) == 1
    clock[0] += 200
    source.item = full_snapshot(clock[0])
    loop.step()
    assert len(calls) == (1 if failure else 2) and model.calls == 8


def test_loop_checks_admission_before_seg_and_again_before_hmr(rig):
    *_, model, loop, uploads = harness(rig)
    seg_calls = []
    loop.segmentation = SimpleNamespace(prepare=lambda *args: seg_calls.append(args) or {}, failed=True)
    checks = iter([True, False])
    loop.gate = SimpleNamespace(check=lambda: SimpleNamespace(admit=next(checks)))
    loop.step()
    assert not seg_calls and not model.calls
    *_, model, loop, uploads = harness(rig)
    loop.segmentation = SimpleNamespace(prepare=lambda *args: seg_calls.append(args) or {}, failed=True)
    checks = iter([True, True, False])
    loop.gate = SimpleNamespace(check=lambda: SimpleNamespace(admit=next(checks)))
    loop.step()
    assert len(seg_calls) == 1 and not model.calls


def test_disabled_seg_keeps_bbox_loop_working(rig):
    *_, model, loop, uploads = harness(rig, freeze=1)
    loop.segmentation = segmentation_from_env({"SIMAI_AVATAR_SEG": "0"})
    loop.step()
    assert model.calls == 4 and uploads


def test_original_coordinate_mask_reaches_appearance_view_and_intersects_silhouette(rig):
    pipeline, state, messages, uploaded, args, _ = pipeline_scene(rig)
    cam = next(iter(args[4].cameras.values()))
    imap = HmrImageMap(cam, cam.K)
    hmr_mask = np.zeros(cam.image_size[::-1], bool)
    hmr_mask[:, :64] = True
    raw_mask = imap.mask_to_original(hmr_mask)
    acc = pipeline.people["w-1"]
    original = acc.accumulate
    views = []
    def capture(wid, vertices, incoming, *rest, **kwargs):
        views.extend(incoming)
        return original(wid, vertices, incoming, *rest, **kwargs)
    acc.accumulate = capture
    pipeline.update(*args, masks={("w-1", cam.camera_id): raw_mask})
    assert len(views) == 1
    np.testing.assert_array_equal(views[0].mask, raw_mask)
    assert acc.config.silhouette_enabled
    # 유효한 메시 실루엣이 있어도 사람 마스크가 비어 있으면 관측 텍셀을 더하지 않는다.
    empty, _, _, _, empty_args, _ = pipeline_scene(rig)
    empty.update(*empty_args, masks={("w-1", cam.camera_id): np.zeros_like(raw_mask)})
    assert not (empty.people["w-1"].weights > 0).any()
    assert (acc.weights > 0).any()
