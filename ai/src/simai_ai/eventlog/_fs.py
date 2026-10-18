"""파일 내구성 보조 (저널·acked_offset 공용)."""

import os
import sys
from pathlib import Path

O_BINARY = getattr(os, "O_BINARY", 0)


def fsync_dir(path: Path) -> None:
    """새로 만들거나 지우거나 바꾼 파일 이름을 디렉터리에 굳힌다.

    Windows 는 디렉터리를 열어 fsync 할 수 없어 건너뛴다(NTFS 는 메타데이터를 자체 저널로 지킨다). 운영 저장소는
    RunPod Network Volume(Linux)이다.
    """
    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    """os.write 가 일부만 쓰는 경우까지 끝까지 쓴다."""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("os.write 가 진행하지 않았다")
        view = view[n:]


def atomic_write(path: Path, data: bytes, fsync=os.fsync) -> None:
    """임시 파일에 쓰고 fsync → os.replace → 디렉터리 fsync. 도중에 죽으면 이전 내용이나 새 내용 하나만 남는다."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | O_BINARY, 0o644)
    try:
        write_all(fd, data)
        fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(path.parent)
