from PyQt6.QtWidgets import QPushButton

from aigauge import settings_dialog
from aigauge.config import Config
from aigauge.providers.catalog import record_scan, scan_due
from aigauge.settings_dialog import SettingsDialog


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

def test_remove_account_purges_profile_dir(qtbot, monkeypatch):
    monkeypatch.setattr(settings_dialog, "set_start_at_login", lambda enabled: None)
    monkeypatch.setattr(settings_dialog, "set_provider_cookie", lambda key, value: None)
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

    assert not profile_dir.exists()


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
