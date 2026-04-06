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


# =============================================================================
# Fixture files — config_valid.json, config_missing_field.json,
#                 config_wildcard_mime.json
#
# These are standalone JSON files in tests/fixtures/ used to document
# and test specific validation scenarios without constructing dicts in code.
# Each fixture is loaded directly and injected into a temporary plugin dir,
# making it obvious from the file name what scenario is being tested.
# =============================================================================

# Paths to the three fixture files
CONFIG_VALID         = FIXTURES_DIR / "config_valid.json"
CONFIG_MISSING_FIELD = FIXTURES_DIR / "config_missing_field.json"
CONFIG_WILDCARD_MIME = FIXTURES_DIR / "config_wildcard_mime.json"


class TestFixtureFiles:
    """
    Tests that use the three standalone fixture JSON files directly.

    Purpose of each fixture:
      config_valid.json         — identical to dummy_plugin/config.json.
                                  Proves the loader accepts a known-good file.
                                  Acts as the canonical reference for valid schema.

      config_missing_field.json — valid in all fields except plugin.contact,
                                  which is intentionally absent.
                                  Proves the loader rejects any missing field.

      config_wildcard_mime.json — valid in all fields except input.accepts,
                                  which contains ["*"] — a forbidden wildcard.
                                  Proves the MIME type allowlist is enforced.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _plugin_from_fixture(self, tmp_path: Path, fixture: Path) -> Path:
        """
        Create a temporary plugin directory whose config.json comes from
        the given fixture file. Adds a stub main.sh so the entrypoint check
        does not interfere with the validation scenario under test.
        Returns the parent plugins/ directory.
        """
        plugin_dir = tmp_path / "dummy_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "config.json").write_text(
            fixture.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (plugin_dir / "main.sh").write_text("#!/usr/bin/env bash\n")
        return tmp_path

    # ------------------------------------------------------------------
    # config_valid.json
    # ------------------------------------------------------------------

    def test_fixture_files_exist(self):
        """All three fixture files must be present in tests/fixtures/."""
        assert CONFIG_VALID.is_file(),         "config_valid.json missing"
        assert CONFIG_MISSING_FIELD.is_file(), "config_missing_field.json missing"
        assert CONFIG_WILDCARD_MIME.is_file(),  "config_wildcard_mime.json missing"

    def test_config_valid_is_accepted(self, tmp_path):
        """config_valid.json must pass the loader without errors."""
        plugins_dir = self._plugin_from_fixture(tmp_path, CONFIG_VALID)
        config = PluginLoader(plugins_dir=plugins_dir).load("dummy_plugin")
        assert isinstance(config, PluginConfig)

    def test_config_valid_matches_dummy_plugin(self):
        """
        config_valid.json must be structurally identical to
        dummy_plugin/config.json — it is the canonical reference.
        """
        valid   = json.loads(CONFIG_VALID.read_text())
        dummy   = json.loads((DUMMY_PLUGIN / "config.json").read_text())
        assert valid == dummy, (
            "config_valid.json has drifted from dummy_plugin/config.json. "
            "Keep them in sync — config_valid.json is the reference schema."
        )

    # ------------------------------------------------------------------
    # config_missing_field.json
    # ------------------------------------------------------------------

    def test_config_missing_field_is_rejected(self, tmp_path):
        """config_missing_field.json must be rejected — plugin.contact is absent."""
        plugins_dir = self._plugin_from_fixture(tmp_path, CONFIG_MISSING_FIELD)
        with pytest.raises(PluginError) as exc_info:
            PluginLoader(plugins_dir=plugins_dir).load("dummy_plugin")
        assert "contact" in str(exc_info.value), (
            "Error message should mention the missing field 'contact'"
        )

    def test_config_missing_field_has_correct_absent_field(self):
        """
        Verify the fixture actually has 'contact' missing and nothing else
        from the plugin section — keeps the fixture honest.
        """
        data = json.loads(CONFIG_MISSING_FIELD.read_text())
        plugin_section = data.get("plugin", {})
        assert "contact" not in plugin_section, (
            "config_missing_field.json should NOT have 'contact' field"
        )
        # All other required plugin fields must be present
        for field in ("name", "version", "description", "author", "license"):
            assert field in plugin_section, (
                f"config_missing_field.json unexpectedly lost field: {field}"
            )

    # ------------------------------------------------------------------
    # config_wildcard_mime.json
    # ------------------------------------------------------------------

    def test_config_wildcard_mime_is_rejected(self, tmp_path):
        """config_wildcard_mime.json must be rejected — input.accepts contains '*'."""
        plugins_dir = self._plugin_from_fixture(tmp_path, CONFIG_WILDCARD_MIME)
        with pytest.raises(PluginError) as exc_info:
            PluginLoader(plugins_dir=plugins_dir).load("dummy_plugin")
        error_msg = str(exc_info.value)
        assert "*" in error_msg or "invalid" in error_msg.lower(), (
            "Error message should mention the wildcard or invalid MIME type"
        )

    def test_config_wildcard_mime_has_wildcard(self):
        """
        Verify the fixture actually has '*' in input.accepts —
        keeps the fixture honest about what it is testing.
        """
        data = json.loads(CONFIG_WILDCARD_MIME.read_text())
        accepts = data.get("input", {}).get("accepts", [])
        assert "*" in accepts, (
            "config_wildcard_mime.json should have '*' in input.accepts"
        )
