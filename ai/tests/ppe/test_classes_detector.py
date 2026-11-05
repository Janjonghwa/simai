"""PPE 클래스 이름 검사·검출 정규화·체크포인트 확인 (명세 5.5 모델, S15P31A101-491). GPU·모델 파일 없이 돈다."""

import hashlib

import pytest

from simai_ai.ppe.classes import CHECKPOINT_NAMES, SPEC_NAMES, normalize_candidates, validate_names
from simai_ai.ppe.detector import verify_checkpoint


@pytest.mark.parametrize("names", [CHECKPOINT_NAMES, SPEC_NAMES, dict(enumerate(CHECKPOINT_NAMES))])
def test_accepts_checkpoint_or_spec_names_in_order(names):
    assert validate_names(names) in (CHECKPOINT_NAMES, SPEC_NAMES)


@pytest.mark.parametrize("names", [
    ("NO_HELMET", "HELMET", "VEST", "NO_VEST"),               # 순서가 바뀜
    ("helmet_head", "no_helmet_head", "vest_torso"),          # 클래스 부족
    ("HELMET", "NO_HELMET", "VEST", "NO_VEST", "PERSON"),     # 클래스 추가
    {1: "HELMET", 2: "NO_HELMET", 3: "VEST", 4: "NO_VEST"},   # 번호가 0 부터가 아님
])
def test_rejects_other_names_or_order(names):
    with pytest.raises(ValueError):
        validate_names(names)


def test_output_uses_spec_names_and_item_state():
    out = normalize_candidates([[10, 20, 40, 70]] * 4, [0.9] * 4, [0, 1, 2, 3], 2304, 1296)
    assert [c.class_name for c in out] == list(SPEC_NAMES)
    assert [(c.item, c.candidate_state) for c in out] == [
        ("helmet", "WORN"), ("helmet", "NOT_WORN"), ("vest", "WORN"), ("vest", "NOT_WORN")]
    assert out[0].xyxy == (10.0, 20.0, 40.0, 70.0)


def test_no_detection_stays_empty():
    assert normalize_candidates([], [], [], 2304, 1296) == []


@pytest.mark.parametrize("boxes,scores,classes", [
    ([[0, 0, 30, 30]], [0.8], [4]),            # 없는 클래스
    ([[0, 0, 30, 30]], [0.8], [0.5]),          # 정수 아닌 클래스
    ([[0, 0, 30, 30]], [float("nan")], [0]),   # 수가 아님
    ([[-2, 0, 30, 30]], [0.8], [0]),           # 이미지 밖
    ([[0, 0, 3000, 30]], [0.8], [0]),
    ([[0, 0, 30, 30]], [], [0]),               # 길이가 다름
])
def test_malformed_detections_are_rejected(boxes, scores, classes):
    with pytest.raises(ValueError):
        normalize_candidates(boxes, scores, classes, 2304, 1296)


def test_checkpoint_is_checked_before_loading(tmp_path):
    path = tmp_path / "ppe.pt"
    path.write_bytes(b"weights")
    digest = hashlib.sha256(b"weights").hexdigest()
    assert verify_checkpoint(path, 7, digest) == path
    assert verify_checkpoint(path, None, "sha256:" + digest) == path
    with pytest.raises(ValueError):
        verify_checkpoint(path, 8, digest)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        verify_checkpoint(path, 7, digest)
    with pytest.raises(ValueError):
        verify_checkpoint(tmp_path / "missing.pt", None, digest)
