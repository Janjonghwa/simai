"""단일 작성자 잠금 (명세 3.2 저널 계약: 단일 작성자의 추가 전용 파일).

저널 디렉터리마다 작성자 하나만 둔다. Pod 교체가 겹쳐 두 Tier 0 가 같은 저널에 쓰면 journal_offset 이 갈라지므로,
쓰기 모드로 열 때 잠금 파일에 배타 잠금을 걸고 실패하면 열지 않는다. 잠금은 운영체제가 쥐므로 프로세스가 강제
종료돼도 풀린다(POSIX flock, Windows msvcrt.locking). RunPod Network Volume 은 파일 잠금을 지원한다(16장: 글로벌
볼륨을 뺀 이유가 잠금·원자적 rename 부재다).
"""

import os
import sys
from pathlib import Path


class WriterLockError(RuntimeError):
    """다른 작성자가 이미 저널을 쥐고 있다."""


class WriterLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            if sys.platform == "win32":
                import msvcrt

                # 첫 바이트를 잠근다. 파일이 비어 있어도 잠글 수 있다. LK_NBLCK 는 기다리지 않는다.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise WriterLockError(f"저널 작성자 잠금을 얻지 못했다(다른 작성자가 쓰는 중): {self._path}") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # 닫으면 어차피 풀린다
        finally:
            os.close(fd)
