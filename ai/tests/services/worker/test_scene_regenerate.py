import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.worker.handlers import JobContext, JobFailed
from services.worker.scene_regenerate import SceneRegenerateHandler


def test_regenerate_worker_success_and_failures(tmp_path):
    bundle = tmp_path / ('bundle-' + 'a' * 32)
    bundle.mkdir()
    (bundle / 'COMPLETE').write_text('{}')
    calls = []
    submitter = SimpleNamespace(settings=SimpleNamespace(state_dir=tmp_path),
        _candidate=lambda: SimpleNamespace(bundle_dir=bundle, calibration_version='cal'),
        _active_calibration=lambda: ('cal', tmp_path / 'cal.json', 'CALIBRATED'),
        prepare=lambda b, c, check: {'ok': check()},
        _post=lambda payload: calls.append(payload) or {'job_id': 'job-1'})
    ctx = JobContext('job', 'lease', 'scene_regenerate', 1, {'request_id': 12, 'requested_at_ms': 1})
    marker = tmp_path / 'tier0.live'
    handler = SceneRegenerateHandler(submitter, marker, timeout_s=.02)
    assert handler(ctx)['static_reconstruct_job_id'] == 'job-1'
    assert calls[0]['request_id'] == 12
    submitter._candidate = lambda: 'NO_BUNDLE'
    with pytest.raises(JobFailed, match='TIER0_NOT_RUNNING'):
        handler(ctx)
    marker.touch()
    with pytest.raises(JobFailed, match='TIMEOUT'):
        handler(ctx)
    control = tmp_path / 'clean_background'
    (control / 'regenerate.response.json').write_text(json.dumps({'request_id': 12, 'status': 'NOT_READY'}))
    with pytest.raises(JobFailed, match='BACKGROUND_NOT_READY'):
        handler(ctx)
    (control / 'regenerate.response.json').write_text(json.dumps({'request_id': 12, 'status': 'READY', 'bundle_path': str(bundle), 'calibration_version': 'cal'}))
    submitter.prepare = lambda *args: (_ for _ in ()).throw(OSError())
    with pytest.raises(JobFailed, match='UPLOAD_FAILED'):
        handler(ctx)
