from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def load_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "configure_claude.py"
    spec = importlib.util.spec_from_file_location("configure_claude", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_desktop_config_merge_preserves_existing_servers_and_creates_backup(
    tmp_path: Path,
) -> None:
    module = load_script()
    config_path = tmp_path / "Claude" / "claude_desktop_config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {"existing": {"command": "existing-server"}},
                "preferences": {"theme": "dark"},
            }
        ),
        encoding="utf-8",
    )
    entry = {"command": "/example/anova-oven-mcp", "args": []}

    module.configure_desktop(
        config_path=config_path,
        name="anova-oven",
        entry=entry,
        remove=False,
        dry_run=False,
    )

    configured = json.loads(config_path.read_text(encoding="utf-8"))
    assert configured["mcpServers"]["existing"] == {"command": "existing-server"}
    assert configured["mcpServers"]["anova-oven"] == entry
    assert configured["preferences"] == {"theme": "dark"}
    assert config_path.stat().st_mode & 0o777 == 0o600
    backups = list(config_path.parent.glob("claude_desktop_config.json.backup-*"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_desktop_dry_run_does_not_create_config(tmp_path: Path) -> None:
    module = load_script()
    config_path = tmp_path / "Claude" / "claude_desktop_config.json"

    module.configure_desktop(
        config_path=config_path,
        name="anova-oven",
        entry={"command": "/example/anova-oven-mcp", "args": []},
        remove=False,
        dry_run=True,
    )

    assert not config_path.exists()
