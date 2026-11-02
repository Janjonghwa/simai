import numpy as np
import pytest

pytest.importorskip("cv2")  # overlay 는 extra io 의 OpenCV 로 그린다

from simai_ai.vlm.face_blur import prepare_vlm_frames
from simai_ai.vlm.overlay import clip_polygon, overlay, project
from simai_ai.vlm.rationale import build_prompt


class Empty:
    def people(self, image):
        return []


class Failed:
    def people(self, image):
        raise ValueError('failed')


def camera():
    return {'image_size': [100, 100], 'K': [[50, 0, 50], [0, 50, 50], [0, 0, 1]],
            'R': np.eye(3).tolist(), 't': [0, 0, 0], 'distortion': {'model': 'NONE', 'coeffs': []}}


def test_projection_and_near_clipping():
    np.testing.assert_allclose(project(camera(), np.array([[1., 0., 2.]])), [[75, 50]])
    poly = np.array([[-1., 0., -1.], [1., 0., 1.], [0., 1., 1.]])
    clipped = clip_polygon(poly, 2, .01, True)
    assert len(clipped) == 4 and np.all(clipped[:, 2] >= .009999)
    assert len(clip_polygon(-np.ones((3, 3)), 2, .01, True)) == 0


def test_overlay_inside_outside_and_face_failure():
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    safe = prepare_vlm_frames([('x', image)], 'BLURRED', Empty()).frames[0]
    state = {'event_zone_id': 'z', 'zone': {'zone_id': 'z', 'height_m': 2, 'polygon_xy': [[-1,-1], [1,-1], [1,1], [-1,1]]}}
    result, drawn = overlay(safe, {}, state, [], camera())
    assert drawn and result.pixels[50, 50, 2] > 0 and not image.any()
    state['zone']['polygon_xy'] = [[10,10], [11,10], [11,11]]
    assert overlay(safe, {}, state, [], camera())[1] is False
    assert not prepare_vlm_frames([('x', image)], 'BLURRED', Failed()).frames


def test_prompt_overlay_only_when_applied():
    inp = {'observation_time': 0, 'rule_id': 'R1', 'rule_version': 1, 'severity': 'HIGH',
           'confidence': 'HIGH', 'needs_check': False, 'structured_state': {}, 'metrics': {}, 'zone_id': None}
    assert '빨간 영역' not in build_prompt(inp, [])
    text = build_prompt({**inp, 'overlay_applied': True}, [])
    assert '빨간 영역은 위험 구역' in text and '노란 박스' in text
    assert '비계' not in text and '책상' not in text


def test_projection_uses_distortion_and_marker():
    cam = camera()
    cam['distortion'] = {'model': 'OPENCV_PINHOLE', 'coeffs': [0.1, 0, 0, 0, 0]}
    np.testing.assert_allclose(project(cam, np.array([[1., 0., 2.]])), [[75.625, 50]])
    frame = prepare_vlm_frames([('x', np.zeros((100, 100, 3), dtype=np.uint8))], 'EXCLUDED', Empty()).frames[0]
    state = {'tracks': [{'world_id': 'w', 'ground_anchor_xyz': [0, 0, 2]}]}
    result, drawn = overlay(frame, {}, state, ['w'], cam)
    assert drawn and result.pixels[50, 56, 1] > 0
