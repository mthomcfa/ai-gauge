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
    assert "<guid>" in dialog._header_text  # noqa: SLF001


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
    from aigauge.error_dialog import _redact_azure_ids

    out = _redact_azure_ids("tenant 11111111222233334444555555555555 failed")
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
