import sys
import warnings

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QPushButton

from aigauge import settings_dialog
from aigauge.config import Config
from aigauge.providers.catalog import record_scan, scan_due
from aigauge.settings_dialog import SettingsDialog
from aigauge.ui_style import wheel_step


def _button(dialog: SettingsDialog, name: str) -> QPushButton:
    button = dialog.findChild(QPushButton, name)
    assert button is not None
    return button


def test_sign_in_button_emits_sign_in_signal(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.sign_in_clicked) as signal:
        _button(dialog, "claude_signin_btn").click()

    assert signal.args == ["claude"]


def test_paste_cookie_button_emits_paste_cookie_signal(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.paste_cookie_clicked) as signal:
        _button(dialog, "codex_paste_cookie_btn").click()

    assert signal.args == ["codex"]


def test_claude_open_usage_button_launches_browser(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(
        settings_dialog, "_open_in_browser", lambda url: opened.append(url)
    )

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    _button(dialog, "claude_open_usage_btn").click()

    assert opened == [settings_dialog.CLAUDE_USAGE_URL]


def test_codex_open_usage_button_launches_browser(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(
        settings_dialog, "_open_in_browser", lambda url: opened.append(url)
    )

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    _button(dialog, "codex_open_usage_btn").click()

    assert opened == [settings_dialog.CODEX_USAGE_URL]




def test_opencode_go_sign_in_button_emits_sign_in_signal(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.sign_in_clicked) as signal:
        _button(dialog, "opencode_go_signin_btn").click()

    assert signal.args == ["opencode_go"]


def test_opencode_go_paste_cookie_button_emits_signal(qtbot):
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.paste_cookie_clicked) as signal:
        _button(dialog, "opencode_go_paste_cookie_btn").click()

    assert signal.args == ["opencode_go"]


def test_opencode_go_open_usage_button_launches_configured_url(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(
        settings_dialog, "_open_in_browser", lambda url: opened.append(url)
    )

    config = Config()
    config.opencode_go.usage_url = "https://opencode.ai/workspace/test/go"
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)
    _button(dialog, "opencode_go_open_usage_btn").click()

    assert opened == ["https://opencode.ai/workspace/test/go"]


def test_opencode_go_open_usage_button_falls_back_for_unsafe_url(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(
        settings_dialog, "_open_in_browser", lambda url: opened.append(url)
    )
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    dialog.opencode_go_url.setText("file:///etc/passwd")
    _button(dialog, "opencode_go_open_usage_btn").click()

    assert opened == [settings_dialog.OPENCODE_GO_USAGE_URL]


def test_opencode_go_settings_apply(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog.opencode_go_cb.setChecked(True)
    dialog.opencode_go_url.setText("https://opencode.ai/workspace/custom/go")
    dialog.apply_to(config)

    assert config.providers.opencode_go is True
    assert config.opencode_go.usage_url == "https://opencode.ai/workspace/custom/go"

def test_add_codex_account_creates_named_secondary_row(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog._add_browser_account("codex")  # noqa: SLF001
    dialog.apply_to(config)

    codex_accounts = [a for a in config.browser_accounts if a.kind == "codex"]
    assert len(codex_accounts) == 2
    assert codex_accounts[1].name == "Account 2"
    assert codex_accounts[1].enabled is True


def test_remove_secondary_account_clears_cookie(qtbot, monkeypatch):
    removed = []
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(
        settings_dialog,
        "set_provider_cookie",
        lambda key, value: removed.append((key, value)),
    )
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog._add_browser_account("claude")  # noqa: SLF001
    account_id = dialog._browser_accounts[-1].id  # noqa: SLF001
    dialog._remove_browser_account(account_id)  # noqa: SLF001
    dialog.apply_to(config)

    assert removed == [(account_id, None)]

def test_remove_account_clears_its_secret_and_hands_the_profile_to_the_app(
    qtbot, monkeypatch
):
    """The dialog no longer deletes the profile directory itself.

    `purge_profile` releases the cached `QWebEngineProfile` and rmtree's its
    directory, and a settings save can remove an account while its scrape is
    still out. Qt requires a profile to outlive its pages, and a page that
    survives its profile can flush rotated session cookies back into the
    directory that was just removed - so only the App, which knows what is in
    flight, may run it. See App._run_profile_purges.

    The stored credential is still cleared here and now: it is a keyring
    entry, nothing holds it open, and it is the part that matters.
    """
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    cleared: list[tuple] = []
    monkeypatch.setattr(
        settings_dialog,
        "set_provider_cookie",
        lambda key, value: cleared.append((key, value)),
    )
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog._add_browser_account("claude")  # noqa: SLF001
    account_id = dialog._browser_accounts[-1].id  # noqa: SLF001

    from aigauge.config import webview_profile_dir

    profile_dir = webview_profile_dir(account_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Cookies").write_bytes(b"SQLite format 3\x00")

    dialog._remove_browser_account(account_id)  # noqa: SLF001
    dialog.apply_to(config)

    assert (account_id, None) in cleared
    assert dialog.removed_profile_ids == [account_id]
    assert profile_dir.exists(), "the dialog deleted a profile the App may be using"


def test_fade_when_inactive_setting_applies(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    assert not dialog.fade_when_inactive_cb.isChecked()
    assert not dialog.opacity_slider.isEnabled()

    dialog.fade_when_inactive_cb.setChecked(True)
    dialog.opacity_slider.setValue(62)
    dialog.apply_to(config)

    assert config.window.fade_when_inactive is True
    assert config.window.opacity == 0.62

def test_clear_saved_pat_checkbox_removes_existing_pat(qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr(
        settings_dialog, "get_github_pat", lambda: None if calls else "saved"
    )
    monkeypatch.setattr(
        settings_dialog, "set_github_pat", lambda value: calls.append(value)
    )

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    dialog.clear_pat_cb.setChecked(True)

    dialog._accept()  # noqa: SLF001

    assert calls == [None]


def test_gauge_colors_dialog_keeps_cutoffs_non_decreasing(qtbot):
    from aigauge.config import ColorThresholds
    from aigauge.settings_dialog import GaugeColorsDialog

    dialog = GaugeColorsDialog("Claude", ColorThresholds())
    qtbot.addWidget(dialog)

    # Dragging green above yellow must push the later bands along, never leave
    # an unreachable range behind.
    dialog._spins["green"].setValue(90)  # noqa: SLF001

    colors = dialog.colors()
    assert colors.green_max == 90
    assert colors.yellow_max >= 90
    assert colors.orange_max >= colors.yellow_max


def test_gauge_colors_dialog_reset_restores_defaults(qtbot):
    from aigauge.config import ColorThresholds
    from aigauge.settings_dialog import GaugeColorsDialog

    dialog = GaugeColorsDialog("Claude", ColorThresholds(green_max=5, green_color="#010203"))
    qtbot.addWidget(dialog)
    dialog._reset_defaults()  # noqa: SLF001

    colors = dialog.colors()
    assert (colors.green_max, colors.yellow_max, colors.orange_max) == (59, 79, 94)
    assert colors.green_color == "#22c55e"


def test_account_and_provider_colors_persist_through_apply(qtbot, monkeypatch):
    from aigauge.config import ColorThresholds

    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog._browser_account_rows[0].colors = ColorThresholds(  # noqa: SLF001
        green_color="#111111"
    )
    dialog._provider_colors["copilot"] = ColorThresholds(  # noqa: SLF001
        red_color="#222222"
    )
    dialog.apply_to(config)

    assert config.browser_accounts[0].colors.green_color == "#111111"
    assert config.copilot.colors.red_color == "#222222"


def test_rescan_meters_button_arms_the_scan_and_asks_for_a_refresh(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog.QMessageBox, "information", lambda *a, **k: None)
    config = Config()
    record_scan(config, "claude", save=False)
    record_scan(config, "codex", save=False)
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.rescan_meters_clicked):
        _button(dialog, "rescan_meters_btn").click()

    assert scan_due(config, "claude") and scan_due(config, "codex")
    # Saved immediately: the refresh it triggers happens before OK is pressed.
    assert scan_due(Config.load(), "claude")


# --- Microsoft section -----------------------------------------------------

SUB = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"


def _tab_titles(dialog: SettingsDialog) -> list[str]:
    from PyQt6.QtWidgets import QTabWidget

    tabs = dialog.findChild(QTabWidget)
    assert tabs is not None
    return [tabs.tabText(i) for i in range(tabs.count())]


def _group_titles(dialog: SettingsDialog) -> list[str]:
    from PyQt6.QtWidgets import QGroupBox

    return [box.title() for box in dialog.findChildren(QGroupBox)]


def test_microsoft_tab_holds_the_three_sub_headings(qtbot):
    """Azure, Foundry and Copilot are one vendor relationship to the person
    configuring them, and Foundry only means anything beside the Azure block
    it configures."""
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    assert "Microsoft" in _tab_titles(dialog)
    assert "GitHub Copilot" not in _tab_titles(dialog)
    titles = _group_titles(dialog)
    for heading in ("Azure", "Foundry", "Copilot"):
        assert heading in titles


def test_copilot_controls_survive_the_move_unchanged(qtbot, monkeypatch):
    """Copilot moved under the Microsoft heading; nothing about it changed."""
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog.gh_username.setText("octocat")
    dialog.gh_billing_org.setText("acme")
    dialog._set_quota_selection(1500)  # noqa: SLF001
    dialog.apply_to(config)

    assert config.copilot.username == "octocat"
    assert config.copilot.billing_org == "acme"
    assert config.copilot.monthly_quota == 1500


def test_azure_settings_round_trip(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog.azure_cb.setChecked(True)
    dialog.azure_tenant.setText(TENANT)
    dialog.azure_client.setText(TENANT)
    dialog.azure_subscription.setText(SUB)
    dialog.azure_allowance.setValue(150.0)
    dialog.azure_reset_day.setValue(15)
    dialog.azure_top_rows.setValue(4)
    dialog.azure_marketplace_cb.setChecked(True)
    dialog.azure_resource_group.setText("rg-ai")
    dialog.apply_to(config)

    assert config.providers.azure is True
    assert config.azure.subscription_id == SUB
    assert config.azure.monthly_allowance == 150.0
    assert config.azure.reset_day == 15
    assert config.azure.top_rows == 4
    assert config.azure.include_marketplace is True
    assert config.azure.resource_group == "rg-ai"


def test_azure_rejects_a_non_guid_and_keeps_the_previous_value(qtbot, monkeypatch):
    """These ids are interpolated into request URLs; a rejected field must not
    block the rest of the save either."""
    warned: list = []
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(
        settings_dialog.QMessageBox, "warning", lambda *a, **k: warned.append(a)
    )
    config = Config()
    config.azure.subscription_id = SUB
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog.azure_subscription.setText("https://evil.example.com/../x")
    dialog.azure_allowance.setValue(42.0)
    dialog.apply_to(config)

    assert config.azure.subscription_id == SUB
    assert dialog.azure_subscription.text() == SUB
    # The rest of the save still went through.
    assert config.azure.monthly_allowance == 42.0
    assert warned


def test_pinned_foundry_ids_accept_arm_paths_and_drop_the_rest(qtbot, monkeypatch):
    warned: list = []
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(
        settings_dialog.QMessageBox, "warning", lambda *a, **k: warned.append(a)
    )
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    good = (
        f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
        "Microsoft.CognitiveServices/accounts/my-foundry"
    )
    dialog.azure_foundry_ids.setPlainText(f"{good}\n\nnot-a-resource-id\n")
    dialog.apply_to(config)

    assert config.azure.foundry_resource_ids == [good]
    assert dialog.azure_foundry_ids.toPlainText() == good
    assert warned


def test_azure_secret_is_saved_to_the_credential_store(qtbot, monkeypatch):
    stored: dict[str, str | None] = {"value": None}
    monkeypatch.setattr(
        settings_dialog, "set_azure_client_secret", lambda s: stored.update(value=s)
    )
    monkeypatch.setattr(
        settings_dialog, "get_azure_client_secret", lambda: stored["value"]
    )
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    dialog.azure_secret_edit.setText("s3cret")
    assert dialog._save_azure_secret() is True  # noqa: SLF001
    assert stored["value"] == "s3cret"


def test_azure_secret_save_is_read_back_before_it_is_believed(qtbot, monkeypatch):
    """A keychain that silently refuses the write would otherwise leave a tile
    asking for a secret the user thinks they saved."""
    warned: list = []
    monkeypatch.setattr(settings_dialog, "set_azure_client_secret", lambda s: None)
    monkeypatch.setattr(settings_dialog, "get_azure_client_secret", lambda: None)
    monkeypatch.setattr(
        settings_dialog.QMessageBox, "warning", lambda *a, **k: warned.append(a)
    )
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    dialog.azure_secret_edit.setText("s3cret")
    assert dialog._save_azure_secret() is False  # noqa: SLF001
    assert warned


def test_a_large_allowance_survives_a_settings_round_trip(qtbot, monkeypatch):
    """QDoubleSpinBox clamps to its range and _apply_azure writes the clamped
    value straight back, so a narrower widget silently rewrites the config.
    An allowance above a million is ordinary in JPY, KRW, INR and CLP."""
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    config = Config()
    config.azure.monthly_allowance = 2_500_000.0
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    dialog.apply_to(config)
    assert config.azure.monthly_allowance == 2_500_000.0


def test_a_foundry_project_child_id_can_be_pinned(qtbot, monkeypatch):
    """docs/next-session.md §8.1 parks "cost rows may carry a project child id"
    as an open risk whose mitigation is pinning ids by hand - which the field
    could not express."""
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(settings_dialog.QMessageBox, "warning", lambda *a, **k: None)
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    child = (
        f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
        "Microsoft.CognitiveServices/accounts/my-foundry/projects/proj1"
    )
    dialog.azure_foundry_ids.setPlainText(child)
    dialog.apply_to(config)
    assert config.azure.foundry_resource_ids == [child]


def test_clear_all_browser_data_hands_the_profiles_to_the_app(qtbot, monkeypatch):
    """The dialog clears the credentials and deletes no directory.

    `purge_profile` releases the cached `QWebEngineProfile` and rmtree's its
    directory; Qt requires a profile to outlive its pages, and this dialog is
    modeless with a refresh cycle running every five minutes, so doing it
    here is the most reachable way to destroy a profile under a live page.
    The id set is the whole sweep - the accounts in the dialog, the accounts
    in the config, the three fixed ids and every directory on disk - because
    the leftovers are what the button is for.
    """
    from aigauge.config import BrowserAccount, app_data_dir

    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "question",
        lambda *a, **k: settings_dialog.QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(settings_dialog.QMessageBox, "information", lambda *a, **k: None)
    cleared: list[str] = []
    monkeypatch.setattr(
        settings_dialog,
        "set_provider_cookie",
        lambda account_id, value: cleared.append(account_id),
    )
    orphan = app_data_dir() / "profiles" / "claude-deadbeef"
    orphan.mkdir(parents=True)

    config = Config()
    config.browser_accounts.append(BrowserAccount(id="codex-12345678", kind="codex"))
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.browser_data_clear_requested) as signal:
        _button(dialog, "clear_browser_data_btn").click()

    ids = signal.args[0]
    assert {"claude", "codex", "opencode_go"} <= set(ids), "a fixed id was missed"
    assert "codex-12345678" in ids, "a configured account was missed"
    assert "claude-deadbeef" in ids, "a profile on disk was missed"
    # Every id whose profile is going is logged out of the credential store
    # at the click: that is a keyring write, nothing holds it open, and it is
    # the part that matters.
    assert sorted(cleared) == sorted(ids)
    assert orphan.is_dir(), "the dialog deleted a profile directory itself"


def test_clear_all_browser_data_says_when_a_busy_profile_goes(qtbot, monkeypatch):
    """What the user is told has to match what the app does.

    The deletion of a profile that is mid-scrape is deferred, and both
    deferral lists are recorded in `config.json`, so the honest answer is
    "when that refresh finishes, or at the next start". While the clear list
    was in memory only the completion box said the profiles were being
    removed and a quit could silently leave one - with its live session
    cookie - on disk.
    """
    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "question",
        lambda *a, **k: settings_dialog.QMessageBox.StandardButton.Yes,
    )
    said: list[str] = []
    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "information",
        lambda parent, title, text, *a, **k: said.append(text),
    )
    monkeypatch.setattr(
        settings_dialog, "set_provider_cookie", lambda account_id, value: None
    )
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    _button(dialog, "clear_browser_data_btn").click()

    assert said, "the click said nothing at all"
    assert "next start" in said[0], said[0]


def test_clear_all_browser_data_says_what_it_left_behind(qtbot, monkeypatch):
    """Names on disk are not bounded by anything the app generates.

    `purge_profile` refuses any that the id rule rejects, which is the
    containment guarantee and is exactly right - but it refuses them deep in
    the App, one at a time, while the button promised to "delete every
    account's saved cookie and embedded-browser profile". The count comes
    back to the dialog, which says the folders were left alone; the names
    themselves never reach the message or the log.

    The filter is on the half that deletes directories, and only that half.
    A stored cookie is a keyring entry with no containment question to
    answer, and this is the one button whose whole promise is "everything",
    so every name on disk is still cleared - which is what it did before the
    filter existed.
    """
    from aigauge.config import app_data_dir

    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "question",
        lambda *a, **k: settings_dialog.QMessageBox.StandardButton.Yes,
    )
    said: list[str] = []
    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "information",
        lambda parent, title, text, *a, **k: said.append(text),
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        settings_dialog,
        "set_provider_cookie",
        lambda account_id, value: cleared.append(account_id),
    )
    profiles = app_data_dir() / "profiles"
    (profiles / "claude-deadbeef").mkdir(parents=True)
    (profiles / "not an id").mkdir()
    (profiles / "also.bad!").mkdir()

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    with qtbot.waitSignal(dialog.browser_data_clear_requested) as signal:
        _button(dialog, "clear_browser_data_btn").click()

    ids = signal.args[0]
    assert "claude-deadbeef" in ids, "a usable leftover was not swept"
    assert not [one for one in ids if " " in one or "!" in one], (
        "an unusable directory name was handed to the purge path"
    )
    assert "2 folder(s)" in said[0], said[0]
    assert "not an id" not in said[0], "the message named a directory on disk"
    assert {"not an id", "also.bad!"} <= set(cleared), (
        "the sweep's filter narrowed the keyring pass too"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlink privilege")
def test_clear_all_browser_data_does_not_delete_through_a_link(qtbot, monkeypatch):
    """The one entry in `profiles/` that deletes something that is not it.

    A link inside `profiles/` pointing at another profile resolves *inside*
    the root, so it passed the containment half and was emitted - and
    `purge_profile` then rmtree's the target. The App's live-scrape deferral
    is keyed on the link's own name, so the account it aliases is deferred
    and deleted in the same heartbeat, through the alias. A link to the root
    itself is the other half: emitted, counted as removed, and refused by
    `purge_profile` where nobody reads the reason.

    Driven through the real button, because it is the count in the
    completion box that was wrong in the "we deleted it" direction.
    """
    from aigauge.config import app_data_dir

    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "question",
        lambda *a, **k: settings_dialog.QMessageBox.StandardButton.Yes,
    )
    said: list[str] = []
    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "information",
        lambda parent, title, text, *a, **k: said.append(text),
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        settings_dialog,
        "set_provider_cookie",
        lambda account_id, value: cleared.append(account_id),
    )
    profiles = app_data_dir() / "profiles"
    (profiles / "claude-deadbeef").mkdir(parents=True)
    try:
        (profiles / "alias").symlink_to(profiles / "claude-deadbeef", True)
        (profiles / "selfroot").symlink_to(profiles, True)
    except (OSError, NotImplementedError):  # pragma: no cover - CI/Windows
        pytest.skip("this filesystem does not allow symlinks")

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    with qtbot.waitSignal(dialog.browser_data_clear_requested) as signal:
        _button(dialog, "clear_browser_data_btn").click()

    ids = signal.args[0]
    assert "claude-deadbeef" in ids, "a usable leftover was not swept"
    assert "alias" not in ids, "the sweep emitted a link to another profile"
    assert "selfroot" not in ids, "the sweep emitted a link to the root"
    assert "2 folder(s)" in said[0], said[0]
    # The keyring half is unfiltered on purpose: a stored cookie has no
    # containment question to answer and this button promises everything.
    assert {"alias", "selfroot"} <= set(cleared)


def test_the_settings_dialog_no_longer_deletes_profiles_itself(
    qtbot, monkeypatch
):
    """Both routes out of this dialog hand the directory to the App.

    This was an `inspect.getsource` substring test. `purge_profile` releases
    the cached `QWebEngineProfile` and rmtree's its directory; the dialog is
    modeless and a refresh cycle runs every five minutes, so only the App -
    which knows what is in flight - may run it. Driven here instead, through
    both buttons that used to: the module-level function is watched, and
    neither the removal nor the clear-all calls it.
    """
    import aigauge.webview.profile as profile_module
    from aigauge.config import app_data_dir

    called: list[str] = []
    monkeypatch.setattr(profile_module, "purge_profile", called.append)
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "question",
        lambda *a, **k: settings_dialog.QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        settings_dialog.QMessageBox, "information", lambda *a, **k: None
    )
    monkeypatch.setattr(
        settings_dialog, "set_provider_cookie", lambda account_id, value: None
    )
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)
    dialog._add_browser_account("claude")  # noqa: SLF001
    account_id = dialog._browser_accounts[-1].id  # noqa: SLF001
    profile_dir = app_data_dir() / "profiles" / account_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Cookies").write_bytes(b"SQLite format 3\x00")

    _button(dialog, "clear_browser_data_btn").click()
    dialog._remove_browser_account(account_id)  # noqa: SLF001
    dialog.apply_to(config)

    assert called == [], "the settings dialog is deleting QtWebEngine profiles again"
    assert profile_dir.is_dir(), "the dialog deleted a profile the App may be using"
    # The spy above is bound on the defining module, so it sees a call made
    # through it or through a function-local import. The one shape it cannot
    # see is a module-level `from .webview.profile import purge_profile`,
    # which binds before any patch - so that name must simply not be here.
    assert not hasattr(settings_dialog, "purge_profile"), (
        "the settings dialog imported purge_profile again"
    )


def test_one_dialog_session_asks_the_app_for_a_profile_once(qtbot, monkeypatch):
    """Clearing all browser data and removing an account are two calls.

    The clear goes to the App at the button and the removal list at OK, so
    an account removed in the same dialog session travelled both routes and
    reached `purge_profile` twice - once for a directory that was already
    gone. Nothing broke (it is idempotent and path-guarded), but the two
    routes are also two deferral lists in the App, and a blocked id sat on
    both and was logged by both at every heartbeat.

    The clear-all set is the whole answer: the button asks for every
    configured account, every fixed id and every usable name in `profiles/`,
    so a row removed before or after the click is always inside it.
    """
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(
        settings_dialog.QMessageBox,
        "question",
        lambda *a, **k: settings_dialog.QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        settings_dialog.QMessageBox, "information", lambda *a, **k: None
    )
    monkeypatch.setattr(
        settings_dialog, "set_provider_cookie", lambda account_id, value: None
    )
    config = Config()
    dialog = SettingsDialog(config)
    qtbot.addWidget(dialog)
    dialog._add_browser_account("claude")  # noqa: SLF001
    account_id = dialog._browser_accounts[-1].id  # noqa: SLF001

    emitted: list[list[str]] = []
    dialog.browser_data_clear_requested.connect(emitted.append)
    _button(dialog, "clear_browser_data_btn").click()
    dialog._remove_browser_account(account_id)  # noqa: SLF001
    dialog.apply_to(config)

    assert account_id in emitted[0], "the clear did not cover the account"
    assert dialog.removed_profile_ids == [], (
        "the App was asked to delete a profile it had just been asked to clear"
    )


# --- Size and scrolling ----------------------------------------------------


def _tabs(dialog: SettingsDialog):
    from PyQt6.QtWidgets import QTabWidget

    tabs = dialog.findChild(QTabWidget)
    assert tabs is not None
    return tabs


def _tab_index(dialog: SettingsDialog, title: str) -> int:
    tabs = _tabs(dialog)
    for i in range(tabs.count()):
        if tabs.tabText(i) == title:
            return i
    raise AssertionError(f"no {title} tab")


def _settled(qtbot, scroll) -> None:
    """Wait for a scroll area to finish re-laying its page.

    A tab switch or a resize can bring the vertical bar in, and that narrows
    the viewport by the bar's extent (774 -> 764 on the Windows runner) and
    posts a ``LayoutRequest``. Until that is delivered the page is still at
    its old width *and* the horizontal range has not been recomputed, so a
    reading taken there says "wider than its viewport, with no bar" about a
    scroll area that is simply mid-layout. One ``qtbot.wait(0)`` is one
    event-loop turn too few. Offscreen on Linux the pages fit the viewport,
    the bar never flips, and the window never opens - which is why this only
    ever failed on Windows.
    """
    try:
        qtbot.waitUntil(
            lambda: scroll.widget().width() <= scroll.viewport().width()
            or scroll.horizontalScrollBar().maximum() > 0,
            timeout=2000,
        )
    except TimeoutError:
        # Genuinely clipped with no bar: let the caller's assertion say which
        # page it was and by how much.
        pass


def _vertical_settled(qtbot, scroll) -> None:
    """The vertical twin of `_settled`, for a reading of `maximum()`.

    A tab switch posts a ``LayoutRequest`` and, until it is delivered, the
    page inside the area is still at the size it had - so ``maximum()`` can
    read 0 for a page that does not fit, or a range for one that does. One
    ``qtbot.wait(0)`` is one turn too few; offscreen on Linux it happens to be
    enough, which is exactly how the same reading on the other axis reached a
    Windows runner before anyone saw it.
    """
    try:
        qtbot.waitUntil(
            lambda: scroll.widget().height() <= scroll.viewport().height()
            or scroll.verticalScrollBar().maximum() > 0,
            timeout=2000,
        )
    except TimeoutError:
        # Let the caller's own assertion name the page and the number.
        pass


def test_the_microsoft_tab_no_longer_sets_the_dialog_floor(qtbot):
    """Wrapping the pages is what did it, not a page that got smaller.

    Before: the dialog's minimumSizeHint() was 519x1112, of which the
    Microsoft page's own 1015-px minimum was nearly all. Measured after:
    271x155.
    """
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)

    assert dialog.minimumSizeHint().height() < 300

    microsoft = _tabs(dialog).widget(_tab_index(dialog, "Microsoft"))
    assert microsoft.widget().sizeHint().height() > 1000, (
        "the page shrank instead of the scroll area absorbing it"
    )


def test_every_tab_page_scrolls(qtbot):
    """The guard for any tab added later: addTab with a bare page fails here."""
    from PyQt6.QtWidgets import QScrollArea

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    tabs = _tabs(dialog)

    assert tabs.count() == 6
    for i in range(tabs.count()):
        page = tabs.widget(i)
        assert isinstance(page, QScrollArea), tabs.tabText(i)
        assert page.widgetResizable()
        # Both axes as-needed. AlwaysOff on the horizontal did not make a
        # page fit - it hid the bar and left the page clipped with the range
        # unreachable.
        assert (
            page.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        assert (
            page.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        # One wheel notch moves the same distance here as on the panel's tile
        # area: these pages kept Qt's default 20 px while the widget used 42.
        step = wheel_step(page)
        assert step > 20, "the shared step is Qt's own default"
        assert page.verticalScrollBar().singleStep() == step
        assert page.horizontalScrollBar().singleStep() == step


def test_microsoft_scrolls_at_the_default_size_and_general_does_not(qtbot):
    """maximum(), not isVisible(): macOS overlay bars are zero-width at rest,
    and a tab that has never been current has not been laid out."""
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    with qtbot.waitExposed(dialog):
        dialog.show()
    tabs = _tabs(dialog)

    ranges = {}
    for i in range(tabs.count()):
        tabs.setCurrentIndex(i)
        _vertical_settled(qtbot, tabs.widget(i))
        ranges[tabs.tabText(i)] = tabs.widget(i).verticalScrollBar().maximum()

    assert ranges["Microsoft"] > 0, ranges
    assert ranges["General"] == 0, ranges


def test_the_default_size_tracks_the_general_page(qtbot):
    """The landing page fits; the tallest page does not drag the window up."""
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    with qtbot.waitExposed(dialog):
        dialog.show()
    tabs = _tabs(dialog)

    general_index = _tab_index(dialog, "General")
    tabs.setCurrentIndex(general_index)
    _vertical_settled(qtbot, tabs.widget(general_index))
    assert tabs.widget(general_index).verticalScrollBar().maximum() == 0

    microsoft = tabs.widget(_tab_index(dialog, "Microsoft"))
    assert dialog.height() < microsoft.widget().sizeHint().height() / 2


@pytest.mark.parametrize(
    "content,chrome,floor,ceiling,expected",
    [
        (486, 103, 420, 720, 589),
        (40, 97, 420, 720, 420),
        (1765, 97, 420, 360, 360),
        (1765, 97, 100, 360, 360),
    ],
    ids=["tall", "short", "capped", "squeezed"],
)
def test_the_height_clamp_has_a_floor_and_a_ceiling(
    content, chrome, floor, ceiling, expected
):
    """The two ends are unreachable offscreen: one 800x800 screen, one font.

    `capped` is the disagreement: a 420 floor against a 360 ceiling is a
    400-px work area at 200% display scale, and the ceiling has to win or the
    dialog is sized taller than the desktop before it is ever painted.
    """
    assert settings_dialog._dialog_height(content, chrome, floor, ceiling) == expected


def test_the_default_height_is_the_measured_need_plus_the_slack(qtbot):
    """The regression that reached CI, pinned from both sides.

    The first cut read General's ``sizeHint()`` after one top-level
    ``activate()``; a nested row was still serving a hint cached before its
    combo box was styled, General under-read by 10 px, and the landing page
    opened with 7 px of scroll range on Windows and 6 on macOS (offscreen
    Linux passed by 5 px of font luck). Now: no range, and the default is
    within the declared slack plus the style's pane rounding of the smallest
    height that shows none - so a stale hint fails here whichever way it
    errs.
    """
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    estimate = dialog.height()
    with qtbot.waitExposed(dialog):
        dialog.show()
    tabs = _tabs(dialog)
    general_index = _tab_index(dialog, "General")
    tabs.setCurrentIndex(general_index)
    qtbot.wait(0)
    general = tabs.widget(general_index)
    assert general.verticalScrollBar().maximum() == 0, dialog._height_terms
    # The estimate measures at the width the scroll area decides with, bar
    # reserved - never wider than the viewport showEvent found. (A page whose
    # own minimum is wider is measured at that minimum inside _page_height,
    # as the area lays it out; the estimate's width stays the viewport's.)
    terms = dialog._height_terms
    assert terms["page_w"] <= terms["viewport_w_at_show"], terms

    # The show-time measurement may grow the dialog where a platform's fonts
    # or style land away from their hints; more than this and the estimate
    # itself is wrong. Any growth is reported so a platform that needs it
    # shows up in the run's warnings with both sets of terms.
    grew = dialog.height() - estimate
    assert 0 <= grew <= 8, dialog._height_terms
    if grew:
        warnings.warn(
            f"the pre-show height estimate was {grew} px short here: "
            f"{dialog._height_terms}",
            stacklevel=1,
        )

    low, high = dialog.minimumHeight(), dialog.height()
    while low < high:
        mid = (low + high) // 2
        dialog.resize(dialog.width(), mid)
        qtbot.wait(0)
        if general.verticalScrollBar().maximum() == 0:
            high = mid
        else:
            low = mid + 1
    smallest = low
    assert smallest > dialog.minimumHeight(), "the search never engaged"
    pane_rounding = 4
    assert 0 <= estimate + grew - smallest <= (
        settings_dialog._DIALOG_HEIGHT_SLACK + pane_rounding
    ), (estimate, grew, smallest, dialog._height_terms)


def test_the_default_width_fits_the_widest_page(qtbot, monkeypatch):
    """No page is clipped, whatever the fonts make of its minimum.

    Offscreen on Linux every page's minimum fits in 620 and the default
    holds; the Windows runner's fonts run a third wider (General 612 px
    against 454) and the dialog opens as wide as the 800-px work area
    allows. So the assertions are the rule's, not a number's: never
    narrower than the default, every page shown no wider than its viewport
    unless the work area is what bounds the dialog, and with the default
    forced down to 300 the rule alone still opens wide enough - the 560
    floor underneath.
    """

    def every_page_fits(dialog: SettingsDialog) -> None:
        tabs = _tabs(dialog)
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            qtbot.wait(0)
            scroll = tabs.widget(i)
            _settled(qtbot, scroll)
            clipped = scroll.widget().width() > scroll.viewport().width()
            # No exemption for the work-area bound any more: where the screen
            # is what stops the dialog from being wide enough, the page's own
            # horizontal bar has to carry the rest. Clipped with no bar is the
            # one answer that is never allowed.
            assert not clipped or scroll.horizontalScrollBar().maximum() > 0, (
                tabs.tabText(i),
                dialog._height_terms,
            )

    fits = SettingsDialog(Config())
    qtbot.addWidget(fits)
    available = fits.screen().availableGeometry().width()
    assert fits.width() >= min(settings_dialog._DIALOG_DEFAULT_W, available)
    with qtbot.waitExposed(fits):
        fits.show()
    every_page_fits(fits)

    monkeypatch.setattr(settings_dialog, "_DIALOG_DEFAULT_W", 300)
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    asked = dialog._height_terms["asked_w"]
    assert asked > 300, "the rule did not engage"
    # The 560 floor is a separate rule and still applies underneath.
    assert dialog.width() == max(asked, settings_dialog._DIALOG_MIN_W)
    # And `width` is what the window got, not what the derivation asked for.
    assert dialog._height_terms["width"] == dialog.width()
    with qtbot.waitExposed(dialog):
        dialog.show()
    every_page_fits(dialog)


def test_the_screen_fraction_is_the_effective_ceiling(qtbot, monkeypatch):
    """Including the minimum size, which used to out-rank it.

    At 200% display scale the logical work area is 400 px high, the ceiling is
    360, and `setMinimumSize(..., min(420, 400))` floored the dialog at 400 -
    so the documented 90% was the one number the code did not enforce, and the
    pre-show size was 20 px taller than the whole desktop. The fraction is
    forced down here rather than the screen faked: offscreen has one 800x800
    screen, and a ceiling under the 420 floor is the only thing that matters.
    """
    monkeypatch.setattr(settings_dialog, "_DIALOG_SCREEN_FRACTION", 0.4)
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    available = dialog.screen().availableGeometry()
    ceiling = int(available.height() * 0.4)
    assert ceiling < settings_dialog._DIALOG_MIN_H, "the case did not engage"

    assert dialog.minimumHeight() == ceiling
    assert dialog.height() <= ceiling, dialog._height_terms

    with qtbot.waitExposed(dialog):
        dialog.show()
    qtbot.wait(0)
    assert dialog.height() <= ceiling, dialog._height_terms


def test_no_page_is_clipped_at_the_dialog_floor(qtbot, monkeypatch):
    """The floor a user can drag to is the same widest-page rule as the
    default, so the 560 constant can no longer put a page 50-80 px off the
    right-hand edge - which is what it did on the Windows fonts, where
    General's minimum is 612: measured at 480, Claude clipped by 46 px with
    the range sitting there and the bar policy hiding it.

    The constant is forced below the rule in the second half, because
    offscreen on Linux every page's minimum already fits inside 560 and the
    rule would otherwise never engage here.
    """

    def nothing_is_clipped_at_the_floor(dialog: SettingsDialog) -> None:
        available = dialog.screen().availableGeometry().width()
        tabs = _tabs(dialog)
        dialog.resize(dialog.minimumWidth(), dialog.height())
        qtbot.wait(0)
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            qtbot.wait(0)
            scroll = tabs.widget(i)
            _settled(qtbot, scroll)
            bar = scroll.horizontalScrollBar()
            if dialog.minimumWidth() < available:
                assert bar.maximum() == 0, (tabs.tabText(i), scroll.widget().width())
            else:
                # A work area narrower than the pages need: the floor gave way
                # to the screen, and the bar is what makes the rest reachable.
                assert (
                    scroll.widget().width() <= scroll.viewport().width()
                    or bar.maximum() > 0
                ), tabs.tabText(i)

    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    with qtbot.waitExposed(dialog):
        dialog.show()
    nothing_is_clipped_at_the_floor(dialog)

    monkeypatch.setattr(settings_dialog, "_DIALOG_MIN_W", 300)
    narrow = SettingsDialog(Config())
    qtbot.addWidget(narrow)
    available = narrow.screen().availableGeometry().width()
    assert narrow.minimumWidth() > min(300, available), (
        "the floor ignored the widest page"
    )
    with qtbot.waitExposed(narrow):
        narrow.show()
    nothing_is_clipped_at_the_floor(narrow)


def test_page_height_measures_at_the_pages_own_minimum_when_wider(qtbot):
    """The Windows case, reproduced: a page whose minimum is wider than the
    viewport is laid out at that minimum, so that is where its height is
    read - never at a narrower width that would wrap its labels more."""
    from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

    page = QWidget()
    qtbot.addWidget(page)
    layout = QVBoxLayout(page)
    wide = QLabel("a row that sets the page's minimum width")
    wide.setMinimumWidth(500)
    layout.addWidget(wide)
    label = QLabel("word " * 60)
    label.setWordWrap(True)
    layout.addWidget(label)
    layout.activate()

    minimum_w = page.minimumSizeHint().width()
    assert minimum_w >= 500
    at_minimum = page.heightForWidth(minimum_w)
    assert page.heightForWidth(300) > at_minimum, (
        "the label should wrap more at 300 than at the minimum, or this "
        "proves nothing"
    )
    assert settings_dialog._page_height(page, 300) == at_minimum
    assert settings_dialog._page_height(page, minimum_w + 200) <= at_minimum


def test_a_short_estimate_is_corrected_before_the_first_paint(qtbot, monkeypatch):
    """The show-time measurement, forced to engage.

    Offscreen the estimate is exact and ``showEvent`` measures without
    resizing, so the correction is exercised by making the estimate wrong on
    purpose: 40 px of negative slack opens the dialog short, and the first
    ``showEvent`` grows it back to a height at which General shows no bar.
    """
    monkeypatch.setattr(settings_dialog, "_DIALOG_HEIGHT_SLACK", -40)
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    short = dialog.height()
    with qtbot.waitExposed(dialog):
        dialog.show()
    general = _tabs(dialog).widget(_tab_index(dialog, "General"))
    qtbot.wait(0)

    assert dialog.height() > short, dialog._height_terms
    # Nothing capped this one, so the two terms agree: what the measurement
    # asked for and what the window got.
    assert dialog._height_terms["grew"] == dialog.height() - short
    assert dialog._height_terms["deficit"] == dialog._height_terms["grew"]
    assert general.verticalScrollBar().maximum() == 0, dialog._height_terms
    assert dialog._fitted_on_show

    # Once only: a later show (the dialog is modal and re-created each time,
    # but a hide/show cycle must not keep growing it).
    grown = dialog.height()
    dialog.hide()
    with qtbot.waitExposed(dialog):
        dialog.show()
    assert dialog.height() == grown


def test_the_show_time_terms_say_what_was_asked_for_and_what_was_got(
    qtbot, monkeypatch
):
    """`grew` was the deficit, i.e. the measurement's question, not the
    window's answer. They differ whenever the resize is capped: at 200 %
    display scale the terms read `grew: 242` while the dialog went 360 to 360,
    because the screen ceiling held it. A maximum height stands in for that
    ceiling here, since an offscreen run has one 800 x 800 screen."""
    monkeypatch.setattr(settings_dialog, "_DIALOG_HEIGHT_SLACK", -40)
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    short = dialog.height()
    dialog.setMaximumHeight(short)
    with qtbot.waitExposed(dialog):
        dialog.show()
    qtbot.wait(0)

    assert dialog.height() == short, "the cap did not hold"
    assert dialog._height_terms["deficit"] > 0, "the measurement found nothing"
    assert dialog._height_terms["grew"] == 0, "a capped resize reported growth"


def test_activate_layouts_refreshes_a_nested_layouts_stale_hint(qtbot):
    """The mechanism on its own, with no dialog in the way.

    A combo box in a row layout inside an inner widget inside a group box's
    grid: after the page's top-level ``activate()`` has cached everything,
    growing the combo box invalidates the inner widget's layout and posts it a
    ``LayoutRequest`` that a hidden widget never handles - the page's own
    layout keeps its cache, and a second top-level ``activate()`` returns
    early on its raised flag.

    Two widgets deep, not one, so the tree has something to order at all -
    but this test is about the *outcome*, and measured, the outcome is the
    same either way round: Qt's hint caches invalidate upward on their own, so
    the page's hint comes back fresh whichever order the layouts are
    activated in. The order itself is pinned directly, in the test below.
    """
    from PyQt6.QtWidgets import (
        QComboBox,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QVBoxLayout,
        QWidget,
    )

    page = QWidget()
    qtbot.addWidget(page)
    outer = QVBoxLayout(page)
    group = QGroupBox("Group")
    grid = QGridLayout(group)
    inner = QWidget()
    inner_layout = QVBoxLayout(inner)
    row = QHBoxLayout()
    combo = QComboBox()
    combo.addItem("one")
    row.addWidget(combo)
    inner_layout.addLayout(row)
    grid.addWidget(inner, 0, 0)
    outer.addWidget(group)
    outer.activate()
    before = page.sizeHint().height()

    combo.setMinimumHeight(combo.sizeHint().height() + 40)
    outer.activate()
    assert page.sizeHint().height() == before, (
        "the top-level activate() alone now refreshes the nested row, so the "
        "deepest-first pass is no longer load-bearing - reconsider it"
    )

    settings_dialog._activate_layouts(page)
    assert page.sizeHint().height() >= before + 40


def test_activate_layouts_visits_descendants_before_their_ancestors(qtbot):
    """The pass's documented contract, asserted as an order.

    Not through an outcome: measured on a three-deep tree, dropping
    ``reversed`` leaves every size hint exactly where the deepest-first pass
    does, because a layout's ``invalidate()`` already walks up to the
    top-level one. What the order buys is the geometry each ``activate()``
    assigns - a parent laid out against a stale child hint stays laid out that
    way, since nothing comes back to it - so the promise is worth keeping and
    worth pinning, and an outcome assertion cannot tell the two apart.
    """
    from PyQt6.QtWidgets import QComboBox, QGroupBox, QVBoxLayout, QWidget

    visited: list[str] = []

    class _Recording(QVBoxLayout):
        def __init__(self, name, parent=None):
            super().__init__(parent)
            self._name = name

        def activate(self):  # noqa: N802 - Qt override
            visited.append(self._name)
            return super().activate()

    page = QWidget()
    qtbot.addWidget(page)
    outer = _Recording("page", page)
    group = QGroupBox("Group")
    group_layout = _Recording("group", group)
    inner = QWidget()
    inner_layout = _Recording("inner", inner)
    inner_layout.addWidget(QComboBox())
    group_layout.addWidget(inner)
    outer.addWidget(group)

    settings_dialog._activate_layouts(page)

    assert {"inner", "group", "page"} <= set(visited), visited
    assert visited.index("inner") < visited.index("group") < visited.index("page")


def test_the_dialog_does_not_remember_its_size(qtbot):
    """No settings-window field exists and none is to be added: the dialog is
    sized from its content every time it opens."""
    config = Config()
    first = SettingsDialog(config)
    qtbot.addWidget(first)
    original_height = first.height()

    before = set(Config().model_dump())
    first.resize(900, 900)
    first.apply_to(config)
    assert set(config.model_dump()) == before
    assert "settings" not in str(config.model_dump().get("window", {}))

    second = SettingsDialog(config)
    qtbot.addWidget(second)
    assert second.height() == original_height


def test_every_named_field_survives_the_wrapping(qtbot):
    """findChild is recursive, so it sees through a scroll area - but a widget
    left behind by a re-parent would not be inside one."""
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    scrolls = dialog._page_scrolls  # noqa: SLF001
    assert len(scrolls) == 6

    named = [
        "claude_signin_btn",
        "codex_paste_cookie_btn",
        "opencode_go_signin_btn",
        "azure_colors_btn",
        "rescan_meters_btn",
    ]
    for name in named:
        widget = _button(dialog, name)
        assert any(
            scroll.widget().isAncestorOf(widget) for scroll in scrolls
        ), f"{name} is not inside any tab page"

    for field in ("azure_subscription", "opencode_go_url", "gh_quota"):
        widget = getattr(dialog, field)
        assert any(
            scroll.widget().isAncestorOf(widget) for scroll in scrolls
        ), f"{field} is not inside any tab page"


def test_a_wrapped_page_does_not_paint_qts_light_background(qtbot):
    """The trap the widget's tile scroll area already fell into once.

    A QScrollArea's viewport has autoFillBackground() True and a Window
    background role, so without the descendant rule it paints #efefef through
    the dark dialog. Measured here: #1f2937 viewport, a #374151 track and a
    #4b5563 handle on the one tab that overflows, and nothing painted in that
    column on a tab that fits.
    """
    dialog = SettingsDialog(Config())
    qtbot.addWidget(dialog)
    with qtbot.waitExposed(dialog):
        dialog.show()
    tabs = _tabs(dialog)
    tabs.setCurrentIndex(_tab_index(dialog, "Microsoft"))
    qtbot.wait(0)

    image = dialog.grab().toImage()
    assert image.pixelColor(30, 200).name() == "#1f2937"

    scroll = tabs.currentWidget()
    bar = scroll.verticalScrollBar()
    assert bar.maximum() > 0
    top_left = bar.mapTo(dialog, bar.rect().topLeft())
    column = top_left.x() + bar.width() // 2
    assert image.pixelColor(column, top_left.y() + 12).name() == "#4b5563"
    assert (
        image.pixelColor(column, top_left.y() + bar.height() - 6).name() == "#374151"
    ), "the track is not visible behind the handle"

    tabs.setCurrentIndex(_tab_index(dialog, "General"))
    qtbot.wait(0)
    fitted = dialog.grab().toImage()
    assert fitted.pixelColor(column, 200).name() == "#1f2937", (
        "a bar was painted for a page with nothing hidden"
    )
