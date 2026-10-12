"""The words Lifeboat shows: a count agrees with its noun ("1 file", not "1 files" or "1 file(s)"),
American spelling, and the ellipsis character. Checked in every string in the source, so a new
message can't slip through."""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from lifeboat.events import Progress
from lifeboat.util import plural

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "lifeboat"

# Nouns that follow a count. "{n} more deleted files" counts too, so a few words may come between.
COUNTED = ("files|folders|volumes|drives|devices|sectors|partitions|records|nodes|errors|warnings|problems|"
           "seconds|minutes|hours|days|parts|entries|copies|snapshots|blocks|reads|times|matches|results|"
           "candidates|checks|changes|fragments|events|passes|attempts|items|bytes|dialogs|keys|passwords")
BETWEEN = "more|deleted|damaged|selected|ticked|unfinished|lost|bad|reallocated|pending|uncorrectable|" \
          "unreadable|partition|other|new|recovered|skipped|failed|found"
COUNT_THEN_NOUN = re.compile(rf"^\s+(?:(?:{BETWEEN})\s+){{0,2}}({COUNTED})\b")

BRITISH = re.compile(
    r"\b(colour|behaviour|favourite|licence|cancell(?:ed|ing)|catalogue|centre|"
    r"centred|grey|organis|recognis|analys(?:e|ed|ing)\b|customis|optimis|initialis|summaris|prioritis|authoris|"
    r"normalis|synchronis|visualis|utilis|finalis|categoris|minimis|maximis|labelled|modelling|travelling|"
    r"programme|defence|judgement|whilst|amongst|learnt)", re.I)


def _strings() -> list[tuple[str, int, ast.AST, str]]:
    """Every string literal and f-string in the package, docstrings left out: (file, line, node, source)."""
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        in_fstring = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
        name = path.relative_to(SOURCE.parent).as_posix()   # "lifeboat/ui/x.py" on Windows too
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                found.append((name, node.lineno, node, text))
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                  and id(node) not in in_fstring):
                found.append((name, node.lineno, node, text))
    return found


STRINGS = _strings()


def _text(node: ast.AST) -> str:
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in node.values)  # type: ignore[misc]
    return node.value  # type: ignore[attr-defined,no-any-return]


def test_plural():
    assert plural(1, "file") == "1 file"
    assert plural(0, "file") == "0 files"
    assert plural(2048, "file") == "2,048 files"
    assert plural(1, "copy", "copies") == "1 copy"
    assert plural(3, "copy", "copies") == "3 copies"


def test_progress_says_what_it_counts():
    """Sizes for bytes, and the count with its noun for anything else ("50,000 of 120,000 records")."""
    assert Progress("Copying", 3 << 30, 10 << 30).amount() == "3.00 GB of 10.0 GB"
    assert Progress("Finding where the files are", 0, 1, unit="file").amount() == "0 of 1 file"
    assert Progress("Rebuilding folders", 50000, 120000, unit="record").amount() == "50,000 of 120,000 records"


def test_a_count_agrees_with_its_noun():
    """``f"{n} files"`` says "1 files" when n is 1: such counts go through ``plural()``."""
    wrong = []
    for name, line, node, text in STRINGS:
        if not isinstance(node, ast.JoinedStr):
            continue
        for before, after in zip(node.values, node.values[1:]):
            if isinstance(before, ast.FormattedValue) and isinstance(after, ast.Constant):
                spec = "".join(str(part.value) for part in getattr(before.format_spec, "values", [])
                               if isinstance(part, ast.Constant))
                if re.search(r"\.[1-9]f", spec):
                    continue   # "1.0 seconds": a decimal takes the plural
                match = COUNT_THEN_NOUN.match(str(after.value))
                if match:
                    wrong.append(f"{name}:{line}: {{{ast.get_source_segment(text, before.value)}}}{after.value[:40]!r}")
    for name, line, node, _text_ in STRINGS:   # "%d files" and "{} files" for % and .format()
        if isinstance(node, ast.Constant) and re.search(rf"(%[-0-9.,]*d|\{{[^}}]*\}})\s+({COUNTED})\b", node.value):
            wrong.append(f"{name}:{line}: {node.value[:60]!r}")
    assert not wrong, "use plural() for these counts:\n" + "\n".join(wrong)


def test_no_placeholder_plurals():
    """"file(s)" reads like a form letter: say "1 file" or "2 files"."""
    wrong = [f"{name}:{line}: {_text(node)[:70]!r}" for name, line, node, _ in STRINGS if "(s)" in _text(node)]
    assert not wrong, "\n".join(wrong)


def test_american_spelling():
    wrong = [f"{name}:{line}: {_text(node)[:70]!r}" for name, line, node, _ in STRINGS
             if BRITISH.search(_text(node))]
    assert not wrong, "\n".join(wrong)


def test_the_ellipsis_is_one_character():
    """"…" not "...", except in the command line tool (a Windows console may not show "…")."""
    wrong = [f"{name}:{line}: {_text(node)[:70]!r}" for name, line, node, _ in STRINGS
             if "..." in _text(node) and not name.endswith("cli.py")]
    assert not wrong, "\n".join(wrong)


def test_features_are_named_as_on_their_buttons():
    """"Deep scan" and "Drive check", as the buttons and windows call them, not "Deep Scan"."""
    wrong = [f"{name}:{line}: {_text(node)[:70]!r}" for name, line, node, _ in STRINGS
             if re.search(r"Deep Scan|Drive Check|Disk Image\b", _text(node))
             and not (name.endswith("device/macos.py") and _text(node) == "Disk Image")]  # macOS's own name
    assert not wrong, "\n".join(wrong)


def test_no_hyphen_for_a_dash():
    """A sentence breaks with a colon or "—", not " - ". Folder names keep " - " (no "—" in file names)."""
    wrong = [f"{name}:{line}: {_text(node)[:70]!r}" for name, line, node, _ in STRINGS
             if " - " in _text(node) and not name.endswith("cli.py")
             and not (name.endswith("scan/scanner.py") and _text(node) == " - {}")]   # "Partition 2 - Data"
    assert not wrong, "\n".join(wrong)


def test_tooltips_end_alike():
    """A one-sentence tooltip has no full stop, like a button's; longer ones have them all."""
    wrong = []
    for name, line, node, text in STRINGS:
        call = f"{text.splitlines()[line - 1]}"
        tip = _text(node).rstrip()
        if "setToolTip(" in call and ". " not in tip and tip.endswith(".") and not tip.endswith("..."):
            wrong.append(f"{name}:{line}: {tip[:70]!r}")
    assert not wrong, "\n".join(wrong)


@pytest.mark.parametrize("message", ["1 files", "1 volumes", "1 sectors"])
def test_the_checks_catch_what_they_should(message):
    count, noun = message.split()
    assert COUNT_THEN_NOUN.match(f" {noun}")
    assert COUNT_THEN_NOUN.match(f" more deleted {noun}")
    assert not COUNT_THEN_NOUN.match(" is ready")
