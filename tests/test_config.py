import json

import pytest
from pydantic import ValidationError

from aigauge.config import (
    DEFAULT_OPENCODE_USAGE_URL,
    WINDOW_MAX_HEIGHT,
    WINDOW_MIN_HEIGHT,
    BrowserAccount,
    ColorThresholds,
    Config,
    account_display_name,
    app_data_dir,
    browser_accounts,
    config_path,
    display_name_for_account,
    qt_scale_factor_env,
    webview_profile_dir,
)


@pytest.mark.parametrize(
    "bad_id",
    ["../evil", "a/b", "..", "x/../../y", "with space", "", "con", "NUL", "com1", "lpt9.dat"],
)
def test_browser_account_rejects_unsafe_ids(bad_id):
    with pytest.raises(ValidationError):
        BrowserAccount(id=bad_id, kind="claude")


@pytest.mark.parametrize("good_id", ["claude", "codex", "opencode_go", "claude-ab12cd34"])
def test_browser_account_accepts_generated_ids(good_id):
    assert BrowserAccount(id=good_id, kind="claude").id == good_id


@pytest.mark.parametrize("bad_id", ["../../evil", "a/b", "..", "foo/bar"])
def test_webview_profile_dir_rejects_traversal(bad_id):
    with pytest.raises(ValueError):
        webview_profile_dir(bad_id)


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://opencode.ai/workspace/x/go",
        "file:///etc/passwd",
        "data:text/html,<h1>hi",
        "https://user:pass@opencode.ai/workspace/x/go",
        "https://opencode.ai:8443/workspace/x/go",
        "https://evil.com/workspace/x/go",
        "https://opencode.ai.evil.com/workspace/x/go",
        "https://127.0.0.1/workspace/x/go",
        "https://opencode.ai",
        "https://opencode.ai/",
        "https://evil.com\\.opencode.ai/workspace/x/go",
        "https://opencode.ai/work\tspace/go",
        "https://opencode.ai/work space/go",
    ],
)
def test_validate_opencode_usage_url_rejects_unsafe(bad_url):
    from aigauge.config import validate_opencode_usage_url

    with pytest.raises(ValueError):
        validate_opencode_usage_url(bad_url)


def test_validate_opencode_usage_url_accepts_workspace_url():
    from aigauge.config import validate_opencode_usage_url

    url = "https://opencode.ai/workspace/wrk_abc/go"
    assert validate_opencode_usage_url(url) == url


def test_load_rejects_config_with_unsafe_opencode_url():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"opencode_go": {"usage_url": "https://evil.com/workspace/x/go"}}',
        encoding="utf-8",
    )
    c = Config.load()
    assert c.opencode_go.usage_url.startswith("https://opencode.ai/workspace/")


def test_load_rejects_config_with_unsafe_account_id():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"browser_accounts": [{"id": "../../evil", "kind": "claude"}]}',
        encoding="utf-8",
    )
    # A poisoned id must not survive into a live profile path; load falls back
    # to defaults rather than adopting the traversal id.
    c = Config.load()
    assert all(webview_profile_dir(a.id) for a in c.browser_accounts)
    assert [a.id for a in c.browser_accounts] == ["claude", "codex"]


def test_unsafe_opencode_url_does_not_wipe_sibling_settings():
    # An existing config with a now-invalid usage_url plus lots of other
    # customization must keep everything else and just coerce the URL to the
    # safe default — not reset the whole config.
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "active_refresh_interval_minutes": 3,
                "copilot": {"username": "octocat", "monthly_quota": 7000},
                "openrouter": {"daily_budget": 25.0},
                "window": {"x": 111, "y": 222},
                "opencode_go": {"usage_url": "http://opencode.ai/workspace/x/go"},
            }
        ),
        encoding="utf-8",
    )
    c = Config.load()
    assert c.opencode_go.usage_url == DEFAULT_OPENCODE_USAGE_URL
    # Siblings preserved:
    assert c.active_refresh_interval_minutes == 3
    assert c.copilot.username == "octocat"
    assert c.copilot.monthly_quota == 7000
    assert c.openrouter.daily_budget == 25.0
    assert c.window.x == 111 and c.window.y == 222


def test_unsafe_account_id_drops_only_that_account():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "active_refresh_interval_minutes": 4,
                "browser_accounts": [
                    {"id": "claude", "kind": "claude"},
                    {"id": "../../evil", "kind": "claude"},
                    {"id": "codex", "kind": "codex"},
                ],
            }
        ),
        encoding="utf-8",
    )
    c = Config.load()
    assert [a.id for a in c.browser_accounts] == ["claude", "codex"]
    assert c.active_refresh_interval_minutes == 4


def test_defaults():
    c = Config()
    assert c.active_refresh_interval_minutes == 5
    assert c.refresh_interval_minutes == 60
    assert c.providers.claude is True
    assert c.providers.codex is True
    assert [a.id for a in c.browser_accounts] == ["claude", "codex"]
    assert [a.kind for a in c.browser_accounts] == ["claude", "codex"]
    assert c.providers.copilot is True
    assert c.providers.opencode_go is False
    assert c.start_at_login is False
    assert c.copilot.monthly_quota == 1500
    assert c.opencode_go.usage_url.startswith("https://opencode.ai/workspace/")
    assert c.collapsed_tiles == []
    assert c.window.always_on_top is True
    assert c.window.collapsed is False
    assert c.window.fade_when_inactive is False
    assert c.window.opacity == 0.8
    assert c.window.ui_scale == 1.0


def test_ui_scale_round_trips_and_maps_to_qt_factor():
    c = Config()
    # Default scale leaves Qt's own DPI handling untouched.
    assert qt_scale_factor_env(c) is None

    c.window.ui_scale = 1.5
    assert qt_scale_factor_env(c) == "1.5"
    c.window.ui_scale = 2.0
    assert qt_scale_factor_env(c) == "2"


def test_ui_scale_persists(tmp_path, monkeypatch):
    c = Config()
    c.window.ui_scale = 1.25
    c.save()
    assert Config.load().window.ui_scale == 1.25


def test_round_trip(tmp_path, monkeypatch):
    c = Config()
    c.active_refresh_interval_minutes = 2
    c.refresh_interval_minutes = 10
    c.start_at_login = True
    c.providers.codex = False
    c.browser_accounts[1].enabled = False
    c.copilot.username = "octocat"
    c.copilot.billing_org = "my-org"
    c.copilot.monthly_quota = 1500
    c.window.x = 100
    c.window.y = 200
    c.providers.opencode_go = True
    c.opencode_go.usage_url = "https://opencode.ai/workspace/test/go"
    c.collapsed_tiles = ["claude"]
    c.save()

    loaded = Config.load()
    assert loaded.active_refresh_interval_minutes == 2
    assert loaded.refresh_interval_minutes == 10
    assert loaded.start_at_login is True
    assert loaded.providers.codex is False
    assert loaded.browser_accounts[1].enabled is False
    assert loaded.providers.claude is True
    assert loaded.copilot.username == "octocat"
    assert loaded.copilot.billing_org == "my-org"
    assert loaded.copilot.monthly_quota == 1500
    assert loaded.window.x == 100
    assert loaded.window.y == 200
    assert loaded.providers.opencode_go is True
    assert loaded.opencode_go.usage_url == "https://opencode.ai/workspace/test/go"
    assert loaded.collapsed_tiles == ["claude"]

def test_load_missing_returns_defaults():
    c = Config.load()
    assert c.refresh_interval_minutes == 60


def test_paths_under_appdata(tmp_path):
    assert str(tmp_path) in str(app_data_dir())
    assert config_path() == app_data_dir() / "config.json"
    assert webview_profile_dir("claude") == app_data_dir() / "profiles" / "claude"


def test_load_corrupt_falls_back_to_defaults():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("{ not valid json", encoding="utf-8")
    c = Config.load()
    assert c.refresh_interval_minutes == 60


def test_load_migrates_old_refresh_interval_to_active_rate():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"refresh_interval_minutes": 5, "providers": {"claude": true, "codex": true, "copilot": true}}',
        encoding="utf-8",
    )
    c = Config.load()
    assert c.active_refresh_interval_minutes == 5
    assert c.refresh_interval_minutes == 60
    assert [(a.id, a.kind, a.enabled) for a in c.browser_accounts] == [
        ("claude", "claude", True),
        ("codex", "codex", True),
    ]


def test_load_migrates_legacy_copilot_pro_request_quota_to_credits():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"copilot": {"monthly_quota": 300}}',
        encoding="utf-8",
    )

    c = Config.load()

    assert c.copilot.monthly_quota == 1500
    assert c.opencode_go.usage_url.startswith("https://opencode.ai/workspace/")
    assert c.collapsed_tiles == []

def test_load_migrates_legacy_provider_toggles_to_browser_accounts():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"providers": {"claude": false, "codex": true, "copilot": false}}',
        encoding="utf-8",
    )

    c = Config.load()

    assert [(a.id, a.kind, a.enabled) for a in c.browser_accounts] == [
        ("claude", "claude", False),
        ("codex", "codex", True),
    ]


def test_browser_account_display_names():
    account = BrowserAccount(id="codex-work", kind="codex", name="Work")

    assert account_display_name(account) == "Codex (Work)"


def test_display_name_for_configured_account():
    c = Config()
    c.browser_accounts.append(
        BrowserAccount(id="claude-team", kind="claude", name="Team")
    )

    assert display_name_for_account(c, "claude-team") == "Claude (Team)"
    assert [a.id for a in browser_accounts(c, kind="claude")] == [
        "claude",
        "claude-team",
    ]


def test_load_migrates_start_with_windows_to_start_at_login():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"start_with_windows": true}',
        encoding="utf-8",
    )
    c = Config.load()
    assert c.start_at_login is True


def test_load_clamps_saved_window_size():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"window": {"width": 5000, "height": 2}}',
        encoding="utf-8",
    )

    c = Config.load()

    assert c.window.width == 340
    assert c.window.height == 80


def test_color_thresholds_reject_stylesheet_injection():
    from aigauge.config import ColorThresholds

    # Colours are interpolated into Qt stylesheets, so a non-hex value could
    # close the declaration and inject arbitrary QSS (including url() fetches).
    bad = ColorThresholds(
        green_color="red; } QWidget { image: url(http://evil/x.png)",
        yellow_color="",
        orange_color="#ggg",
        red_color="javascript:alert(1)",
    )
    assert bad.green_color == "#22c55e"
    assert bad.yellow_color == "#f59e0b"
    assert bad.orange_color == "#f97316"
    assert bad.red_color == "#ef4444"
    # A legitimate custom colour is preserved.
    assert ColorThresholds(green_color="#0A1B2C").green_color == "#0A1B2C"


def test_color_thresholds_repair_out_of_order_cutoffs():
    from aigauge.config import ColorThresholds

    repaired = ColorThresholds(green_max=90, yellow_max=10, orange_max=50)
    assert (repaired.green_max, repaired.yellow_max, repaired.orange_max) == (59, 79, 94)


def test_existing_config_without_colors_still_loads_with_defaults():
    # Adding per-account colours must not disturb a config written by an
    # earlier version: every other setting survives and colours default.
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "active_refresh_interval_minutes": 3,
                "copilot": {"username": "octocat", "monthly_quota": 7000},
                "browser_accounts": [{"id": "claude", "kind": "claude"}],
            }
        ),
        encoding="utf-8",
    )
    c = Config.load()
    assert c.active_refresh_interval_minutes == 3
    assert c.copilot.username == "octocat"
    assert c.copilot.monthly_quota == 7000
    assert c.browser_accounts[0].colors.green_max == 59
    assert c.copilot.colors.red_color == "#ef4444"


@pytest.mark.parametrize(
    "colors",
    [
        # Non-str / non-int payloads: these hit pydantic's own type coercion,
        # which raises - and a raise inside Config.load() discards the whole
        # file. Every one of these previously wiped the user's config.
        {"green_max": 500},
        {"green_max": -1},
        {"green_max": 10**20},
        {"green_max": "abc"},
        {"green_max": 59.7},
        {"green_max": None},
        {"green_max": True},
        {"green_color": 123},
        {"green_color": True},
        {"green_color": ["#fff"]},
        {"green_color": None},
        {"green_color": ""},
        {"green_color": "#fff"},
        {"green_color": "red; } * { background: url(http://evil/x) } a {"},
        {"green_max": 90, "yellow_max": 10, "orange_max": 50},
        # Non-finite floats. json.dumps writes these as the bare tokens
        # Infinity / -Infinity / NaN and json.loads accepts them by default,
        # so they reach the validator as real floats. int(inf) raises
        # OverflowError - a different exception class than int("abc").
        {"green_max": float("inf")},
        {"green_max": float("-inf")},
        {"green_max": float("nan")},
        {"yellow_max": float("inf")},
        {"orange_max": float("-inf")},
        # A colors block that isn't a mapping at all.
        "nope",
        None,
        [],
        7,
    ],
)
def test_malformed_colors_never_wipe_the_config(colors):
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "active_refresh_interval_minutes": 3,
                "copilot": {
                    "username": "octocat",
                    "monthly_quota": 7000,
                    "colors": colors,
                },
                "openrouter": {"daily_budget": 25.0},
                "window": {"x": 111, "y": 222},
            }
        ),
        encoding="utf-8",
    )
    c = Config.load()
    # Every unrelated setting must survive a bad colours block.
    assert c.active_refresh_interval_minutes == 3
    assert c.copilot.username == "octocat"
    assert c.copilot.monthly_quota == 7000
    assert c.openrouter.daily_budget == 25.0
    assert c.window.x == 111 and c.window.y == 222
    # And the bands are always usable.
    assert 0 <= c.copilot.colors.green_max <= 100
    assert c.copilot.colors.green_color.startswith("#")


@pytest.mark.parametrize("literal", ["1e400", "-1e400", "Infinity", "-Infinity", "NaN"])
def test_overflowing_numeric_literals_never_wipe_the_config(literal):
    # These are lexically valid to json.loads (which accepts the non-finite
    # tokens by default) but overflow int(), so they must be handled as
    # malformed rather than escaping Config.load() as an exception.
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"active_refresh_interval_minutes": 3, "copilot": {"username": "octocat",'
        ' "colors": {"green_max": ' + literal + "}}}",
        encoding="utf-8",
    )
    c = Config.load()
    assert c.active_refresh_interval_minutes == 3
    assert c.copilot.username == "octocat"
    assert 0 <= c.copilot.colors.green_max <= 100


class _ReprBomb:
    """A value whose repr() raises, like a >4300-digit int or deep nesting."""

    def __init__(self, exc: type[BaseException]) -> None:
        self._exc = exc

    def __repr__(self) -> str:
        raise self._exc("boom")


@pytest.mark.parametrize("exc", [ValueError, RecursionError, MemoryError, TypeError])
@pytest.mark.parametrize("field", ["green_max", "green_color"])
def test_validators_never_render_untrusted_values_with_repr(exc, field):
    # repr() is not total. logging swallows a ValueError raised during
    # formatting but lets RecursionError through, so a bare %r in a validator
    # that promises never to raise is a live hazard.
    colors = ColorThresholds.model_validate({field: _ReprBomb(exc)})
    assert 0 <= colors.green_max <= 100
    assert colors.green_color == "#22c55e"


def test_unparseable_config_is_preserved_not_silently_destroyed():
    # A file that is not JSON has no recoverable structure, so defaults are the
    # only option - but Config.save() overwrites it on the next window move, so
    # the user's settings must be recoverable from somewhere.
    config_path().parent.mkdir(parents=True, exist_ok=True)
    raw = '{"copilot": {"username": "octocat"}, "window": {'  # truncated
    config_path().write_text(raw, encoding="utf-8")
    c = Config.load()
    assert c.copilot.username is None
    backup = config_path().with_suffix(config_path().suffix + ".corrupt")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == raw


def test_repeated_failed_loads_do_not_accumulate_backups():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        config_path().write_text("{not json", encoding="utf-8")
        Config.load()
    backups = list(config_path().parent.glob("*.corrupt*"))
    assert len(backups) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"window": {"height": "abc"}},
        {"window": {"opacity": 5.0}},
        {"expanded_tiles": 5},
        {"browser_accounts": "x"},
        {"refresh_interval_minutes": 9999},
        {"providers": []},
        {"openrouter": "nope"},
        {"opencode_go": 7},
    ],
)
def test_one_bad_setting_does_not_discard_the_others(bad):
    # Before salvage, a single bogus value anywhere in the file cost the user
    # every unrelated setting - accounts, quotas, budgets, window geometry -
    # and the next save made that permanent.
    config_path().parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active_refresh_interval_minutes": 3,
        "copilot": {"username": "octocat", "monthly_quota": 7000},
        "openrouter": {"daily_budget": 25.0},
        "browser_accounts": [{"id": "work", "kind": "claude"}],
        "window": {"x": 111, "y": 222},
    }
    payload.update(bad)
    config_path().write_text(json.dumps(payload), encoding="utf-8")
    c = Config.load()
    assert c.active_refresh_interval_minutes == 3
    assert c.copilot.username == "octocat"
    assert c.copilot.monthly_quota == 7000
    # Only the key that is actually broken is dropped. _migrate always
    # re-seeds the built-in claude/codex accounts, so assert on membership.
    if "browser_accounts" not in bad:
        assert "work" in [a.id for a in c.browser_accounts]
    if "openrouter" not in bad:
        assert c.openrouter.daily_budget == 25.0
    if "window" not in bad:
        assert c.window.x == 111 and c.window.y == 222


def test_malformed_account_colors_do_not_wipe_the_config():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "active_refresh_interval_minutes": 3,
                "copilot": {"username": "octocat"},
                "browser_accounts": [
                    {"id": "claude", "kind": "claude", "colors": {"green_max": 500}},
                    {"id": "codex", "kind": "codex", "colors": 7},
                ],
            }
        ),
        encoding="utf-8",
    )
    c = Config.load()
    assert c.copilot.username == "octocat"
    assert c.active_refresh_interval_minutes == 3
    assert [a.id for a in c.browser_accounts] == ["claude", "codex"]


def test_color_thresholds_clamp_rather_than_reject_numeric_cutoffs():
    from aigauge.config import ColorThresholds

    # In range after clamping and still ordered -> the clamped value is kept.
    assert ColorThresholds(green_max=-5).green_max == 0
    # Clamping to 100 would put green above yellow, so the band set is
    # repaired to defaults rather than left with an unreachable range.
    repaired = ColorThresholds(green_max=500)
    assert repaired.green_max <= repaired.yellow_max <= repaired.orange_max
    assert (repaired.green_max, repaired.yellow_max) == (59, 79)
    # A clamped value that stays ordered survives.
    assert ColorThresholds(green_max=500, yellow_max=100, orange_max=100).green_max == 100


def test_color_thresholds_reject_assignment_of_non_hex_color():
    from aigauge.config import ColorThresholds

    # validate_assignment keeps a runtime mutation from smuggling a payload
    # into the Qt stylesheet sinks.
    ct = ColorThresholds()
    ct.green_color = "red; } * { background: url(http://evil/x) }"
    assert ct.green_color == "#22c55e"


@pytest.mark.parametrize(
    "payload,check",
    [
        ({"window": {"height": "abc"}}, lambda c: c.window.height == 220),
        # Pinned to the exact clamp, not ">= 1": that weaker assertion was
        # satisfied by the raw 5, by the default 220 and by the floor alike,
        # so it could not tell clamping from doing nothing at all.
        ({"window": {"height": 5}}, lambda c: c.window.height == WINDOW_MIN_HEIGHT),
        ({"window": {"height": 9999}}, lambda c: c.window.height == WINDOW_MAX_HEIGHT),
        ({"window": {"opacity": 5.0}}, lambda c: c.window.opacity == 1.0),
        ({"window": {"opacity": -1}}, lambda c: c.window.opacity == 0.3),
        ({"window": {"ui_scale": -3}}, lambda c: c.window.ui_scale == 0.75),
        ({"window": {"ui_scale": 99}}, lambda c: c.window.ui_scale == 4.0),
        ({"copilot": {"monthly_quota": 0}}, lambda c: c.copilot.monthly_quota == 1),
        ({"copilot": {"monthly_quota": "x"}}, lambda c: c.copilot.monthly_quota == 1500),
        ({"refresh_interval_minutes": 99999}, lambda c: c.refresh_interval_minutes == 180),
        ({"active_refresh_interval_minutes": 0}, lambda c: c.active_refresh_interval_minutes == 1),
        (
            {"active_refresh_interval_minutes": float("inf")},
            lambda c: c.active_refresh_interval_minutes == 5,
        ),
        ({"openrouter": {"daily_budget": -1}}, lambda c: c.openrouter.daily_budget == 0.0),
        (
            {"openrouter": {"daily_budget": float("nan")}},
            lambda c: c.openrouter.daily_budget is None,
        ),
        ({"openrouter": {"daily_budget": True}}, lambda c: c.openrouter.daily_budget is None),
    ],
)
def test_bounded_settings_coerce_instead_of_discarding_their_block(payload, check):
    """Field(ge=/le=) raises, and a raise costs the whole top-level key.

    A negative daily_budget used to take the user's OpenRouter gauge colours
    with it, silently. Every bounded setting must clamp instead, so one bad
    number costs only that number.
    """
    base = {
        "active_refresh_interval_minutes": 3,
        "copilot": {"username": "octocat"},
        "window": {"x": 111, "y": 222},
    }
    for key, value in payload.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(base), encoding="utf-8")
    c = Config.load()
    assert check(c)
    # The sibling settings in the same block must survive.
    assert c.copilot.username == "octocat"


def test_bad_budget_no_longer_discards_openrouter_gauge_colours():
    # This is the exact reported regression.
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps({"openrouter": {"daily_budget": -1, "colors": {"green_max": 10}}}),
        encoding="utf-8",
    )
    c = Config.load()
    assert c.openrouter.colors.green_max == 10
