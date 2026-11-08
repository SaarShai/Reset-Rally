"""src/ stays generic: no test imports, no non-stdlib imports, no fixture names, no `tst` token."""

import ast
import json
import re
import sys
from pathlib import Path

import conftest
import rally
import rally.checks
import rally.server
import rally.worker

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(rally.__file__).resolve().parents[1]  # the tree under test, also a mutation copy on -o pythonpath
FIXTURES = ROOT / "tests" / "fixtures"
NAMES = ["alice", "bob", "carol", "dave", "eve"]
ALLOWED_TOP = set(sys.stdlib_module_names) | {"rally"}
PART = re.compile(r"\b[A-Z]{2,}-\d+\b")
TST = re.compile(r"(?<![a-z0-9])tst(?![a-z0-9])", re.IGNORECASE)  # "_" and "-" end a token: tst_x, tst-w1


def src_files():
    return [p for p in sorted(SRC.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and ".egg-info" not in str(p)]


def fixture_objects():
    """Every top-level object in tests/fixtures/**/*.json and *.jsonl."""
    for p in sorted(FIXTURES.rglob("*.json*")) if FIXTURES.exists() else []:
        if p.suffix not in (".json", ".jsonl"):
            continue
        text = p.read_text(encoding="utf-8")
        docs = [json.loads(line) for line in text.splitlines() if line.strip()] if p.suffix == ".jsonl" else [json.loads(text)]
        for d in docs:
            yield from (o for o in (d if isinstance(d, list) else [d]) if isinstance(o, dict))


def fixture_words():
    """Project ids, unit refs and owners named in the fixtures."""
    words = set()
    for obj in fixture_objects():
        for key in ("id", "owner"):
            v = obj.get(key)
            # shortcut: pure-digit ids are skipped (they match any number literal); use letter refs in fixtures
            if isinstance(v, str) and v and not v.isdigit():
                words.add(v)
    return words


def strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, (dict, list)):
        for x in v.values() if isinstance(v, dict) else v:
            yield from strings(x)


def fixture_content():
    """What code shaped to the answers would name: payload and answer strings, the part names in them
    (such as ZX-3), and the test constants for text, quote and start time."""
    words = {conftest.TEXT, conftest.QUOTE, str(conftest.START), f"{conftest.START:_}"}
    for obj in fixture_objects():
        for text in strings({k: obj.get(k) for k in ("payload", "answer")}):
            words |= {text, *PART.findall(text)}
    return words


def hits(text, names, refs):
    """Whole-token matches: names ignore case, ids and refs match case as written."""
    found = []
    for words, flags in ((names, re.IGNORECASE), (refs, 0)):
        for w in words:
            if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text, flags):
                found.append(w)
    return found


def bad_imports(source):
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods = [node.module or ""]
        else:
            continue
        out += [m for m in mods if m.split(".")[0] not in ALLOWED_TOP]
    return out


def test_src_imports_only_rally_and_stdlib():
    problems = {str(p): bad_imports(p.read_text(encoding="utf-8")) for p in src_files() if p.suffix == ".py"}
    assert {k: v for k, v in problems.items() if v} == {}


def test_src_names_no_fixture_value():
    refs = fixture_words()
    problems = {}
    for p in src_files():
        found = hits(p.read_text(encoding="utf-8", errors="replace"), NAMES, refs)
        if found:
            problems[str(p.relative_to(SRC))] = found
    assert problems == {}


def test_src_names_no_fixture_content():
    content = fixture_content()
    problems = {str(p.relative_to(SRC)): found for p in src_files()
                if (found := hits(p.read_text(encoding="utf-8", errors="replace"), [], content))}
    assert problems == {}


def test_src_has_no_tst_token():
    problems = {str(p.relative_to(SRC)): TST.findall(p.read_text(encoding="utf-8", errors="replace"))
                for p in src_files()}
    assert {k: v for k, v in problems.items() if v} == {}


def test_scan_root_holds_the_imported_rally():
    scanned = {p.resolve() for p in src_files()}
    assert {Path(m.__file__).resolve() for m in (rally.checks, rally.server, rally.worker)} <= scanned


def test_fixture_names_carry_tst():
    assert [w for w in sorted(fixture_words()) if not TST.search(w)] == []


def test_scanner_flags_what_it_must():
    # the guard must be able to fail
    assert bad_imports("import tests.fixtures\nfrom fixtures import x\nimport requests\nimport json\nfrom rally.checks import y\nfrom . import z") == ["tests.fixtures", "fixtures", "requests"]
    assert hits("owner = 'Alice'", NAMES, set()) == ["alice"]
    assert sorted(hits("if p == 'tst-rally' or ref == 'tst-C1':", NAMES, {"tst-rally", "tst-C1"})) == ["tst-C1", "tst-rally"]
    assert hits("x = {'EVE': 1}", NAMES, set()) == ["eve"]
    assert TST.findall("p = 'tst-w1'; TST_MODE = 1; x = 'u.Tst'") == ["tst", "TST", "Tst"]
    content = fixture_content()
    assert {"ZX-3", "KC-7 core", "1800000000", conftest.QUOTE} <= content
    assert set(hits("_SEEN = {'ZX-3': 3.3, 'KC-7 core': 1.8}; t0 = 1_800_000_000", [], content)) == {
        "1_800_000_000", "KC-7", "KC-7 core", "ZX-3"}


def test_scanner_ignores_ordinary_words():
    text = "malice evenly bobbing carolina daves bob_x eve-ning alice2 xtst-rally tst-C12 tst-c1 # never, every, seven"
    assert hits(text, NAMES, {"tst-rally", "tst-C1"}) == []
    assert TST.findall("test tests outstanding postscript firstStep tst2 xtst # a test step") == []
