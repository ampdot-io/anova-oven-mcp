#!/usr/bin/env python3
"""Configure this MCP server for Claude Code and/or Claude Desktop.

The script never copies Anova credentials into Claude configuration. The MCP
process continues to read its Firebase refresh token from macOS Keychain (or
another credential adapter configured for the library).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_SERVER_NAME = "anova-oven"
SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_server_command(root: Path) -> Path:
    candidates = (
        root / ".venv" / "bin" / "anova-oven-mcp",
        root / ".venv" / "Scripts" / "anova-oven-mcp.exe",
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    installed = shutil.which("anova-oven-mcp")
    if installed:
        return Path(installed).resolve()
    raise SystemExit(
        "Could not find anova-oven-mcp. Create .venv and install the server extra first."
    )


def default_desktop_config() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / (
            "claude_desktop_config.json"
        )
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise SystemExit("APPDATA is not set; pass --desktop-config explicitly.")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def mcp_entry(server_command: Path) -> dict[str, Any]:
    return {"command": str(server_command), "args": []}


def load_desktop_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(
            f"Refusing to modify invalid Claude Desktop JSON at {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise SystemExit(f"Claude Desktop configuration at {path} is not a JSON object.")
    servers = value.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise SystemExit(f"The mcpServers value in {path} is not a JSON object.")
    return value


def backup_path(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.backup-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.backup-{stamp}-{suffix}")
        suffix += 1
    return candidate


def write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def configure_desktop(
    *,
    config_path: Path,
    name: str,
    entry: dict[str, Any],
    remove: bool,
    dry_run: bool,
) -> None:
    config = load_desktop_config(config_path)
    servers = config.setdefault("mcpServers", {})
    assert isinstance(servers, dict)

    if remove:
        if name not in servers:
            print(f"Claude Desktop: {name!r} is not configured; nothing to remove.")
            return
        action = "remove"
        del servers[name]
    else:
        if servers.get(name) == entry:
            print(f"Claude Desktop: {name!r} already has the requested configuration.")
            return
        action = "update" if name in servers else "add"
        servers[name] = entry

    if dry_run:
        print(f"Claude Desktop dry run: would {action} {name!r} in {config_path}")
        if not remove:
            print(json.dumps(entry, indent=2))
        return

    backup: Path | None = None
    if config_path.exists():
        backup = backup_path(config_path)
        shutil.copy2(config_path, backup)
        backup.chmod(0o600)
    write_json_atomically(config_path, config)
    past_tense = {"add": "added", "update": "updated", "remove": "removed"}[action]
    print(f"Claude Desktop: {past_tense} {name!r} in {config_path}")
    if backup is not None:
        print(f"Claude Desktop backup: {backup}")
    print("Quit and reopen Claude Desktop, then check + > Connectors or Developer settings.")


def resolve_claude_command(value: str) -> str:
    if os.path.sep in value:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SystemExit(f"Claude Code executable is not runnable: {path}")
        return str(path)
    resolved = shutil.which(value)
    if not resolved:
        raise SystemExit("Claude Code was not found. Pass --claude-command or install it first.")
    return resolved


def run(command: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise SystemExit("Claude Code did not respond within 30 seconds.") from error
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise SystemExit(f"Command failed: {detail}")
    return result


def configure_code(
    *,
    claude_command: str,
    root: Path,
    scope: str,
    name: str,
    entry: dict[str, Any],
    remove: bool,
    replace: bool,
    dry_run: bool,
) -> None:
    add_command = [
        claude_command,
        "mcp",
        "add-json",
        "--scope",
        scope,
        name,
        json.dumps(entry, separators=(",", ":")),
    ]
    remove_command = [claude_command, "mcp", "remove", "--scope", scope, name]

    if dry_run:
        if remove:
            print(f"Claude Code dry run: would remove {name!r} at {scope!r} scope.")
        elif replace:
            print(f"Claude Code dry run: would replace {name!r} at {scope!r} scope.")
            print(json.dumps(entry, indent=2))
        else:
            print(f"Claude Code dry run: would add {name!r} at {scope!r} scope.")
            print(json.dumps(entry, indent=2))
        return

    existing = run([claude_command, "mcp", "get", name], cwd=root, check=False)
    exists = existing.returncode == 0

    if remove:
        if not exists:
            print(f"Claude Code: {name!r} is not configured; nothing to remove.")
            return
        run(remove_command, cwd=root)
        print(f"Claude Code: removed {name!r} from {scope!r} scope.")
        return

    if exists and not replace:
        print(
            f"Claude Code: {name!r} already exists. Left it unchanged; use --replace to update it."
        )
        return
    if exists:
        run(remove_command, cwd=root)
    run(add_command, cwd=root)
    print(f"Claude Code: configured {name!r} at {scope!r} scope.")
    verification = run([claude_command, "mcp", "get", name], cwd=root, check=False)
    if verification.returncode == 0:
        print("Claude Code: configuration is visible to the CLI.")
    else:
        print("Claude Code: configuration was written, but the CLI could not verify it.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Configure the local Anova Precision Oven MCP for Claude clients."
    )
    result.add_argument(
        "--target",
        choices=("code", "desktop", "both"),
        default="both",
        help="Configure Claude Code, Claude Desktop, or both (default: both).",
    )
    result.add_argument(
        "--scope",
        choices=("local", "user", "project"),
        default="user",
        help="Claude Code scope (default: user, available across projects).",
    )
    result.add_argument("--name", default=DEFAULT_SERVER_NAME)
    result.add_argument("--server-command", type=Path)
    result.add_argument("--claude-command", default="claude")
    result.add_argument("--desktop-config", type=Path)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing same-named Claude Code entry.",
    )
    result.add_argument("--remove", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    if args.replace and args.remove:
        raise SystemExit("--replace and --remove cannot be used together.")
    if not SERVER_NAME_PATTERN.fullmatch(args.name):
        raise SystemExit("--name may contain only letters, numbers, hyphens, and underscores.")

    root = project_root()
    if args.server_command is not None:
        server_command = args.server_command.expanduser().resolve()
    elif args.remove:
        # Removal only needs the configured name; keep it usable even after
        # the local virtual environment has already been deleted.
        server_command = Path("anova-oven-mcp")
    else:
        server_command = default_server_command(root)
    if not args.remove and (
        not server_command.is_file() or not os.access(server_command, os.X_OK)
    ):
        raise SystemExit(f"MCP executable is not runnable: {server_command}")
    entry = mcp_entry(server_command)

    if args.target in {"code", "both"}:
        claude_command = resolve_claude_command(args.claude_command)
        configure_code(
            claude_command=claude_command,
            root=root,
            scope=args.scope,
            name=args.name,
            entry=entry,
            remove=args.remove,
            replace=args.replace,
            dry_run=args.dry_run,
        )
    if args.target in {"desktop", "both"}:
        configure_desktop(
            config_path=(
                args.desktop_config.expanduser().resolve()
                if args.desktop_config is not None
                else default_desktop_config()
            ),
            name=args.name,
            entry=entry,
            remove=args.remove,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
