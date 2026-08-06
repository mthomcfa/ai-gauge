"""Behavioral test for how the Claude extractor reads page text.

Executes the real expression from EXTRACTOR_JS in node against a stubbed DOM
where innerText and textContent deliberately differ, the way a real page with
an inline <style> block does. A substring assertion ("does the source say
innerText?") would be satisfied by the word appearing in a comment - which is
exactly the failure mode this repo has already shipped three times.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from aigauge.providers.claude import EXTRACTOR_JS

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to evaluate the extractor JS"
)

# The real page: Claude inlines <style> in the body, so textContent is the CSS
# source concatenated with the visible text, while innerText is just the text.
_CSS = (
    "#static-composer[hidden]{display:none}#static-composer{position:absolute;"
    "z-index:10;left:0;right:0;width:100%}"
)
_VISIBLE = "Plan usage limits Current session All models"


def _body_text_expression() -> str:
    """Pull the bodyText assignment out of the production source."""
    match = re.search(
        r"const bodyText = (\(\(document\.body.*?)\;", EXTRACTOR_JS, re.S
    )
    assert match, "bodyText assignment not found; did EXTRACTOR_JS change shape?"
    return match.group(1)


def _eval_body_text(*, inner: str, text: str) -> str:
    script = f"""
    globalThis.document = {{ body: {{ innerText: {json.dumps(inner)},
                                      textContent: {json.dumps(text)} }} }};
    process.stdout.write(String({_body_text_expression()}));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_body_text_excludes_inline_stylesheet_source():
    body_text = _eval_body_text(inner=_VISIBLE, text=_CSS + " " + _VISIBLE)

    assert "Plan usage limits" in body_text
    assert "static-composer" not in body_text, "CSS leaked into the page text"
    assert "z-index" not in body_text


def test_body_text_has_no_percent_when_the_page_shows_none():
    """The load-bearing consequence.

    Two idle checks require the ABSENCE of a percent sign. CSS is full of
    "width:100%", so reading textContent made both unreachable on any page
    with an inline <style>.
    """
    body_text = _eval_body_text(inner=_VISIBLE, text=_CSS + " " + _VISIBLE)

    assert "%" not in body_text


def test_body_text_falls_back_to_text_content_when_inner_text_is_absent():
    # Not every environment populates innerText (detached documents, some
    # headless paths); the fallback must still yield the text.
    body_text = _eval_body_text(inner="", text=_VISIBLE)

    assert "Plan usage limits" in body_text


def _panel_signal_expression() -> str:
    match = re.search(r"const usagePanelSignals = (/.+?/i)\.test", EXTRACTOR_JS)
    assert match, "usagePanelSignals not found; did EXTRACTOR_JS change shape?"
    return match.group(1)


@pytest.mark.parametrize(
    "heading",
    [
        # Older layout.
        "Plan usage limits Current session All models",
        # Newer gauge/bar layout: heading is "Plan usage" and the seven-day
        # meter is labelled "Weekly", per Claude's own meter table.
        "Plan usage Current session Weekly Opus only Sonnet only",
        # The heading ALONE must be enough. Including "Current session" above
        # let a regex still pinned to "Plan usage limits" pass on that token,
        # so the heading change was not actually being tested.
        "Plan usage Weekly Opus only Sonnet only Claude Design",
    ],
)
def test_both_claude_usage_layouts_are_recognised_as_the_usage_panel(heading):
    script = f"""
    const re = {_panel_signal_expression()};
    process.stdout.write(String(re.test({json.dumps(heading)})));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == "true", f"layout not recognised: {heading!r}"


def test_weekly_row_falls_back_from_all_models_to_weekly():
    # The newer layout has no "All models" row at all.
    assert "readRow('All models') || readRow('Weekly')" in EXTRACTOR_JS
