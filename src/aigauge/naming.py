"""Every provider display name the user ever sees, in one table.

Before this module the same six names were written out in nine places -
``config.display_name_for_account``'s dict, four ``ensure_tile`` literals and a
second dict in ``app.py``, ``widget.COMPACT_DISPLAY_NAMES``,
``menubar.PROVIDER_LABELS``, and a scattering of ``settings_dialog`` strings -
so "what is this provider called" had no answer, only a majority. Renaming one
surface left the others behind, which is how the panel came to say ``Copilot``
while Settings said ``GitHub Copilot``.

Three tiers, because the surfaces have three different amounts of room:

* :func:`full` - ``Company · Surface``. Tile headers, dialog titles, Settings
  checkboxes, group boxes and tabs. Anything with a line to itself.
* :func:`compact` - the product alone. Collapsed summary chips, the tray
  tooltip, anywhere a repeated company name would cost a row.
* :func:`abbrev` - two letters, for the macOS menu bar, where the native item
  is variable-width text and a full name would double it per provider.

The separator is U+00B7 with a space either side, matching the one label that
already shipped this way (``Microsoft · Azure``). Company first: the company is
what the user is deciding about when they look at a bill.

**This module imports nothing from the app.** ``config`` imports it, never the
reverse, so the table can be read - and tested - without pydantic, keyring or a
config file. It is also why the names are keyed by *kind* (``claude``) rather
than by account id (``claude-3f9a…``): :func:`kind_of` does that mapping, and
it is the same rule ``config.account_kind`` applies without needing a ``Config``
to apply it.

Ids are not labels and never become them. Every ``UsageSnapshot.provider``,
config key, profile directory, keyring entry and log line stays an id; nothing
in this module is written to disk or matched against stored data. Renaming a
label here is a presentation change and nothing else - see ``history.py``,
whose keys are ``id::metric-label``.
"""
from __future__ import annotations

# U+00B7 MIDDLE DOT with a space either side. Note that ``widget._MetricRow``
# splits a *metric* label on this same string to make a two-column note row, so
# a company-prefixed name must never be routed through a metric label.
SEPARATOR = " · "

# The company behind each surface, or None where the product is the company.
#
# Copilot is GitHub's, not Microsoft's: the credential is a GitHub PAT, the
# host is api.github.com, and the product carries GitHub's name. Microsoft owns
# GitHub, which is why Settings used to file Copilot under a Microsoft tab -
# but the tile names the vendor whose page the number came from.
COMPANY: dict[str, str | None] = {
    "claude": "Anthropic",
    "codex": "OpenAI",
    "opencode_go": None,
    "copilot": "GitHub",
    "azure": "Microsoft",
    "openrouter": None,
}

# The product name as its own vendor writes it.
#
# Codex reads "ChatGPT + Codex" in full because that is what the one
# subscription covers and what the sign-in page is: the usage the tile shows is
# a ChatGPT plan's, and a user looking for "why is my ChatGPT limit here" gets
# an answer from the header rather than from the docs.
SURFACE: dict[str, str] = {
    "claude": "Claude",
    "codex": "ChatGPT + Codex",
    "opencode_go": "OpenCode",
    "copilot": "Copilot",
    "azure": "Azure",
    "openrouter": "OpenRouter",
}

# What a surface is called where there is no room for the company or for the
# product's full name. Deliberately a separate table rather than a rule over
# SURFACE: "ChatGPT + Codex" shortens to "Codex", which no rule would guess.
COMPACT: dict[str, str] = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode_go": "OpenCode",
    "copilot": "Copilot",
    "azure": "Azure",
    "openrouter": "OpenRouter",
}

# Two letters for the macOS menu bar. Unchanged from the day they shipped: they
# are what a long-time user reads without looking, and the menu bar is the one
# surface where a wider label costs another app its place.
ABBREV: dict[str, str] = {
    "claude": "Cl",
    "codex": "Cx",
    "opencode_go": "Go",
    "copilot": "Cp",
    "azure": "Az",
    "openrouter": "OR",
}

# Every kind this module knows, in the order the panel and Settings show them.
# Anthropic and OpenAI first because they are the subscriptions being watched;
# Microsoft and GitHub are two vendors, not one, so Copilot is no longer filed
# behind Azure.
KINDS: tuple[str, ...] = (
    "claude",
    "codex",
    "opencode_go",
    "azure",
    "copilot",
    "openrouter",
)


def kind_of(provider_id: str) -> str:
    """The family a provider or account id belongs to.

    ``claude-3f9a12cd`` -> ``claude``; everything else is its own family. Ids
    are generated with a ``<kind>-<hex>`` shape (``config.generate_browser_account_id``),
    so the prefix is the kind and nothing else has to be consulted to find it.
    An id from no known family comes back unchanged, which is what lets the
    label functions fall back on it rather than raise.
    """
    text = str(provider_id or "")
    for kind in ("claude", "codex"):
        if text == kind or text.startswith(f"{kind}-"):
            return kind
    return text


def full(kind: str) -> str:
    """``Company · Surface`` - the name for any surface with a line to itself.

    Falls back to a title-cased id for a kind this table has never heard of, so
    a provider added without touching this module still renders something a
    person can read instead of raising inside a paint.
    """
    key = kind_of(kind)
    surface = SURFACE.get(key)
    if surface is None:
        return key.replace("_", " ").title()
    company = COMPANY.get(key)
    return f"{company}{SEPARATOR}{surface}" if company else surface


def compact(kind: str) -> str:
    """The product alone, for chips, the tray tooltip and the menu popover."""
    key = kind_of(kind)
    return COMPACT.get(key) or full(key)


def abbrev(kind: str) -> str:
    """Two letters for the macOS menu bar.

    The caller keeps its own fallback for an unknown id (``provider[:2]``),
    which is why this returns the empty string rather than inventing one: an
    abbreviation guessed from a name is not the same thing as one chosen.
    """
    return ABBREV.get(kind_of(kind), "")


def account_label(kind: str, name: str | None) -> str:
    """The full name with the user's own identifier last, in brackets.

    ``Anthropic · Claude (Work)``. The company stays: an account named "Work"
    is still an Anthropic subscription, and the bracketed part is the only
    piece of the string the user wrote. An empty or blank name gives the plain
    full name back.
    """
    base = full(kind)
    label = (name or "").strip()
    return f"{base} ({label})" if label else base
