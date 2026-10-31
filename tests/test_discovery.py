from __future__ import annotations

from plugin.plugins.subnautica_player import catalog, discovery


def test_aliases_resolve_both_games():
    assert catalog.resolve_game("深海迷航").game.id == "subnautica"
    assert catalog.resolve_game("Subnautica").game.id == "subnautica"
    assert catalog.resolve_game("零度之下").game.id == "below_zero"
    assert catalog.resolve_game("below zero").game.id == "below_zero"
    assert catalog.resolve_game("Subnautica: Below Zero").game.id == "below_zero"


def test_unknown_and_empty_are_not_guessed():
    assert catalog.resolve_game("这不是游戏").game is None
    assert catalog.resolve_game("").game is None
    assert catalog.resolve_game("这不是游戏").reason == "unknown"


def test_subnautica_2_is_explicitly_unsupported():
    result = catalog.resolve_game("Subnautica 2")
    assert result.game is None
    assert result.reason == "unsupported_subnautica_2"
    assert catalog.resolve_game("深海迷航2").reason == "unsupported_subnautica_2"


def test_process_profile_by_exe_and_path():
    assert discovery._profile_from_process("Subnautica.exe", "").id == "subnautica"
    assert discovery._profile_from_process("SubnauticaZero.exe", "").id == "below_zero"
    assert discovery._profile_from_process(
        "Subnautica.exe",
        r"D:\games\SubnauticaZero\SubnauticaZero.exe",
    ).id == "below_zero"
    assert discovery._profile_from_process("chrome.exe", "") is None


def test_window_profile_requires_unity_and_title():
    unity = discovery.WindowInfo(
        hwnd=1, pid=2, title="Subnautica: Below Zero", visible=True,
        rect=(0, 0, 1920, 1080), class_name="UnityWndClass",
    )
    assert discovery._profile_from_window(unity).id == "below_zero"

    browser = discovery.WindowInfo(
        hwnd=3, pid=4, title="Subnautica - 深海迷航", visible=True,
        rect=(0, 0, 1280, 720), class_name="Chrome_WidgetWin_1",
    )
    assert discovery._profile_from_window(browser) is None

    unrelated = discovery.WindowInfo(
        hwnd=5, pid=6, title="Subnautica 2", visible=True,
        rect=(0, 0, 1280, 720), class_name="UnityWndClass",
    )
    # Subnautica 2 is intentionally not in the catalogue.
    assert discovery._profile_from_window(unrelated) is None


def test_physical_rect_with_no_window_is_safe():
    assert discovery.physical_window_rect(0) == (0, 0, 0, 0)


def test_window_usable_rejects_icon_sized_minimised_window():
    window = discovery.WindowInfo(
        hwnd=1, pid=1, title="Subnautica", visible=True,
        rect=(-32000, -32000, -31801, -31966), minimized=True,
    )
    assert not window.is_usable()
