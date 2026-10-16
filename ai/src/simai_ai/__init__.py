"""simai AI 기능 패키지.

기능별 하위 패키지만 두고, 배포 단위 조립은 ai/services/ 가 맡는다.
이 파일은 하위 패키지를 import 하지 않는다(simai_ai.risk 만 설치·import 해도 다른 기능이 끌려오지 않게).
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("simai-ai")
except PackageNotFoundError:  # 설치하지 않고 소스 경로(PYTHONPATH=ai/src)로 import 한 경우
    __version__ = "0+unknown"
