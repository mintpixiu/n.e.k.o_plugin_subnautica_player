from __future__ import annotations

import inspect
import json
import re
import tomllib
from pathlib import Path

from plugin.plugins import subnautica_player
from plugin.plugins.subnautica_player import catalog

PLUGIN_DIR = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "subnautica_play",
    "subnautica_status",
    "subnautica_look",
    "subnautica_command",
    "subnautica_plan",
    "subnautica_ui",
    "subnautica_sequence",
    "subnautica_aim",
    "subnautica_stop",
    "subnautica_resume",
    "subnautica_plan_status",
    "subnautica_milestone",
    "subnautica_note",
    "subnautica_biome",
    "subnautica_selfcheck",
    "subnautica_games",
}

EXPECTED_ENTRIES = {
    "subnautica_play",
    "subnautica_stop",
    "subnautica_resume",
    "subnautica_status",
    "subnautica_look",
    "subnautica_selfcheck",
    "subnautica_plan_status",
}


def load_manifest():
    return tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))


def class_functions():
    return {
        name: value
        for name, value in vars(subnautica_player.SubnauticaPlayerPlugin).items()
        if inspect.isfunction(value)
    }


class TestManifest:
    def test_identity(self):
        data = load_manifest()
        plugin = data["plugin"]
        assert plugin["id"] == "subnautica_player"
        assert plugin["type"] == "plugin"
        assert plugin["version"] == "1.0.0"
        assert plugin["entry"] == "plugin.plugins.subnautica_player:SubnauticaPlayerPlugin"
        assert plugin["name"]

    def test_manual_start_and_safety_defaults(self):
        data = load_manifest()
        assert data["plugin_runtime"]["enabled"] is True
        assert data["plugin_runtime"]["auto_start"] is False
        cfg = data["subnautica"]
        assert cfg["autopilot_enabled"] is False
        assert cfg["dry_run"] is False
        assert cfg["require_foreground"] is True
        assert cfg["yield_on_human_input"] is True
        assert cfg["pause_hotkey_enabled"] is True
        assert cfg["start_in_game_required"] is True
        assert cfg["auto_resume_from_menu"] is False
        assert cfg["blind_frame_limit"] >= 2

    def test_config_table_is_not_plugin_id(self):
        data = load_manifest()
        assert "subnautica" in data
        assert "subnautica_player" not in data

    def test_no_runtime_dependencies(self):
        project = tomllib.loads((PLUGIN_DIR / "pyproject.toml").read_text(encoding="utf-8"))
        assert project["project"]["dependencies"] == []
        assert not (PLUGIN_DIR / "requirements.txt").exists()

    def test_docs_and_schema_exist(self):
        assert (PLUGIN_DIR / "docs" / "quickstart.md").is_file()
        schema = json.loads((PLUGIN_DIR / "config.schema.json").read_text(encoding="utf-8"))
        assert schema["type"] == "object"
        assert "subnautica" in schema["properties"]
        assert schema["properties"]["subnautica"]["type"] == "object"

    def test_i18n_declared_files_are_valid(self):
        data = load_manifest()
        i18n = data["plugin"]["i18n"]
        directory = PLUGIN_DIR / i18n["locales_dir"]
        assert (directory / f"{i18n['default_locale']}.json").is_file()
        for path in directory.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["plugin.name"]


class TestSurface:
    def test_tool_names(self):
        names = {
            meta.get("name")
            for fn in class_functions().values()
            if (meta := getattr(fn, "__neko_metadata__", {})).get("name", "").startswith("subnautica_")
        }
        assert names == EXPECTED_TOOLS

    def test_entries_are_async_and_unique(self):
        entries = {}
        for name, fn in class_functions().items():
            meta = getattr(fn, "__neko_metadata__", {})
            entry_id = meta.get("id")
            if entry_id and str(entry_id).startswith("subnautica_"):
                entries[entry_id] = fn
        assert set(entries) == EXPECTED_ENTRIES
        for entry in entries.values():
            assert inspect.iscoroutinefunction(entry)

    def test_tool_schemas_are_objects(self):
        for name, fn in class_functions().items():
            meta = getattr(fn, "__neko_metadata__", {})
            if not str(meta.get("name", "")).startswith("subnautica_"):
                continue
            schema = meta.get("parameters")
            assert isinstance(schema, dict), name
            assert schema.get("type") == "object", name
            assert "properties" in schema, name


class TestHygiene:
    def test_no_forbidden_imports(self):
        forbidden = re.compile(
            r"^\s*(?:import|from)\s+(cv2|opencv|pyautogui|pydirectinput|win32gui|"
            r"win32con|mss|dxcam|keyboard|pynput|psutil)\b",
            re.M,
        )
        for path in PLUGIN_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert forbidden.search(text) is None, path.name

    def test_no_game_launch_or_shell(self):
        for path in PLUGIN_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "subprocess" not in text, path.name
            assert "ShellExecute" not in text, path.name
            assert "steam://rungameid" not in text, path.name

    def test_no_startup_side_effects(self):
        text = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        assert "asyncio.run(" not in text
        assert "time.sleep(" not in text
        assert "threading.Thread" not in text


class TestCatalogue:
    def test_two_games_and_ids(self):
        assert {g.id for g in catalog.GAMES} == {"subnautica", "below_zero"}
        assert catalog.GAME_BY_ID["subnautica"].app_id == "264710"
        assert catalog.GAME_BY_ID["below_zero"].app_id == "848450"
        assert catalog.GAME_BY_ID["subnautica"].exe == "Subnautica.exe"
        assert catalog.GAME_BY_ID["below_zero"].exe == "SubnauticaZero.exe"

    def test_milestone_keys_unique(self):
        for game in catalog.GAMES:
            keys = [m.key for m in game.milestones]
            assert len(keys) == len(set(keys)), game.id
            assert keys

    def test_core_keys_present(self):
        for game in catalog.GAMES:
            for action in ("forward", "back", "left", "right", "up", "down",
                           "interact", "use", "pda", "slot1"):
                assert game.key(action), (game.id, action)

    def test_biomes_have_required_fields(self):
        for key, info in catalog.BIOMES.items():
            assert info["zh"] and info["depth"] and info["hazards"], key
