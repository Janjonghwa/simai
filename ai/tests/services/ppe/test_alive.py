"""PPE 생존 파일(S15P31A101-634, services/ppe/alive.py)."""

from __future__ import annotations

from pathlib import Path

from services.ppe.alive import ALIVE_ENV, AliveWriter, alive_path, read_alive_ms


def test_path_comes_from_env_or_defaults_under_state(tmp_path: Path) -> None:
    assert alive_path({ALIVE_ENV: str(tmp_path / "x.alive")}, tmp_path) == tmp_path / "x.alive"
    assert alive_path({ALIVE_ENV: "  "}, tmp_path) == tmp_path / "state" / "ppe.alive"
    assert alive_path({}, tmp_path) == tmp_path / "state" / "ppe.alive"


def test_beat_writes_at_most_once_per_period_and_creates_the_folder(tmp_path: Path) -> None:
    path = tmp_path / "state" / "ppe.alive"
    writer = AliveWriter(path, period_ms=1_000)
    assert writer.beat(10_000) is True
    assert read_alive_ms(path) == 10_000
    assert writer.beat(10_500) is False  # 1초 안에는 다시 쓰지 않는다(0.05 s 주기마다 디스크를 두드리지 않게)
    assert read_alive_ms(path) == 10_000
    assert writer.beat(11_000) is True
    assert read_alive_ms(path) == 11_000
    assert not path.with_name("ppe.alive.tmp").exists()  # 바꿔 넣기라 임시 파일이 남지 않는다


def test_read_returns_none_for_missing_or_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "ppe.alive"
    assert read_alive_ms(path) is None
    path.write_text("not-a-number", encoding="ascii")
    assert read_alive_ms(path) is None


def test_write_failure_is_counted_and_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="ascii")
    writer = AliveWriter(blocker / "sub" / "ppe.alive")  # 부모가 파일이라 폴더를 만들 수 없다
    assert writer.beat(1_000) is False
    assert writer.failed == 1
