"""Номер шага не живёт в пользовательском тексте.

25.09 порядок шагов 12–15 поменялся, и все фразы вида «появится на шаге 13»
разом стали врать. Чинить это заменой числа на другое число бессмысленно:
план поменяется снова. Номер шага — внутренняя единица плана, экран говорит
о том, чего ещё нет, своими словами.

Тест смотрит на строковые литералы, потому что тексты собирает сервер
(Архитектура ч.2 §1.3): ровно эти строки и уезжают на экран. Комментарии
и докстроки он не трогает — там номер прошедшего шага это запись истории,
а не обещание.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEP_NUMBER = re.compile(r"шаг[аеиу]?\s+\d")

# Комментарий на фронте: там номер прошедшего шага — запись истории, а не
# обещание экрана. Блочные комментарии бывают многострочными, в том числе
# в виде {/* … */} внутри разметки, поэтому нужен не шаблон строки, а разбор.
LINE_COMMENT = re.compile(r"//.*")


def _without_comments(text: str) -> list[tuple[int, str]]:
    """Строки файла без комментариев. Номера строк сохраняются."""
    out: list[tuple[int, str]] = []
    in_block = False
    for n, line in enumerate(text.splitlines(), 1):
        rest = line
        kept = ""
        while rest:
            if in_block:
                end = rest.find("*/")
                if end == -1:
                    rest = ""
                else:
                    rest = rest[end + 2 :]
                    in_block = False
                continue
            start = rest.find("/*")
            if start == -1:
                kept += rest
                rest = ""
            else:
                kept += rest[:start]
                rest = rest[start + 2 :]
                in_block = True
        out.append((n, LINE_COMMENT.sub("", kept)))
    return out


def _python_literals(path: Path) -> list[tuple[int, str]]:
    """Все строковые литералы файла, кроме докстрок."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.append((node.lineno, node.value))
    return found


def test_backend_texts_have_no_step_numbers() -> None:
    bad = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for line, value in _python_literals(path):
            if STEP_NUMBER.search(value):
                bad.append(f"{path.relative_to(ROOT)}:{line}: {value.strip()[:70]}")
    assert not bad, "номер шага в тексте:\n" + "\n".join(bad)


def test_frontend_texts_have_no_step_numbers() -> None:
    bad = []
    for pattern in ("*.tsx", "*.ts", "*.css", "*.html"):
        for path in sorted((ROOT / "web" / "src").rglob(pattern)):
            source = path.read_text(encoding="utf-8")
            for n, line in _without_comments(source):
                if STEP_NUMBER.search(line):
                    bad.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:70]}")
    assert not bad, "номер шага в тексте:\n" + "\n".join(bad)
