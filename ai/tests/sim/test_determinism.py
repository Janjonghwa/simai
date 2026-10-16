"""같은 입력이면 출력이 바이트 단위로 같아야 한다 (명세 21.3 시나리오 재생: 결과가 매번 같게)."""

from pathlib import Path

import pytest


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("scenario", ["basic", "podium", "crossing"])
def test_same_input_same_bytes(run_sim, tmp_path, scenario):
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        result = run_sim(scenario, out)
        assert result.returncode == 0, result.stderr
    files_a, files_b = _files(a), _files(b)
    assert sorted(files_a) == sorted(files_b)
    assert files_a == files_b, [k for k in files_a if files_a[k] != files_b.get(k)]


def test_seed_changes_noise_but_not_geometry(run_sim, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert run_sim("basic", a).returncode == 0
    assert run_sim("basic", b, "--seed", "7").returncode == 0
    files_a, files_b = _files(a), _files(b)
    assert files_a["calibration.json"] == files_b["calibration.json"]
    assert files_a["truth/world_truth.jsonl"] == files_b["truth/world_truth.jsonl"]
    assert files_a["observations/cam1.jsonl"] != files_b["observations/cam1.jsonl"]
