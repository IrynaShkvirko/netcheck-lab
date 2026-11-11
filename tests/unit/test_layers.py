"""The package layers (README, the code table): each package imports only the ones below it,
so the model stays free of I/O and a backend never reaches into a test run. Checked from
the source, imports inside functions included."""

from __future__ import annotations

import ast

from .intents import ROOT

# package -> the netcheck packages it may import (besides itself)
ALLOWED: dict[str, set[str]] = {
    "model": set(),
    "devices": {"model"},
    "backends": {"model", "devices"},
    "run": {"model", "devices", "backends"},
    "assertions": {"model", "devices", "backends", "run"},
    "faults": {"model", "devices", "backends", "run"},
}
# Deliberate exceptions, each imported inside a function: (importing file, imported module)
EXCEPTIONS = {
    # restoring a snapshot re-applies the faults the ledger recorded; faults drive a backend
    ("netcheck/backends/base.py", "netcheck.faults.catalog"),
}


def _imports(path):  # type: ignore[no-untyped-def]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module


def test_every_module_lives_in_a_layer() -> None:
    top = {p.name for p in (ROOT / "netcheck").iterdir() if p.suffix == ".py"}
    assert top == {"__init__.py", "__main__.py", "pytest_plugin.py"}, (
        "new top-level module: put it in a package (README, the code table)"
    )
    packages = {p.name for p in (ROOT / "netcheck").iterdir() if (p / "__init__.py").exists()}
    assert packages == set(ALLOWED)


def test_each_package_imports_only_the_layers_below_it() -> None:
    wrong = []
    for package, allowed in ALLOWED.items():
        for path in sorted((ROOT / "netcheck" / package).rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            for module in _imports(path):
                parts = module.split(".")
                if parts[0] != "netcheck" or len(parts) < 2 or parts[1] == package:
                    continue
                if parts[1] not in allowed and (rel, module) not in EXCEPTIONS:
                    wrong.append(f"{rel} imports {module}")
    assert not wrong, "imports against the layers:\n" + "\n".join(wrong)


def test_the_exceptions_are_still_needed_and_imported_late() -> None:
    for rel, module in EXCEPTIONS:
        tree = ast.parse((ROOT / rel).read_text())
        top_level = {n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
        assert module in set(_imports(ROOT / rel)), f"{rel} no longer imports {module}"
        assert module not in top_level, f"{rel} imports {module} at module level"
