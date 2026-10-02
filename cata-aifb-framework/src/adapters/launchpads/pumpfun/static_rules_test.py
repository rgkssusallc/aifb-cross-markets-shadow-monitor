"""Test-enforced chain isolation for the pump.fun package.

Modelled on src/operations/static_rules_test.py. The project rule is prose:

    "Codebase isolation should be maintained at chain level. Launchpads under
    different chains should use different packages. The same method can't be
    used for two different chain launchpads."

Prose does not survive contact with a hurried import, so this asserts it over
the AST. The tradeoff being protected is the one Arc already accepted: some
loop-shape duplication between bridge/main.py and this package's main.py, in
exchange for "changing anything about pump.fun never requires touching
PONS/Arc's production files". That trade is only real while the import graph
actually stays separate.

Run: python src/adapters/launchpads/pumpfun/static_rules_test.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[2]                      # .../src
PKG_MODULE = "src.adapters.launchpads.pumpfun"

sys.path.insert(0, str(SRC.parent))

# What this package is allowed to import. Everything here is either its own
# code or GENUINELY generic infra shared by every chain -- the same allow-list
# Arc's isolation section describes.
ALLOWED_PREFIXES = (
    PKG_MODULE,                      # itself
    "src.adapters.data",             # generic RPC clients, no chain logic
    "src.domain",                    # TokenLaunch / GraduationSignal / chains
    "src.config",                    # env helpers
)

# Packages that must never be imported from here. These are other chains'
# production files; importing one is what the rule exists to prevent.
FORBIDDEN_PREFIXES = (
    "src.bridge",
    "src.adapters.launchpads.pons",
    "src.adapters.launchpads.arc",
    "src.tier0",
    "src.collectors",
    "src.telegram",
)


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def imported_modules(path: Path) -> set[str]:
    """Every module name imported by a file, absolute and relative alike."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import inside the package resolves within it.
                out.add(PKG_MODULE)
            elif node.module:
                out.add(node.module)
    return out


def test_no_other_package_imports_pumpfun():
    """Mirrors test_no_other_package_imports_operations.

    Nothing outside this package may depend on it. The moment something does,
    pump.fun has stopped being removable and the isolation is one-directional
    at best.
    """
    offenders: list[str] = []
    for path in _py_files(SRC):
        if HERE in path.parents or path.parent == HERE:
            continue
        for name in imported_modules(path):
            if name == PKG_MODULE or name.startswith(PKG_MODULE + "."):
                offenders.append(str(path.relative_to(SRC)))
                break
    assert offenders == [], (
        f"these files import the pumpfun package: {offenders}. Nothing "
        "outside it may depend on it.")


def test_pumpfun_imports_only_itself_and_shared_infra():
    """Mirrors test_operations_imports_only_itself_src_config_and_src_labels."""
    offenders: list[tuple[str, str]] = []
    for path in _py_files(HERE):
        for name in imported_modules(path):
            if not name.startswith("src."):
                continue            # stdlib / third-party are not the concern
            if not name.startswith(ALLOWED_PREFIXES):
                offenders.append((str(path.relative_to(SRC)), name))
    assert offenders == [], (
        f"disallowed imports: {offenders}. Allowed src.* prefixes are "
        f"{ALLOWED_PREFIXES}.")


def test_pumpfun_never_imports_another_chains_production_files():
    """The explicit half of the rule, named so a failure reads unambiguously."""
    offenders: list[tuple[str, str]] = []
    for path in _py_files(HERE):
        for name in imported_modules(path):
            if name.startswith(FORBIDDEN_PREFIXES):
                offenders.append((str(path.relative_to(SRC)), name))
    assert offenders == [], (
        f"these imports cross a chain-isolation boundary: {offenders}")


def test_the_entrypoint_is_standalone_like_arc_main():
    """main.py must be runnable without any other chain's bridge.

    Arc's main.py sets this precedent explicitly; the duplication is the
    accepted price of the isolation.
    """
    names = imported_modules(HERE / "main.py")
    bad = [n for n in names if n.startswith(FORBIDDEN_PREFIXES)]
    assert bad == [], f"main.py imports {bad}"
    assert any(n.startswith(PKG_MODULE) for n in names), (
        "main.py should be wiring this package together")


def test_every_module_has_a_docstring_explaining_why_not_just_what():
    """The repo's documentation rule, enforced rather than hoped for.

    "Every code change must be documented in the same change ... explain why,
    not just what; state assumptions and known limitations explicitly,
    inline." A bare module is how that erodes. The length floor is crude but
    it does catch a one-line placeholder.
    """
    thin: list[str] = []
    for path in _py_files(HERE):
        if path.name == "__init__.py":
            continue
        doc = ast.get_docstring(ast.parse(path.read_text())) or ""
        if len(doc.strip()) < 120:
            thin.append(f"{path.name} ({len(doc.strip())} chars)")
    assert thin == [], f"modules lacking a substantive docstring: {thin}"


def test_no_module_hardcodes_a_discriminator_byte_array():
    """Discriminators must be DERIVED, not pasted.

    A pasted byte array cannot be verified by reading it, and a wrong one
    decodes cleanly into a plausible wrong value. constants.py derives every
    one from Anchor's hashing rule and checks it against the vendored IDL;
    the only literals permitted are the four cross-check values in that file's
    own verification block.
    """
    offenders: list[str] = []
    for path in _py_files(HERE):
        if path.name in ("constants.py",) or path.name.endswith("_test.py"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            elts = node.elts
            if len(elts) != 8:
                continue
            if all(isinstance(e, ast.Constant) and isinstance(e.value, int)
                   and 0 <= e.value <= 255 for e in elts):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        f"hardcoded 8-byte arrays found at {offenders}; derive them in "
        "constants.py instead")


def test_the_vendored_idl_records_when_it_was_refreshed():
    """An undated vendored IDL is how a decoder silently rots."""
    from src.adapters.launchpads.pumpfun import constants as C
    assert C.IDL_VENDORED_AT and len(C.IDL_VENDORED_AT) == 10
    assert C.IDL_SOURCE.startswith("https://")
    assert C.IDL_PATH.exists(), "the vendored IDL must ship with the package"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"pass {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
