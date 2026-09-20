"""Границы модулей — ограничение, а не договорённость.

Модуль не имеет права импортировать другой модуль. Общее — только
eds.contracts и eds.platform. Тест обходит AST всех файлов и падает на нарушении.
Это тот самый шов, по которому монолит однажды разрежут (Архитектура ч.1 §1).
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "eds"
MODULES_DIR = SRC / "modules"


def _module_of(path: pathlib.Path) -> str | None:
    try:
        rel = path.relative_to(MODULES_DIR)
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def _imported_names(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for name in _imported_names(tree):
        parts = name.split(".")
        if len(parts) >= 3 and parts[0] == "eds" and parts[1] == "modules":
            found.add(parts[2])
    return found


def test_modules_do_not_import_each_other() -> None:
    violations: list[str] = []
    for path in MODULES_DIR.rglob("*.py"):
        own = _module_of(path)
        if own is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imported_modules(tree):
            if imported != own:
                violations.append(f"{path.relative_to(SRC)} импортирует eds.modules.{imported}")
    assert not violations, "нарушены границы модулей:\n" + "\n".join(violations)


def test_contracts_import_nothing_from_modules() -> None:
    """Контракты не знают о модулях: иначе шов перестанет быть швом."""
    violations: list[str] = []
    for path in (SRC / "contracts").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _imported_modules(tree):
            violations.append(str(path.relative_to(SRC)))
    assert not violations, "контракты импортируют модули: " + ", ".join(violations)


def test_modules_do_not_import_orchestration() -> None:
    """Зависимости идут только в одну сторону: app → modules, и никогда обратно.

    Обратный импорт создал бы цикл и, что важнее, сделал бы модуль зависимым
    от способа, которым его сегодня вызывают.
    """
    violations: list[str] = []
    for path in MODULES_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_names(tree):
            if name == "eds.app" or name.startswith("eds.app."):
                violations.append(f"{path.relative_to(SRC)} импортирует {name}")
    assert not violations, "модуль зависит от оркестрации:\n" + "\n".join(violations)


def test_platform_does_not_import_modules() -> None:
    """Платформа ничего не знает о модулях: иначе она перестаёт быть платформой."""
    violations: list[str] = []
    for path in (SRC / "platform").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_names(tree):
            if name.startswith("eds.modules") or name.startswith("eds.app"):
                violations.append(f"{path.relative_to(SRC)} импортирует {name}")
    assert not violations, "платформа зависит от модулей:\n" + "\n".join(violations)
