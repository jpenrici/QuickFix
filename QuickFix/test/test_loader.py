# =============================================================================
# QuickFix - tests/test_loader.py
# =============================================================================
# Tests for core/loader.py
#
# Run from project root:
#   python -m pytest tests/test_loader.py -v
# =============================================================================

import json
import sys
from pathlib import Path

import pytest

# Make core/ importable from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.loader import (
    PluginConfig,
    PluginError,
    PluginLoader,
    PluginNotFoundError,
)

# -----------------------------------------------------------------------------
# Fixtures paths
#
# All tests use FIXTURES_DIR / dummy_plugin exclusively.
# The real plugins/ directory is out of scope here — each plugin is
# responsible for its own tests under plugins/<name>/tests/run_tests.sh
# -----------------------------------------------------------------------------

FIXTURES_DIR  = Path(__file__).parent / "fixtures"
DUMMY_PLUGIN  = FIXTURES_DIR / "dummy_plugin"


def _load_valid() -> PluginConfig:
    loader = PluginLoader(plugins_dir=FIXTURES_DIR)
    return loader.load("dummy_plugin")


# =============================================================================
# Happy path
# =============================================================================

class TestValidPlugin:

    def test_load_returns_plugin_config(self):
        config = _load_valid()
        assert isinstance(config, PluginConfig)

    def test_plugin_name_matches_directory(self):
        config = _load_valid()
        assert config.plugin.name == "dummy_plugin"

    def test_plugin_dir_is_set(self):
        config = _load_valid()
        assert config.plugin_dir == DUMMY_PLUGIN

    def test_entrypoint_runtime(self):
        config = _load_valid()
        assert config.execution.runtime == "bash"
        assert config.execution.entrypoint == "main.sh"

    def test_overwrites_input_is_always_false(self):
        config = _load_valid()
        assert config.output.overwrites_input is False

    def test_accepts_mime_exact_match(self):
        loader = PluginLoader(plugins_dir=FIXTURES_DIR)
        config = loader.load("dummy_plugin")
        assert loader.accepts_mime(config, "text/plain") is True
        assert loader.accepts_mime(config, "image/png")  is False

    def test_discover_returns_list(self):
        loader = PluginLoader(plugins_dir=FIXTURES_DIR)
        plugins = loader.discover()
        assert isinstance(plugins, list)
        assert any(p.plugin.name == "dummy_plugin" for p in plugins)

    def test_discover_empty_dir(self, tmp_path):
        loader = PluginLoader(plugins_dir=tmp_path)
        assert loader.discover() == []

    def test_discover_nonexistent_dir(self, tmp_path):
        loader = PluginLoader(plugins_dir=tmp_path / "no_such_dir")
        assert loader.discover() == []


# =============================================================================
# Missing sections
# =============================================================================

class TestMissingSections:

    def _load_without(self, tmp_path: Path, section: str) -> None:
        base = json.loads((DUMMY_PLUGIN / "config.json").read_text())
        del base[section]
        plugin_dir = tmp_path / "bad_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text(json.dumps(base))
        (plugin_dir / "main.sh").write_text("#!/usr/bin/env bash\n")
        PluginLoader(plugins_dir=tmp_path).load("bad_plugin")

    @pytest.mark.parametrize("section", [
        "plugin", "execution", "sandbox", "input",
        "output", "requirements", "gui",
    ])
    def test_missing_section_raises(self, tmp_path, section):
        with pytest.raises(PluginError):
            self._load_without(tmp_path, section)


# =============================================================================
# Missing fields within sections
# =============================================================================

class TestMissingFields:

    def _load_without_field(self, tmp_path: Path, section: str, field: str) -> None:
        base = json.loads((DUMMY_PLUGIN / "config.json").read_text())
        del base[section][field]
        plugin_dir = tmp_path / "bad_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text(json.dumps(base))
        (plugin_dir / "main.sh").write_text("#!/usr/bin/env bash\n")
        PluginLoader(plugins_dir=tmp_path).load("bad_plugin")

    @pytest.mark.parametrize("section,field", [
        ("plugin",       "name"),
        ("plugin",       "version"),
        ("plugin",       "description"),
        ("plugin",       "author"),
        ("plugin",       "contact"),
        ("plugin",       "license"),
        ("execution",    "runtime"),
        ("execution",    "entrypoint"),
        ("execution",    "timeout_seconds"),
        ("execution",    "args_extra"),
        ("sandbox",      "engine"),
        ("sandbox",      "allow_network"),
        ("sandbox",      "allow_new_processes"),
        ("sandbox",      "writable_paths"),
        ("input",        "accepts"),
        ("input",        "max_size_mb"),
        ("input",        "encoding"),
        ("output",       "produces"),
        ("output",       "filename_suffix"),
        ("output",       "overwrites_input"),
        ("requirements", "system_binaries"),
        ("requirements", "min_free_disk_mb"),
        ("requirements", "os"),
        ("gui",          "has_own_window"),
        ("gui",          "dialog_tool"),
        ("gui",          "extra_input_required"),
    ])
    def test_missing_field_raises(self, tmp_path, section, field):
        with pytest.raises(PluginError):
            self._load_without_field(tmp_path, section, field)


# =============================================================================
# Forbidden values
# =============================================================================

class TestForbiddenValues:

    def _load_with(self, tmp_path: Path, section: str, field: str, value) -> None:
        base = json.loads((DUMMY_PLUGIN / "config.json").read_text())
        base[section][field] = value
        plugin_dir = tmp_path / "bad_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text(json.dumps(base))
        (plugin_dir / "main.sh").write_text("#!/usr/bin/env bash\n")
        PluginLoader(plugins_dir=tmp_path).load("bad_plugin")

    def test_wildcard_mime_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "input", "accepts", ["*"])

    def test_partial_mime_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "input", "accepts", ["text/*"])

    def test_unknown_runtime_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "execution", "runtime", "nodejs")

    def test_unknown_sandbox_engine_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "sandbox", "engine", "docker")

    def test_overwrites_input_true_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "output", "overwrites_input", True)

    def test_timeout_too_low_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "execution", "timeout_seconds", 0)

    def test_timeout_too_high_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "execution", "timeout_seconds", 301)

    def test_unknown_os_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "requirements", "os", "windows")

    def test_entrypoint_with_path_separator_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "execution", "entrypoint", "subdir/main.sh")

    def test_invalid_plugin_name_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "plugin", "name", "My Plugin!")

    def test_invalid_semver_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "plugin", "version", "v1.0")

    def test_writable_path_not_output_dir_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "sandbox", "writable_paths", ["/tmp"])

    def test_unknown_dialog_tool_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "gui", "dialog_tool", "kdialog")

    def test_empty_accepts_list_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "input", "accepts", [])

    def test_empty_description_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "plugin", "description", "Short")

    def test_suffix_without_underscore_rejected(self, tmp_path):
        with pytest.raises(PluginError):
            self._load_with(tmp_path, "output", "filename_suffix", "reversed")


# =============================================================================
# Structural errors
# =============================================================================

class TestStructuralErrors:

    def test_not_found_raises_plugin_not_found_error(self, tmp_path):
        loader = PluginLoader(plugins_dir=tmp_path)
        with pytest.raises(PluginNotFoundError):
            loader.load("nonexistent_plugin")

    def test_missing_config_json_raises(self, tmp_path):
        (tmp_path / "my_plugin").mkdir()
        loader = PluginLoader(plugins_dir=tmp_path)
        with pytest.raises(PluginNotFoundError):
            loader.load("my_plugin")

    def test_invalid_json_raises(self, tmp_path):
        plugin_dir = tmp_path / "bad_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text("{ not valid json }")
        loader = PluginLoader(plugins_dir=tmp_path)
        with pytest.raises(PluginError):
            loader.load("bad_plugin")

    def test_config_json_not_object_raises(self, tmp_path):
        plugin_dir = tmp_path / "bad_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text("[1, 2, 3]")
        loader = PluginLoader(plugins_dir=tmp_path)
        with pytest.raises(PluginError):
            loader.load("bad_plugin")

    def test_missing_entrypoint_file_raises(self, tmp_path):
        base = json.loads((DUMMY_PLUGIN / "config.json").read_text())
        plugin_dir = tmp_path / "dummy_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text(json.dumps(base))
        # main.sh intentionally not created
        loader = PluginLoader(plugins_dir=tmp_path)
        with pytest.raises(PluginError):
            loader.load("dummy_plugin")

    def test_name_mismatch_raises(self, tmp_path):
        base = json.loads((DUMMY_PLUGIN / "config.json").read_text())
        base["plugin"]["name"] = "different_name"
        plugin_dir = tmp_path / "dummy_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text(json.dumps(base))
        (plugin_dir / "main.sh").write_text("#!/usr/bin/env bash\n")
        loader = PluginLoader(plugins_dir=tmp_path)
        with pytest.raises(PluginError):
            loader.load("dummy_plugin")
