"""LaTeX math in agent replies renders instead of showing raw ``$``.

QTextDocument has no math support, so ``$...$`` / ``$$...$$`` spans are
rendered to inline PNGs with matplotlib mathtext (already a dependency).

The interesting cases are all the ones where math should NOT be rendered --
currency, shell variables, code spans -- and the failure mode where an
unparseable formula gets silently swallowed instead of shown as text.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import html as _html  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PyQt6")

from src.gui.agent_dock import (  # noqa: E402
    _MATH_CACHE,
    _markdown_to_html,
)

IMG = '<img src="data:image/png'


def _n_images(text: str) -> int:
    return _markdown_to_html(text).count(IMG)


def _visible_text(source: str) -> str:
    """Rendered text with tags stripped and entities unescaped.

    Qt escapes `<` to `&lt;` inside code spans, which is correct -- compare
    against the unescaped form so the assertion is about content, not
    serialization.
    """
    out = _markdown_to_html(source)
    return _html.unescape(re.sub(r"<[^>]+>", "", out)).replace(" ", " ")


def test_inline_math_renders() -> None:
    assert _n_images(r"The area is $A = \frac{i_p}{2.69}$ cm^2.") == 1


def test_display_math_renders() -> None:
    assert _n_images(r"$$i_p = 2.69 \times 10^5 n^{3/2}$$") == 1


def test_display_math_not_parsed_as_two_inline_spans() -> None:
    """$$...$$ must match as one block, not as two empty inline spans."""
    assert _n_images(r"$$a + b$$") == 1


@pytest.mark.parametrize(
    "text",
    [
        "It cost $5 and $10 total.",          # currency
        "Set $HOME and $PATH in the shell.",  # shell variables
        "Plain **bold** text, no math.",      # no math at all
    ],
)
def test_non_math_dollar_signs_are_left_alone(text: str) -> None:
    assert _n_images(text) == 0


def test_math_inside_inline_code_is_not_rendered() -> None:
    assert _n_images("Use the var `$cr$` here.") == 0


def test_math_inside_fenced_code_is_not_rendered() -> None:
    assert _n_images("```\nprice = $5 and $10\n```") == 0


@pytest.mark.parametrize(
    "source, must_survive",
    [
        ("Use the var `$cr$` here.", "$cr$"),
        ("Check `if current < threshold:` now.", "current < threshold"),
        ("```\nprice = $5 and $10\n```", "price = $5 and $10"),
        ("Both `$a$` and `$b$` stay.", "$b$"),
    ],
)
def test_code_span_text_survives_verbatim(source, must_survive) -> None:
    """Code content must reach the user intact.

    Asserting only "no <img> leaked" is not enough and hid a real bug: an
    earlier implementation swapped code spans out for NUL-delimited
    placeholders, but QTextDocument.setMarkdown rewrites NUL to U+FFFD, so
    restoration silently no-opped and EVERY code span rendered as garbage
    like "�CODE0�". That produces no <img> either, so the
    image-count assertions stayed green through total content loss.
    """
    text = _visible_text(source)
    assert must_survive in text, f"code content lost: {text!r}"
    assert "CODE0" not in text, "placeholder token leaked into output"
    assert "�" not in text, "U+FFFD replacement char leaked into output"


def test_code_span_with_angle_brackets_is_escaped_not_injected() -> None:
    """`<` inside code must be escaped, not spliced into the HTML raw."""
    out = _markdown_to_html("Check `if a < b:` now.")
    assert "&lt;" in out, "angle bracket was not escaped"


@pytest.mark.parametrize(
    "source",
    [
        "Use $$ for display math, e.g. $$x=1$$ ok",
        "this is $$ moderately priced $$ gear",
    ],
)
def test_stray_double_dollar_does_not_swallow_a_paragraph(source) -> None:
    """A stray `$$` pair must not render prose as a formula.

    Non-greedy display matching finds the NEAREST following `$$`, so two
    literal `$$` in one message can bracket ordinary prose. Blocking the
    match at a blank line bounds the damage; this test pins the behaviour
    so it cannot silently get worse.
    """
    text = _visible_text(source)
    # The prose between the delimiters must still be readable text.
    assert "priced" in text or "display math" in text


def test_unparseable_latex_is_preserved_verbatim() -> None:
    """A bad formula must stay visible as text, never vanish.

    Silently dropping it would lose content the agent meant to convey.
    """
    src = r"broken $\frac{unclosed$ stays"
    out = _markdown_to_html(src)
    assert IMG not in out
    assert "unclosed" in out


def test_repeated_formula_is_cached() -> None:
    """The formula is rasterized ONCE, not once per re-render.

    Asserting `_markdown_to_html(src) == _markdown_to_html(src)` does not
    test caching: mathtext is deterministic, so that holds with the cache
    ripped out entirely. Spy on the renderer instead -- bubbles re-render
    on every streaming delta, so a cache regression is a real perf bug.
    """
    import src.gui.agent_dock as dock

    formula = r"$E_{1/2} + \Delta_{cache}$"
    _MATH_CACHE.clear()
    calls = {"n": 0}
    real = dock._render_math_png

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    dock._render_math_png = _counting
    try:
        _markdown_to_html(formula)
        _markdown_to_html(formula)
        _markdown_to_html(formula)
    finally:
        dock._render_math_png = real

    assert calls["n"] == 3, "renderer should be consulted on every pass"
    # ...but only the first should have done real rasterization work.
    assert len(_MATH_CACHE) == 1, (
        f"expected one cached entry, got {len(_MATH_CACHE)}"
    )


def test_cache_is_populated_on_first_render() -> None:
    """Ground truth: the entry actually lands in the cache."""
    _MATH_CACHE.clear()
    _markdown_to_html(r"$\alpha_{fresh}$")
    assert len(_MATH_CACHE) == 1


def test_markdown_still_works_alongside_math() -> None:
    out = _markdown_to_html(r"**bold** and $x^2$ together")
    assert IMG in out
    assert "bold" in out
