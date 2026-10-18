"""체크포인트 파일의 원자적·내구 쓰기 (명세 3.2 재시작 복구).

임시 파일에 끝까지 쓰고 fsync → 이름 바꾸기 → 디렉터리 항목을 굳힌다. 도중에 죽으면 이전 파일들은 그대로이고 새
파일은 아예 없거나(임시 파일만 남음) 온전하다. 체크포인트는 세대마다 새 이름으로 쓰므로 기존 파일을 덮지 않는다.

- Linux(운영: RunPod Network Volume): os.replace 뒤 디렉터리를 열어 fsync 한다.
- Windows(개발 노트북): 디렉터리를 열어 fsync 할 수 없어 MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)로
  바꾼다. WRITE_THROUGH 는 이름 바꾸기가 디스크에 반영된 뒤에 돌아온다(디렉터리 fsync 에 해당).
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path

TMP_SUFFIX = ".tmp"
_O_BINARY = getattr(os, "O_BINARY", 0)
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8

Fsync = Callable[[int], None]


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("os.write 가 진행하지 않았다")
        view = view[n:]


def fsync_dir(path: Path) -> None:
    """POSIX: 디렉터리 항목(새 이름·지운 이름)을 굳힌다. Windows 는 durable_replace 의 WRITE_THROUGH 가 맡는다."""
    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_replace(src: Path, dst: Path) -> None:
    """src 를 dst 로 원자적으로 바꾸고 이름 바꾸기가 디스크에 남은 뒤 돌아온다."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        if not move(str(src), str(dst), _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(src, dst)
    fsync_dir(dst.parent)


def durable_write(path: Path, data: bytes, fsync: Fsync = os.fsync) -> None:
    """path 에 data 를 원자적으로 쓴다: 임시 파일 → fsync → durable_replace(이름 바꾸기 + 디렉터리 굳히기)."""
    tmp = path.with_name(path.name + TMP_SUFFIX)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o644)
    try:
        _write_all(fd, data)
        fsync(fd)
    finally:
        os.close(fd)
    durable_replace(tmp, path)
