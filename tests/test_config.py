import json
import logging
import sys

import pytest
from pydantic import ValidationError

from aigauge.config import (
    DEFAULT_OPENCODE_USAGE_URL,
    WINDOW_MAX_DIMENSION,
    WINDOW_MIN_WIDTH,
    WINDOW_MIN_HEIGHT,
    BrowserAccount,
    ColorThresholds,
    Config,
    account_display_name,
    app_data_dir,
    browser_accounts,
    config_path,
    display_name_for_account,
    is_usable_profile_id,
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


@pytest.mark.parametrize("bad_id", ["copilot", "openrouter", "opencode_go", "azure"])
def test_browser_account_rejects_another_providers_key(bad_id):
    """`App._build_providers` keys one dict on both, so an account carrying
    one of these ids owns that provider's entry in `_providers`, its
    snapshot, its tile and its place in the refresh queue - one account's
    numbers under another provider's name. `claude` and `codex` are not on
    the list: they are the two fixed browser accounts, and those ids are
    theirs."""
    with pytest.raises(ValidationError):
        BrowserAccount(id=bad_id, kind="claude")


@pytest.mark.parametrize("good_id", ["claude", "codex", "claude-ab12cd34"])
def test_browser_account_accepts_generated_ids(good_id):
    assert BrowserAccount(id=good_id, kind="claude").id == good_id


def test_a_config_naming_a_provider_as_an_account_still_loads(caplog):
    """`Config.load()` coerces rather than raises. One bad account must cost
    the user that account, not their whole settings file - the id is dropped
    in the migration, before validation can raise out of the blanket
    except."""
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "active_refresh_interval_minutes": 7,
                "browser_accounts": [
                    {"id": "claude", "kind": "claude"},
                    {"id": "copilot", "kind": "claude", "name": "Sneaky"},
                    {"id": "x" * 500_000, "kind": "claude"},
                ],
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="aigauge.config"):
        loaded = Config.load()

    assert loaded.active_refresh_interval_minutes == 7, (
        "the whole config was discarded"
    )
    assert [account.id for account in loaded.browser_accounts] == ["claude", "codex"]
    dropped = [
        record.getMessage()
        for record in caplog.records
        if "dropping browser account" in record.getMessage()
    ]
    assert len(dropped) == 2
    assert max(len(message) for message in dropped) < 200, (
        "a 500 000-character id reached the log"
    )


@pytest.mark.parametrize("bad_id", ["../../evil", "a/b", "..", "foo/bar"])
def test_webview_profile_dir_rejects_traversal(bad_id):
    with pytest.raises(ValueError):
        webview_profile_dir(bad_id)


def test_a_usable_profile_id_is_one_the_purge_will_actually_act_on():
    """The sweep's filter and the deletion's refusal must be the same rule.

    Settings' "Clear all browser data" tells the user how many folders in
    `profiles/` it left alone. It counted names the id rule rejects - but
    `purge_profile` refuses on two tests, the id rule *and* whether the
    resolved path is still inside `profiles/`, and a symlink pointing out of
    that directory has a perfectly legal name. So the one entry a hostile
    tree would construct was counted as deleted while the purge refused it,
    and the message was wrong in the "we deleted it" direction.
    """
    assert is_usable_profile_id("claude-deadbeef")
    for bad in ("../../evil", "a/b", "..", "foo/bar", "not an id", "CON", ""):
        assert not is_usable_profile_id(bad), bad
        with pytest.raises(ValueError):
            webview_profile_dir(bad)

    # The containment half, where the filesystem allows it to be built.
    profiles = app_data_dir() / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    outside = app_data_dir() / "OUTSIDE"
    outside.mkdir(exist_ok=True)
    try:
        (profiles / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows/CI
        pytest.skip("this filesystem does not allow symlinks")
    assert not is_usable_profile_id("escape"), (
        "a legal name resolving outside profiles/ counted as one to delete"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlink privilege")
def test_a_symlink_inside_the_profiles_directory_is_never_usable():
    """The predicate has to answer about the entry, not about its target.

    `webview_profile_dir` resolves, so a link inside `profiles/` that points
    at *another* profile passes containment - and the sweep then hands that
    id to `purge_profile`, which rmtree's the target. The live-scrape
    deferral is keyed on the link's own name, so an account mid-scrape has
    its `QWebEngineProfile` directory deleted under it through an alias the
    deferral never sees. A link to the root itself is the complementary
    case: `webview_profile_dir` permits it and `purge_profile` refuses it,
    so the entry was counted as deleted and then left alone.

    This app writes no links under `profiles/`, so refusing every one of
    them costs nothing real and makes the count the button reports the count
    that was really left behind.
    """
    profiles = app_data_dir() / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "live_one").mkdir(exist_ok=True)
    try:
        (profiles / "alias").symlink_to(profiles / "live_one", True)
        (profiles / "selfroot").symlink_to(profiles, True)
        (profiles / "dangling").symlink_to(profiles / "nothere", True)
        (profiles / "loop_a").symlink_to(profiles / "loop_b")
        (profiles / "loop_b").symlink_to(profiles / "loop_a")
    except (OSError, NotImplementedError):  # pragma: no cover - CI/Windows
        pytest.skip("this filesystem does not allow symlinks")

    assert is_usable_profile_id("live_one"), "a real profile directory"
    for name, why in (
        ("alias", "a link to another account's profile"),
        ("selfroot", "a link to the profiles/ root, which the purge refuses"),
        ("dangling", "a link to nothing"),
        ("loop_a", "a symlink loop, where resolve() raises RuntimeError"),
    ):
        assert not is_usable_profile_id(name), why
    assert (profiles / "live_one").is_dir(), "the predicate deleted something"


def test_an_entry_resolving_to_the_profiles_root_is_not_one_to_delete(monkeypatch):
    """The containment case `webview_profile_dir` and `purge_profile` differ on.

    `webview_profile_dir` permits `resolved == root`; `purge_profile` refuses
    it. A plain symlink is caught a line earlier, so this is the reachable
    shape on Windows, where a directory *junction* to the `profiles/` root is
    followed by `resolve()` and reported as a link by nothing - `is_symlink()`
    answers False for one. Stood in for here by making that answer False.
    """
    from pathlib import Path

    profiles = app_data_dir() / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(
        Path, "resolve", lambda self, strict=False: profiles.absolute()
    )
    assert not is_usable_profile_id("junction"), (
        "an entry resolving to the profiles/ root was counted as one to delete"
    )


def test_is_usable_profile_id_answers_where_the_filesystem_refuses(monkeypatch):
    """A public predicate returns a bool or it is not one.

    `resolve()` touches the filesystem, and both arms of that are reachable
    from a directory nothing in this app created: a name the OS refuses
    raises `OSError`, and a symlink loop raises `RuntimeError`, which is not
    an `OSError` at all. Today's only caller filters on `is_dir()` first,
    which is False for a loop, so neither escapes the dialog - but the
    branch is what makes that filter optional rather than load-bearing.
    """
    from pathlib import Path

    for error in (OSError("refused"), RuntimeError("Symlink loop from ...")):

        def _raise(self, *args, _error=error, **kwargs):
            raise _error

        monkeypatch.setattr(Path, "resolve", _raise)
        assert is_usable_profile_id("claude-deadbeef") is False, error
        monkeypatch.undo()


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
    """The window is the user's to size from 1.4.0+cfa.8, so a saved width is
    kept rather than replaced with the constant - but it is still bounded.
    The bound here is a sanity bound: the loader has no idea which monitor the
    window will open on, and UsageWidget clamps to that screen's work area."""
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"window": {"width": 5000, "height": 2}}',
        encoding="utf-8",
    )

    c = Config.load()

    assert c.window.width == WINDOW_MAX_DIMENSION
    assert c.window.height == WINDOW_MIN_HEIGHT


def test_load_keeps_a_1_3_x_window_block_unchanged():
    """340x420 was the only size 1.3.x could save. It is inside the new bounds,
    so an upgrade opens the window exactly where it was left."""
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"window": {"width": 340, "height": 420, "x": 100, "y": 120}}',
        encoding="utf-8",
    )

    c = Config.load()

    assert (c.window.width, c.window.height) == (340, 420)
    assert (c.window.x, c.window.y) == (100, 120)


def test_a_1_3_x_config_has_never_been_sized_by_hand():
    """The upgrade case. 1.3.x wrote its auto-fitted height back on every
    release, hide and close, so "the size is not the first-run 340x220" said
    yes for practically every installed config - and auto-fit would have gone
    off for every existing user on their first launch of 1.4.0. The file
    itself is the answer: it has no ``user_sized`` key."""
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        '{"window": {"width": 340, "height": 268}}', encoding="utf-8"
    )

    assert Config.load().window.user_sized is False


def test_user_sized_survives_a_round_trip():
    c = Config()
    c.window.user_sized = True
    c.window.width, c.window.height = 500, 300
    c.save()

    loaded = Config.load()
    assert loaded.window.user_sized is True
    assert (loaded.window.width, loaded.window.height) == (500, 300)


@pytest.mark.parametrize(
    "raw",
    ["yes", 1, 0, None, [], {"a": 1}, "false"],
    ids=["yes", "one", "zero", "null", "list", "dict", "false-string"],
)
def test_a_hostile_user_sized_coerces_to_never_sized(raw):
    """It gates auto-fit, and a raise inside WindowState costs the user the
    whole window block. Anything but a real bool reads as "no"."""
    from aigauge.config import WindowState

    assert WindowState(user_sized=raw).user_sized is False


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"width": -5}, WINDOW_MIN_WIDTH),
        ({"width": 99999}, WINDOW_MAX_DIMENSION),
        ({"width": float("nan")}, 340),
        ({"width": "wide"}, 340),
        ({"width": True}, 340),
        ({"width": None}, 340),
    ],
    ids=["negative", "huge", "nan", "string", "bool", "null"],
)
def test_window_width_is_coerced_never_rejected(payload, expected):
    """A raise anywhere inside WindowState reaches Config.load()'s salvage and
    discards the whole window block, so every one of these coerces."""
    from aigauge.config import WindowState

    assert WindowState(**payload).width == expected


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
        ({"window": {"height": 9999}}, lambda c: c.window.height == WINDOW_MAX_DIMENSION),
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


def test_the_pending_purge_list_coerces_instead_of_carrying_junk():
    """The list is a delete list read at startup, from a file the app also
    treats as hostile everywhere else in this module.

    `purge_profile` is the defence that matters - it refuses anything that
    does not resolve strictly inside `profiles/` - but the coercion is what
    keeps a non-string from reaching it at all, and a validator nothing tests
    is a validator a future edit can delete.
    """
    assert Config(pending_profile_purges={"a": 1}).pending_profile_purges == []
    assert Config(pending_profile_purges="claude").pending_profile_purges == []
    assert Config(pending_profile_purges=None).pending_profile_purges == []
    assert Config(
        pending_profile_purges=[
            "claude-ab12cd34",
            "",
            None,
            True,
            3,
            1.5,
            b"codex",
            ["nested"],
            {"k": "v"},
            "codex-99999999",
        ]
    ).pending_profile_purges == ["claude-ab12cd34", "codex-99999999"]


def test_the_clear_list_is_coerced_and_bounded_like_the_purge_list():
    """The second deferral list is read at startup from the same file, and
    reaches the same rmtree, so it carries the same bounds."""
    from aigauge.config import _PENDING_PURGE_LIMIT, _PROFILE_ID_MAX_LEN

    assert Config(pending_data_clears={"a": 1}).pending_data_clears == []
    assert Config(pending_data_clears="claude").pending_data_clears == []
    assert Config(
        pending_data_clears=["claude", "", None, 3, b"codex", ["nested"], "codex"]
    ).pending_data_clears == ["claude", "codex"]
    assert Config(
        pending_data_clears=["claude", "a" * (_PROFILE_ID_MAX_LEN + 1)]
    ).pending_data_clears == ["claude"]
    assert len(
        Config(
            pending_data_clears=[f"codex-{index:08d}" for index in range(5_000)]
        ).pending_data_clears
    ) == _PENDING_PURGE_LIMIT


def test_the_pending_purge_list_survives_a_round_trip_through_the_file():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps({"pending_profile_purges": ["claude-ab12cd34", 7, ""]}),
        encoding="utf-8",
    )

    assert Config.load().pending_profile_purges == ["claude-ab12cd34"]


def test_the_pending_purge_list_is_bounded_in_length_and_in_entries():
    """A delete list read at every start, from a file this module treats as
    hostile everywhere else. `purge_profile` is still the defence that
    matters - it refuses anything that does not resolve strictly inside
    `profiles/` - but a poisoned file carrying 5 000 ids of 200 000
    characters should not reach it, or the line that announces the drain, at
    all. The length bound is `_PROFILE_ID_RE`'s own, so nothing the app can
    generate is lost.
    """
    from aigauge.config import _PENDING_PURGE_LIMIT, _PROFILE_ID_MAX_LEN

    long_id = "a" * (_PROFILE_ID_MAX_LEN + 1)
    kept = Config(
        pending_profile_purges=["claude-ab12cd34", long_id, "b" * _PROFILE_ID_MAX_LEN]
    ).pending_profile_purges
    assert kept == ["claude-ab12cd34", "b" * _PROFILE_ID_MAX_LEN]

    many = Config(
        pending_profile_purges=[f"codex-{index:08d}" for index in range(5_000)]
    ).pending_profile_purges
    assert len(many) == _PENDING_PURGE_LIMIT
    assert many[0] == "codex-00000000", "the cap took the wrong end of the list"


# --- config.json is written atomically --------------------------------------
#
# Since 1.3.1+cfa.6 the app writes this file on its own - at every deferred
# purge drain, from `App.__init__` and from the five-minute heartbeat - so a
# crash inside a write the user never asked for could truncate the file that
# holds every setting plus both pending lists. The loader survives that and
# the settings do not.


def _write_a_config(**kwargs) -> str:
    cfg = Config(**kwargs)
    cfg.save()
    return config_path().read_text(encoding="utf-8")


def test_a_save_that_cannot_replace_leaves_the_previous_file_intact():
    import os

    before = _write_a_config(refresh_interval_minutes=42)
    real_replace = os.replace

    def refuse(src, dst):
        raise OSError(28, "No space left on device")

    try:
        os.replace = refuse
        with pytest.raises(OSError):
            Config(refresh_interval_minutes=7).save()
    finally:
        os.replace = real_replace

    assert config_path().read_text(encoding="utf-8") == before
    assert list(config_path().parent.glob("*.tmp")) == [], "a temp file was left behind"


def test_a_save_interrupted_before_the_replace_leaves_the_old_file():
    """The window a bare `write_text` could not survive: the bytes are down but
    the rename has not happened."""
    import os

    before = _write_a_config(active_refresh_interval_minutes=3)
    real_fsync = os.fsync

    def refuse(fd):
        raise OSError(5, "Input/output error")

    try:
        os.fsync = refuse
        with pytest.raises(OSError):
            Config(active_refresh_interval_minutes=9).save()
    finally:
        os.fsync = real_fsync

    assert config_path().read_text(encoding="utf-8") == before
    assert list(config_path().parent.glob("*.tmp")) == []


def test_a_normal_save_round_trips_and_leaves_nothing_beside_it():
    Config(refresh_interval_minutes=33, expanded_tiles=["claude"]).save()

    loaded = Config.load()
    assert loaded.refresh_interval_minutes == 33
    assert loaded.expanded_tiles == ["claude"]
    assert [p.name for p in config_path().parent.iterdir()] == ["config.json"]


def test_the_file_arrives_by_replace_rather_than_by_truncating_it():
    """The property, not the spelling: the previous document is replaced whole
    rather than opened for writing."""
    import os

    seen: list = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    try:
        os.replace = spy
        Config().save()
    finally:
        os.replace = real_replace

    assert len(seen) == 1
    src, dst = seen[0]
    assert dst == str(config_path())
    assert src != dst and ".config-" in src


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_saved_file_is_owner_only_on_posix():
    """Not asked for - `mkstemp` creates 0600 and `os.replace` carries the temp
    file's mode across. Pinned rather than left implicit: it is a tightening
    (a bare `write_text` gave 0644 under the usual umask) and the one thing
    about the write that a reader could be surprised by.
    """
    import stat

    Config().save()
    assert stat.S_IMODE(config_path().stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlink privilege")
def test_a_save_replaces_a_symlinked_config_rather_than_writing_through_it():
    """The behaviour change `os.replace` brought, pinned and documented.

    A bare `write_text` followed the link, so a `config.json` pointed at some
    other file got that file truncated and overwritten with app JSON, in a
    directory the user never chose. `os.replace` replaces the link itself.
    This is the right direction - and it is also why a user who deliberately
    symlinks this file into a synced folder loses the link at the first save,
    which the docstring now says.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.parent / "elsewhere.json"
    target.write_text("do not overwrite me", encoding="utf-8")
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - CI/Windows
        pytest.skip("this filesystem does not allow symlinks")

    Config().save()

    assert not path.is_symlink(), "the save wrote through the link"
    assert path.is_file()
    assert target.read_text(encoding="utf-8") == "do not overwrite me"
    assert list(path.parent.glob("*.tmp")) == []


def test_a_quarantined_config_is_written_the_same_way():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text("{ not json", encoding="utf-8")

    Config.load()

    backup = config_path().with_suffix(config_path().suffix + ".corrupt")
    assert backup.read_text(encoding="utf-8") == "{ not json"
    assert list(config_path().parent.glob("*.tmp")) == []
