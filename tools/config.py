"""Single source of truth for neon-legion runtime configuration.

Precedence (highest first):
    1. CLI flag (caller decides)
    2. Environment variable (legacy, kept for backwards compat)
    3. config.toml (project root, gitignored)
    4. config.example.toml (committed defaults)
    5. Code default (defined here)
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Map (toml dotted path) -> (env var name, type converter).
# Add an entry here when you introduce a new config key.
ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "backend.port": ("NEON_LEGION_PORT", int),
    "backend.host": ("NEON_LEGION_HOST", str),
    "backend.snapshot_interval_seconds": ("NEON_LEGION_SNAPSHOT_INTERVAL", int),
    "paths.snapshot_output": ("NEON_LEGION_SNAPSHOT_OUTPUT", str),
    "paths.salt_file": ("NEON_LEGION_SALT_FILE", str),
    "hooks.claude_projects_dir": ("CLAUDE_PROJECTS_DIR", str),
    # Legacy env vars kept for backwards compat (no toml key in example yet).
    # When a value is read via get_legacy_env(), it stays env-only.
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return base + overlay; overlay wins, nested dicts merge recursively."""
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Return the merged config dict. Cached for the process lifetime."""
    example = _read_toml(PROJECT_ROOT / "config.example.toml")
    real_path = PROJECT_ROOT / "config.toml"
    real = _read_toml(real_path) if real_path.exists() else {}
    return _deep_merge(example, real)


def get(dotted_key: str, default: Any = None, convert: type | None = None) -> Any:
    """Get config value by dotted key with env override.

    Order: env var (from ENV_OVERRIDES map) -> toml -> default.
    """
    env_var, env_type = ENV_OVERRIDES.get(dotted_key, (None, None))
    if env_var:
        raw = os.environ.get(env_var)
        if raw is not None:
            try:
                return env_type(raw)
            except (ValueError, TypeError):
                pass

    cfg: Any = load_config()
    for part in dotted_key.split("."):
        if isinstance(cfg, dict) and part in cfg:
            cfg = cfg[part]
        else:
            return default

    if convert is not None:
        try:
            return convert(cfg)
        except (ValueError, TypeError):
            return default
    return cfg


def get_legacy_env(name: str, default: Any = None, convert: type = str) -> Any:
    """Read env vars that are not in the toml schema yet."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return convert(raw)
    except (ValueError, TypeError):
        return default


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def read_role_providers(roles_path: Path | str) -> dict[str, str]:
    """Parse `[role.<name>].provider` → returns {role_name: agent_provider}.

    Uses `tomllib` (stdlib) so it correctly handles all TOML quote styles:
    double-quoted, single-quoted, and triple-quoted multiline literals.

    Returns empty dict on missing/unreadable file. Never raises.

    Used by `tools/slop_score.py` and `tools/disagreement_router.py` (both
    previously rolled their own double-quote-only regex parser — DeepSeek
    flagged that as a shared blind spot that breaks on single-quoted
    `roles.toml` files).
    """
    p = Path(roles_path)
    if not p.exists():
        return {}
    try:
        with p.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}

    out: dict[str, str] = {}
    roles_table = data.get("role")
    if not isinstance(roles_table, dict):
        return out
    for name, cfg in roles_table.items():
        if isinstance(cfg, dict):
            provider = cfg.get("provider")
            if isinstance(provider, str) and provider:
                out[str(name)] = provider
    return out
