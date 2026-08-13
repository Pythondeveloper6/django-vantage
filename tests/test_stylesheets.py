"""Guards on the stylesheets themselves.

Both of these are properties no feature test would notice losing. RTL breaks silently:
the page still renders, just mirrored wrongly, and nobody reading English sees it. A
hard-coded colour breaks silently too — it looks right in whichever theme the author
had open and wrong in the other, and it makes ``BRAND_COLOR`` a partial promise.
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src/vantage/static/vantage"
STYLES = sorted(STATIC.glob("*.css"))

#: `margin-left`, `padding-right`, `left:`, and friends. Logical equivalents
#: (`margin-inline-start`, `inset-inline-start`) mirror under `dir="rtl"`; these do not.
PHYSICAL = re.compile(
    r"(?<![\w-])(?:margin|padding|border)-(?:left|right)\s*:"
    r"|(?<![\w-])(?:left|right)\s*:\s*(?!.*var\()"
)

#: Hex, rgb() and hsl() literals. Every colour must come from a token so the theme
#: layer is the single point of control.
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|(?<![\w-])(?:rgba?|hsla?)\s*\(")

#: `oklch(0 0 0 / …)` is how a neutral shadow is written, and a shadow that follows
#: the brand hue looks wrong. Those are the only bare colours allowed.
SHADOW_NEUTRAL = re.compile(r"oklch\(\s*[01](?:\.\d+)?\s+0\s+0")


def significant_lines(path: Path):
    """Lines that are not comments, and not deliberately direction-specific."""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("/*", "*", "//")):
            continue
        if 'dir="rtl"' in line or 'dir="ltr"' in line or ":dir(" in line:
            continue
        yield number, line


def test_there_are_stylesheets_to_check():
    """A path typo would otherwise make every test below vacuously pass."""
    assert len(STYLES) >= 5


@pytest.mark.parametrize("path", STYLES, ids=lambda p: p.name)
def test_no_physical_directions(path):
    """RTL works by setting dir='rtl' and nothing else — but only if nothing is
    pinned to left or right."""
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in significant_lines(path)
        if PHYSICAL.search(line)
    ]
    assert not offenders, "use logical properties (inline-start/end):\n" + "\n".join(offenders)


@pytest.mark.parametrize("path", STYLES, ids=lambda p: p.name)
def test_no_hard_coded_colours(path):
    """Every colour reads from a token, or BRAND_COLOR and dark mode are half-promises."""
    offenders = []
    for number, line in significant_lines(path):
        if not LITERAL_COLOUR.search(line):
            continue
        offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "use a CSS custom property:\n" + "\n".join(offenders)


@pytest.mark.parametrize("path", STYLES, ids=lambda p: p.name)
def test_bare_oklch_is_only_used_for_neutral_shadows(path):
    """A shadow that follows the brand hue looks wrong, so those stay neutral —
    but nothing else may bypass the token layer."""
    for number, line in significant_lines(path):
        for match in re.finditer(r"oklch\([^)]*\)", line):
            if "var(" in match.group() or SHADOW_NEUTRAL.match(match.group()):
                continue
            # Token definitions themselves live in theme.py, not in these files.
            assert "--" in line.split(":")[0], f"{path.name}:{number}: {line.strip()}"


@pytest.mark.parametrize("path", STYLES, ids=lambda p: p.name)
def test_animations_respect_reduced_motion(path):
    """An admin panel is a tool; motion should never be mandatory."""
    text = path.read_text(encoding="utf-8")
    if "animation:" not in text and "@keyframes" not in text:
        pytest.skip("no animation in this stylesheet")
    base = (STATIC / "vantage.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in text or "prefers-reduced-motion" in base


# ── template comment hygiene ─────────────────────────────────────────────────

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "src/vantage/templates"
TEMPLATES = sorted(TEMPLATE_ROOT.rglob("*.html"))

MULTILINE_COMMENT = re.compile(r"\{#[^#]*?\n", re.DOTALL)


def test_there_are_templates_to_check():
    assert len(TEMPLATES) >= 10


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_no_multiline_hash_comments(path):
    """Django's ``{# #}`` is SINGLE-LINE ONLY.

    Spanning two lines does not comment anything out — the text renders verbatim into
    the page. It has escaped into the running UI three times in this project, once
    across the whole header, because it looks exactly like a comment while reading the
    template. Use ``{% comment %}`` for anything longer than a line.
    """
    text = path.read_text(encoding="utf-8")
    hits = MULTILINE_COMMENT.findall(text)
    assert not hits, (
        f"{path.name}: {len(hits)} multi-line {{# #}} comment(s) — these render as "
        "visible text. Use {% comment %} … {% endcomment %}."
    )
