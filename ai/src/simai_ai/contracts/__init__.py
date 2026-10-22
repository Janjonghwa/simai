"""contracts/ JSON Schema 에서 생성한 타입.

생성 코드(models.py, pydantic v2)이므로 손으로 고치지 않는다. 스키마를 고친 뒤 ai/ 에서
`uv run python ../contracts/scripts/generate_python_models.py` 로 다시 만든다(--check 는 커밋과 같은지만 본다).
models.py 는 pydantic 이 있어야 import 된다(dev 그룹). 이 __init__ 은 models 를 불러오지 않는다.
simai_ai.risk 가 import 해도 되는 유일한 simai_ai 하위 패키지다(기본 설치에 pydantic 이 없으니 risk 는 아직 models 를 쓰지 않는다).
"""
