import json
import time

import pytest

from aigauge.error_dialog import _format_diagnostics
from aigauge.models import SnapshotStatus, UsageSnapshot


def test_format_diagnostics_redacts_email():
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"body_text": "signed in as person@example.com on the usage page"},
    )
    out = _format_diagnostics("claude", snapshot)
    assert "person@example.com" not in out
    assert "[redacted-email]" in out


def test_format_diagnostics_truncates_long_body_text():
    long_body = "person@example.com " + ("x" * 5000)
    snapshot = UsageSnapshot(
        provider="codex",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"body_text": long_body},
    )
    out = _format_diagnostics("codex", snapshot)
    assert "[truncated]" in out
    assert "person@example.com" not in out
    # The huge page dump must not be copied wholesale into the clipboard.
    assert out.count("x") < 1000


def test_format_diagnostics_caps_page_supplied_strings_other_than_body_text():
    """The cap must not be an allowlist of one field name.

    Error snapshots now carry page- and Chromium-supplied strings whose length
    nothing on this side controls: document.title, Chromium's load_error_string
    and each row's raw text. Truncating only the key literally named body_text
    stopped covering the payload the moment those fields were added.
    """
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="page failed to load",
        raw={
            "load_failed": True,
            "title": "t" * 20000,
            "load_error_string": "e" * 20000,
            "session": {"raw": "r" * 20000},
        },
    )
    out = _format_diagnostics("claude", snapshot)

    assert out.count("t") < 3000, "document.title reached the clipboard uncapped"
    assert out.count("e") < 3000, "Chromium's error string reached the clipboard uncapped"
    assert out.count("r") < 3000, "a nested row's raw text reached the clipboard uncapped"


def test_format_diagnostics_still_caps_body_text_harder_than_other_strings():
    # body_text is the largest and most identifying field; its tighter limit
    # must not be lost to the general cap.
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"body_text": "b" * 20000},
    )
    assert _format_diagnostics("claude", snapshot).count("b") < 700


def test_format_diagnostics_includes_the_app_version():
    # Fork and upstream ship overlapping release numbers, so a pasted
    # diagnostics blob is ambiguous without the full version string. The
    # README and the version-scheme docs both promise this field is here.
    import json

    from aigauge import __version__

    snapshot = UsageSnapshot(provider="claude", status=SnapshotStatus.ERROR, error="boom")
    payload = json.loads(_format_diagnostics("claude", snapshot))
    assert payload["app_version"] == __version__
    assert "+cfa." in payload["app_version"]


def test_diagnostics_are_bounded_against_a_page_controlled_payload():
    """Breadth, not just per-string length.

    Everything in snapshot.raw comes from a provider page. Measured before this
    cap existed: a 50,000-key payload produced a 1.3 MB clipboard carrying
    attacker-chosen text into a bug report.
    """
    hostile = {"/evil": {"planted": "ATTACKER-CONTROLLED-STRING"}}
    hostile.update({f"flood{i}": i for i in range(50000)})
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"api": hostile, "rows": list(range(5000))},
    )

    out = _format_diagnostics("claude", snapshot)

    assert len(out) < 50_000, f"clipboard payload was {len(out)} bytes"
    assert "more keys" in out, "truncation must be visible, not silent"
    assert "more items" in out


def test_format_diagnostics_redacts_azure_identifiers():
    """A subscription or tenant GUID identifies the account the way an email
    address does, and resource-group and resource names are chosen by the
    account holder - they routinely name a client, a project, or a person."""
    snapshot = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error=(
            "Azure rejected the cost query for /subscriptions/"
            "11111111-2222-3333-4444-555555555555/resourceGroups/rg-acme-prod"
            "/providers/Microsoft.CognitiveServices/accounts/acme-foundry"
        ),
        raw={"tenant": "99999999-8888-7777-6666-555555555555"},
    )
    out = _format_diagnostics("azure", snapshot)
    assert "11111111-2222-3333-4444-555555555555" not in out
    assert "99999999-8888-7777-6666-555555555555" not in out
    assert "rg-acme-prod" not in out
    assert "acme-foundry" not in out
    # The shape survives, so a bug report still says what kind of resource it was.
    assert "/subscriptions/<guid>/resourceGroups/<redacted>" in out
    assert "Microsoft.CognitiveServices/accounts/<redacted>" in out


def test_guids_are_redacted_for_every_provider_but_ordinary_text_is_kept():
    """The redaction runs over the whole blob, for every provider, and that is
    deliberate: a GUID identifies an account whoever issued it. What must
    survive is ordinary text - the previous version of this test asserted the
    opposite intent using a fixture with no GUID in it, so it could not fail
    for the reason its name implied."""
    snapshot = UsageSnapshot(
        provider="copilot",
        status=SnapshotStatus.ERROR,
        error="GitHub API 403",
        raw={
            "usageItems": [],
            "sku": "copilot_ai_credits",
            "conversation_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        },
    )
    out = _format_diagnostics("copilot", snapshot)
    assert "copilot_ai_credits" in out
    assert "<redacted>" not in out
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" not in out
    assert "<guid>" in out


def test_diagnostics_truncate_a_long_error_string():
    """snapshot.error is as page- or API-supplied as snapshot.raw is, and it
    was the one field _sanitize_raw never saw."""
    snapshot = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error="x" * 100_000,
    )
    out = _format_diagnostics("azure", snapshot)
    assert len(out) < 20_000
    assert "[truncated]" in out


def test_strings_nested_in_lists_are_capped_like_dict_values():
    """The length cap applied only to strings that were a *direct* dict value.

    A list of pairs - snapshot.raw["buckets"] is [[ServiceName, cost], ...] -
    walked straight past it, and ServiceName comes off the wire.
    """
    from aigauge.error_dialog import _STRING_LIMIT, _sanitize_raw

    out = _sanitize_raw({"buckets": [["A" * 50_000, 1.0]], "notes": ["B" * 50_000]})
    assert len(out["buckets"][0][0]) <= _STRING_LIMIT + 20
    assert len(out["notes"][0]) <= _STRING_LIMIT + 20


def test_a_hostile_service_name_cannot_inflate_the_diagnostics_blob():
    snapshot = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error="boom",
        raw={"buckets": [["S" * 200_000, 1.0]], "notes": ["N" * 200_000]},
    )
    assert len(_format_diagnostics("azure", snapshot)) < 20_000


def test_the_dialog_header_does_not_render_markup_from_an_error(qtbot):
    """The header is a RichText QLabel, so an error string is markup unless it
    is escaped - and the AAD error code is an upstream-supplied string."""
    from aigauge.error_dialog import ErrorDetailsDialog

    snapshot = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error="<b>Session expired.</b> Sign in at <a href='https://evil.example'>here</a>",
    )
    dialog = ErrorDetailsDialog("azure", "Microsoft · Azure", snapshot)
    qtbot.addWidget(dialog)
    text = dialog._header_text  # noqa: SLF001
    assert "<a href" not in text
    assert "&lt;b&gt;" in text


def test_the_dialog_header_redacts_azure_ids(qtbot):
    from aigauge.error_dialog import ErrorDetailsDialog

    snapshot = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error=(
            "Azure request failed for /subscriptions/"
            "11111111-2222-3333-4444-555555555555/providers/x"
        ),
    )
    dialog = ErrorDetailsDialog("azure", "Microsoft · Azure", snapshot)
    qtbot.addWidget(dialog)
    assert "11111111-2222-3333-4444-555555555555" not in dialog._header_text  # noqa: SLF001
    # Redacted first and escaped last, so the marker is escaped markup in the
    # source and renders as the literal "<guid>" - see
    # test_the_header_shows_the_redaction_marker_to_the_reader.
    assert "&lt;guid&gt;" in dialog._header_text  # noqa: SLF001


def test_child_resource_names_are_redacted_too():
    """A Foundry project name is exactly the customer-identifying string this
    pass exists to remove, and it sits one segment below the account."""
    from aigauge.error_dialog import _redact_azure_ids

    out = _redact_azure_ids(
        "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg-x"
        "/providers/Microsoft.CognitiveServices/accounts/acme-foundry"
        "/projects/CLIENT-ALPHA"
    )
    assert "CLIENT-ALPHA" not in out
    assert "acme-foundry" not in out
    # The shape still says what kind of resource was involved.
    assert "Microsoft.CognitiveServices/accounts/<redacted>/projects/<redacted>" in out


def test_url_encoded_resource_paths_are_redacted():
    """requests echoes an encoded path back in its exception message."""
    from aigauge.error_dialog import _redact_azure_ids

    out = _redact_azure_ids(
        "/subscriptions/11111111-2222-3333-4444-555555555555%2FresourceGroups"
        "%2Frg-secret%2Fproviders%2FMicrosoft.CognitiveServices%2Faccounts"
        "%2Fclient-name"
    )
    assert "rg-secret" not in out
    assert "client-name" not in out


def test_a_guid_without_hyphens_is_redacted():
    """Azure accepts and emits both forms, and the dashed pattern does not
    match this one at all. Anchored to an Azure context so an md5 elsewhere in
    the blob is not swept up with it."""
    from aigauge.error_dialog import _redact_azure_ids

    out = _redact_azure_ids(
        "https://login.microsoftonline.com/tenants/"
        "11111111222233334444555555555555/oauth2/v2.0/token"
    )
    assert "11111111222233334444555555555555" not in out
    assert "<guid>" in out


def test_a_guid_after_an_encoded_separator_is_redacted():
    """%2F ends in a hex digit, so \\b never fired between it and the GUID -
    the resource names in an encoded path were redacted while the
    subscription id in the same string was not."""
    from aigauge.error_dialog import _redact_azure_ids

    out = _redact_azure_ids(
        "https://management.azure.com/subscriptions%2F"
        "11111111-1111-1111-1111-111111111111%2FresourceGroups%2Frg-secret"
    )
    assert "11111111-1111-1111-1111-111111111111" not in out
    assert "rg-secret" not in out


# --- the redaction pass is a scalpel, not a broom ---------------------------


def test_a_hash_that_is_not_an_azure_id_survives_the_blob():
    """Any 32 hex digits became <guid>, so an md5, a Chromium request id or a
    32-char session id in a Claude or Codex payload was removed from the blob
    the user is told to paste into a bug report."""
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="etag mismatch",
        raw={"etag": "d41d8cd98f00b204e9800998ecf8427e"},
    )
    out = _format_diagnostics("claude", snapshot)
    assert "d41d8cd98f00b204e9800998ecf8427e" in out


def test_a_non_azure_providers_path_is_left_alone():
    """/providers/ is not an Azure-only word: a model name on an OpenRouter
    URL was being redacted out of another provider's diagnostics."""
    from aigauge.error_dialog import _redact_azure_ids

    text = "https://api.example.com/v1/providers/openrouter/models/gpt-4o"
    assert _redact_azure_ids(text) == text


def test_an_arm_path_is_still_redacted_end_to_end():
    from aigauge.error_dialog import _redact_azure_ids

    out = _redact_azure_ids(
        "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups"
        "/rg-client/providers/Microsoft.Web/sites/client-portal"
    )
    assert "rg-client" not in out and "client-portal" not in out
    assert "<guid>" in out and "Microsoft.Web/sites/<redacted>" in out


def test_a_name_containing_an_apostrophe_is_redacted_whole():
    """The segment pattern stopped at a quote, so the customer-identifying
    tail of the name survived into the clipboard blob."""
    from aigauge.error_dialog import _redact_azure_ids

    out = _redact_azure_ids(
        "/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups"
        "/rg-o'brien-secret/providers/Microsoft.Web/sites/x"
    )
    assert "brien-secret" not in out


def test_json_structure_survives_the_segment_pattern():
    """A double quote still ends a segment: the blob is JSON, and eating the
    closing quote would make it unreadable."""
    from aigauge.error_dialog import _redact_azure_ids

    out = _redact_azure_ids(
        '{"url": "/subscriptions/11111111-2222-3333-4444-555555555555'
        '/resourceGroups/rg-x", "status": 403}'
    )
    assert out.endswith('", "status": 403}')


def test_sanitize_raw_caps_dict_keys_like_values():
    """The cap exists because the email pass is quadratic on a long
    non-matching string; a key is as good a place to put one as a value."""
    from aigauge.error_dialog import _STRING_LIMIT, _sanitize_raw

    out = _sanitize_raw({"k" * 5000: 1})
    assert all(len(key) <= _STRING_LIMIT + 20 for key in out)


def test_sanitize_raw_caps_strings_inside_tuples():
    from aigauge.error_dialog import _sanitize_raw

    out = _sanitize_raw({"pair": ("t" * 5000, "u" * 5000)})
    assert all(len(item) < 3000 for item in out["pair"])


def test_sanitize_raw_caps_strings_inside_sets():
    """A set was the last container the walk stepped over, and
    ``json.dumps(default=str)`` then rendered it verbatim - after the cap had
    already run, so the cap had nothing to do with the size of the blob."""
    from aigauge.error_dialog import _sanitize_raw

    out = _sanitize_raw({"ids": {"S" * 5000}, "frozen": frozenset({"T" * 5000})})
    assert all(len(item) < 3000 for item in out["ids"])
    assert all(len(item) < 3000 for item in out["frozen"])
    # A list, so the blob is still JSON rather than a Python repr.
    assert json.dumps(out)


def test_sanitize_raw_does_not_recurse_off_the_stack():
    """json.loads accepts a payload deeper than this walk could handle, and
    ErrorDetailsDialog.__init__ does not catch RecursionError - so the one
    dialog a user opens when something is already wrong would not open."""
    from aigauge.error_dialog import _sanitize_raw

    deep = current = {}
    for _ in range(600):
        current["next"] = {}
        current = current["next"]
    out = _sanitize_raw(deep)
    assert "too deep" in json.dumps(out)


def test_the_email_pass_is_linear_in_a_long_non_matching_string():
    """The unbounded quantifiers backtracked quadratically: 80 KB took 7.5 s
    and 200 KB was still running after 25 s, on the GUI thread. The string cap
    keeps today's payloads short enough to hide it, so this pins the pattern
    itself."""
    from aigauge.error_dialog import _redact_emails

    blob = "a" * 200_000
    started = time.perf_counter()
    assert _redact_emails(blob) == blob
    assert time.perf_counter() - started < 0.1


def test_the_diagnostics_blob_is_bounded_for_a_huge_error_string():
    snapshot = UsageSnapshot(
        provider="claude",
        status=SnapshotStatus.ERROR,
        error="x" * 200_000,
        raw={"body_text": "y" * 200_000},
    )
    started = time.perf_counter()
    _format_diagnostics("claude", snapshot)
    assert time.perf_counter() - started < 0.1


def test_email_addresses_are_still_redacted_by_the_linear_pattern():
    from aigauge.error_dialog import _redact_emails

    assert _redact_emails("mail person.name+tag@sub.example.co.uk here") == (
        "mail [redacted-email] here"
    )


@pytest.mark.parametrize(
    "address",
    [
        "a" * 70 + "@example.com",
        "sk-" + "A" * 70 + ".alice@example.com",
        "a@" + "d" * 300 + ".com",
    ],
)
def test_a_long_address_is_still_an_address(address):
    """The lookbehind that makes the pass linear allows exactly one start
    position per run of local-part characters, so the bound has to cover the
    *whole* run: at {1,64} an identifier joined to an address by a dot - which
    is what a leaked token followed by a name looks like - pushed the run past
    the bound, no start position was viable, and the address was emitted
    whole. The bounds are what keep the pass linear; their size is not."""
    from aigauge.error_dialog import _redact_emails

    assert _redact_emails(f"contact {address} please") == (
        "contact [redacted-email] please"
    )


def test_the_header_shows_the_redaction_marker_to_the_reader(qtbot):
    """The markers were inserted after html.escape, so Qt parsed <redacted>
    as an unknown tag and dropped it - the reader saw /subscriptions// and
    could not tell whether an id had been removed or was never there."""
    from PyQt6.QtGui import QTextDocument

    from aigauge.error_dialog import ErrorDetailsDialog

    snapshot = UsageSnapshot(
        provider="azure",
        status=SnapshotStatus.ERROR,
        error=(
            "Azure request failed at /subscriptions/"
            "11111111-2222-3333-4444-555555555555/resourceGroups/rg-x"
            "/providers/Microsoft.Web/sites/portal"
        ),
    )
    dialog = ErrorDetailsDialog("azure", "Microsoft · Azure", snapshot)
    qtbot.addWidget(dialog)

    document = QTextDocument()
    document.setHtml(dialog._header_text)  # noqa: SLF001
    rendered = document.toPlainText()
    assert "<redacted>" in rendered
    assert "<guid>" in rendered
