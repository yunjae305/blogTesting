"""선언한 파이썬 하한을 코드가 실제로 지키는지 확인한다.

`pyproject.toml`은 `requires-python = ">=3.11"`이라고 약속하는데, 정작 코드에는
3.12부터 되는 문법(PEP 695 타입 파라미터, `def f[T](...)`)이 섞여 있었다. CI가 3.13으로
돌아서 아무도 몰랐고, 3.11에서 받으면 import 시점에 SyntaxError로 죽는다 — 실행조차
못 하는 실패라 오래 숨어 있기 좋다(2026-08-04 정리에서 발견).

여기서는 **문법 검사만** 한다. 표준 라이브러리 함수가 언제 생겼는지(예: 3.12의
`itertools.batched`)까지는 잡지 못하므로, 실제로 하한 버전에서 돌려 보는 것을 대신하지는
않는다.
"""

import ast
import tomllib
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = API_ROOT / "pyproject.toml"


def _declared_floor() -> tuple[int, int]:
    """pyproject의 `requires-python`에서 (major, minor)를 읽는다.

    하한을 올리면 이 테스트도 자동으로 따라온다 — 두 값이 어긋날 자리를 만들지 않는다.
    """
    spec = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["requires-python"]
    floor = spec.replace(">=", "").strip()
    major, minor = floor.split(".")[:2]
    return int(major), int(minor)


def _source_files() -> list[Path]:
    return sorted(p for p in (API_ROOT / "app").rglob("*.py") if "__pycache__" not in p.parts)


def test_모든_모듈이_선언한_하한_문법으로_파싱된다():
    floor = _declared_floor()
    failures: list[str] = []

    for path in _source_files():
        try:
            # feature_version은 그 버전의 파서를 흉내 낸다 — 상위 버전 전용 문법이면 여기서 터진다.
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=floor)
        except SyntaxError as error:
            failures.append(f"{path.relative_to(API_ROOT)}:{error.lineno} {error.msg}")

    assert not failures, (
        f"Python {floor[0]}.{floor[1]}에서 파싱되지 않는 파일이 있습니다. "
        f"문법을 낮추거나 pyproject.toml의 requires-python을 올리세요:\n  "
        + "\n  ".join(failures)
    )


def test_검사할_파일이_실제로_있다():
    """rglob이 빈 목록을 돌려주면 위 테스트가 아무것도 검사하지 않고 통과한다."""
    assert len(_source_files()) > 50


def test_상위_버전_문법은_이_검사가_잡는다():
    """검사 자체가 동작하는지 — 3.12 전용 문법을 하한으로 파싱하면 실패해야 한다."""
    with pytest.raises(SyntaxError):
        ast.parse("def f[T](x: T) -> T: return x\n", feature_version=(3, 11))
