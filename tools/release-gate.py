#!/usr/bin/env python
"""Release privacy gate: hard-fail scanner for private data leaks.

Different from tools/oss-sanitize.py, which transforms data. This gate only
detects violations and exits non-zero. Run it before pushing to a public
remote.

Usage:
    python tools/release-gate.py
    python tools/release-gate.py --json
    python tools/release-gate.py --quiet

Exit codes: 0 clean | 1 violations | 2 config or invocation error
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "tools" / "release-gate.toml"
ALLOW_MARKER_RE = re.compile(r"#\s*release-gate:\s*allow\b", re.IGNORECASE)
VALID_SEVERITIES = {"ok", "info", "warn", "fail"}


DEFAULT_CONFIG = {
    "scope": {
        "include_globs": [],
        "exclude_globs": [
            "LICENSE",
            "*.svg",
            "tests/**",
        ],
    },
    "patterns": {
        "customer_codenames": [],
        "allowed_emails": ["noreply@anthropic.com"],
        "allowed_absolute_paths": [],
    },
    "severity": {
        "forced_ignored": "fail",
        "api_token": "fail",
        "absolute_path": "warn",
        "email": "warn",
        "lan_address": "warn",
        "co_authored_by": "fail",
        "cyrillic_path": "warn",
        "tracker_jsonl_tracked": "fail",
        "private_prompts_tracked": "fail",
        "pycache_tracked": "fail",
        "backup_tracked": "fail",
        "log_tracked": "warn",
        "env_file_tracked": "fail",
        "customer_codename": "fail",
        "binary_skipped": "info",
    },
}


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
LAN_IP_RE = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])"
    r"(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})\b"
)
MDNS_RE = re.compile(
    r"(?<![\w.-])(?:[a-z0-9-]+\.)+local(?=[:/\s'\"\)\]\}]|$)",
    re.IGNORECASE,
)
WINDOWS_PATH_RE = re.compile(r"(?<![\w.-])[A-Za-z]:[\\/][^\s\"'<>`]+")
CYRILLIC_PATH_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z]:[\\/]|~/|/)"
    r"[^\s\"'<>`]*[\u0400-\u04ff][^\s\"'<>`]*"
)
TOKEN_PATTERNS = [
    re.compile(r"\bsk-or-v1-[A-Za-z0-9][A-Za-z0-9._-]{2,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9][A-Za-z0-9._-]{2,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9][A-Za-z0-9_]{15,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgho_[A-Za-z0-9][A-Za-z0-9_]{15,}\b"),
    re.compile(r"\bxoxb-[A-Za-z0-9][A-Za-z0-9-]{8,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|api|token|secret|key)\b[\w .-]{0,32}"
        r"[:=]\s*[\"']?([A-Za-z0-9_-]{32,}={0,2})",
        re.IGNORECASE,
    ),
]
CO_AUTHORED_BY_RE = re.compile(r"^\s*Co-Authored-By:\s*(.+)$", re.IGNORECASE)
AGENT_COAUTHOR_RE = re.compile(
    r"\b(?:claude|codex|gpt|openai|anthropic|deepseek|opencode|openclaw|ai)\b",
    re.IGNORECASE,
)


class ReleaseGateError(Exception):
    """Configuration or invocation error."""


def _emit_utf8() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def _copy_config() -> dict:
    return {
        section: {
            key: list(value) if isinstance(value, list) else value
            for key, value in values.items()
        }
        for section, values in DEFAULT_CONFIG.items()
    }


def _load_config(path: Path) -> dict:
    config = _copy_config()
    if not path.exists():
        raise ReleaseGateError(f"missing config: {path.relative_to(PROJECT_ROOT).as_posix()}")

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseGateError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ReleaseGateError(f"cannot read config {path}: {exc}") from exc

    for section in raw:
        if section not in config:
            raise ReleaseGateError(f"unknown config section [{section}]")
        if not isinstance(raw[section], dict):
            raise ReleaseGateError(f"config section [{section}] must be a table")
        for key, value in raw[section].items():
            if key not in config[section]:
                raise ReleaseGateError(f"unknown config key [{section}].{key}")
            config[section][key] = value

    _validate_string_list(config, "scope", "include_globs")
    _validate_string_list(config, "scope", "exclude_globs")
    _validate_string_list(config, "patterns", "customer_codenames")
    _validate_string_list(config, "patterns", "allowed_emails")
    _validate_string_list(config, "patterns", "allowed_absolute_paths")

    for category, severity in config["severity"].items():
        if not isinstance(severity, str) or severity not in VALID_SEVERITIES:
            raise ReleaseGateError(
                f"invalid severity for {category!r}: expected one of "
                f"{', '.join(sorted(VALID_SEVERITIES))}"
            )
    return config


def _validate_string_list(config: dict, section: str, key: str) -> None:
    value = config[section][key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReleaseGateError(f"[{section}].{key} must be a list of strings")


def _git(args: list[str]) -> list[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseGateError(f"git {' '.join(args)} failed: {stderr}")
    text = proc.stdout.decode("utf-8", errors="surrogateescape")
    if "-z" in args:
        return [part for part in text.split("\0") if part]
    return [line.rstrip("\n") for line in text.splitlines() if line.rstrip("\n")]


def _tracked_files() -> list[str]:
    return sorted(_git(["ls-files", "-z"]))


def _forced_ignored_files() -> list[str]:
    # Git requires -c/--cached together with -i for tracked ignored files.
    return sorted(_git(["ls-files", "-ci", "--exclude-standard", "-z"]))


def _path_matches(path: str, pattern: str) -> bool:
    p = PurePosixPath(path)
    return p.match(pattern) or p.match(f"**/{pattern}")


def _is_included(path: str, include_globs: list[str]) -> bool:
    return not include_globs or any(_path_matches(path, pattern) for pattern in include_globs)


def _is_excluded(path: str, exclude_globs: list[str]) -> bool:
    return any(_path_matches(path, pattern) for pattern in exclude_globs)


def _severity(config: dict, category: str) -> str:
    return config["severity"].get(category, "fail")


def _violation(path: str, line: int | None, category: str, preview: str, config: dict) -> dict:
    return {
        "path": path,
        "line": line,
        "category": category,
        "severity": _severity(config, category),
        "preview": _clean_preview(preview, category),
    }


def _clean_preview(text: str, category: str) -> str:
    value = " ".join(text.strip().split())
    limit = 96
    if category == "api_token" and len(value) > 24:
        return value[:20] + "..."
    if len(value) > limit:
        return value[: limit - 3] + "..."
    return value


def _allowed_email(email: str, allowed: list[str]) -> bool:
    email_l = email.lower()
    for raw in allowed:
        item = raw.strip().lower()
        if not item:
            continue
        if item == email_l:
            return True
        if item.startswith("@") and email_l.endswith(item):
            return True
        if "*" in item:
            pattern = "^" + re.escape(item).replace(r"\*", ".*") + "$"
            if re.match(pattern, email_l):
                return True
    return False


def _allowed_absolute_path(path: str, allowed: list[str]) -> bool:
    normalized = _normalize_path_text(path)
    for raw in allowed:
        item = _normalize_path_text(raw)
        if item and (normalized == item or normalized.startswith(item.rstrip("/") + "/")):
            return True
    return False


def _normalize_path_text(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def _customer_patterns(codenames: list[str]) -> list[re.Pattern]:
    patterns: list[re.Pattern] = []
    for codename in codenames:
        term = codename.strip()
        if not term:
            continue
        patterns.append(
            re.compile(
                r"(?<![\w\u0400-\u04ff])"
                + re.escape(term)
                + r"(?![\w\u0400-\u04ff])",
                re.IGNORECASE,
            )
        )
    return patterns


def _tracked_path_violations(tracked: list[str], config: dict) -> list[dict]:
    violations: list[dict] = []
    for path in tracked:
        basename = path.rsplit("/", 1)[-1]
        if path.startswith("tracker/") and basename.endswith(".jsonl"):
            violations.append(
                _violation(path, None, "tracker_jsonl_tracked", "raw tracker JSONL is tracked", config)
            )
        if path.startswith("prompts/private/"):
            violations.append(
                _violation(path, None, "private_prompts_tracked", "private prompt is tracked", config)
            )
        if _is_backup_path(path):
            violations.append(
                _violation(path, None, "backup_tracked", "backup directory content is tracked", config)
            )
        if _is_compiled_artifact(path):
            violations.append(
                _violation(path, None, "pycache_tracked", "compiled/cache artifact is tracked", config)
            )
        if _is_log_file(path):
            violations.append(_violation(path, None, "log_tracked", "log file is tracked", config))
        if _is_env_file(path):
            violations.append(_violation(path, None, "env_file_tracked", "env file is tracked", config))
    return violations


def _is_backup_path(path: str) -> bool:
    parts = path.split("/")
    return (
        path.startswith(".oss-backup/")
        or path.startswith(".oss-sanitize-backup/")
        or path.startswith(".backup/")
        or "backups" in parts
    )


def _is_compiled_artifact(path: str) -> bool:
    parts = path.split("/")
    return (
        "__pycache__" in parts
        or ".pytest_cache" in parts
        or path.endswith(".pyc")
        or path.endswith(".pyo")
    )


def _is_log_file(path: str) -> bool:
    return path.endswith(".log") or path.endswith(".stderr.log") or path.endswith("/runner.log")


def _is_env_file(path: str) -> bool:
    basename = path.rsplit("/", 1)[-1]
    return basename == ".env" or basename.startswith(".env.") or basename.endswith(".env.local")


def _read_text_file(path: Path) -> tuple[str | None, bool]:
    try:
        with path.open("rb") as fh:
            head = fh.read(8192)
            if b"\0" in head:
                return None, True
            rest = fh.read()
    except OSError:
        return None, False

    data = head + rest
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), False


def _content_violations(path: str, text: str, config: dict, customer_patterns: list[re.Pattern]) -> list[dict]:
    violations: list[dict] = []
    allowed_emails = config["patterns"]["allowed_emails"]
    allowed_paths = config["patterns"]["allowed_absolute_paths"]

    for line_no, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER_RE.search(line):
            continue

        for match in EMAIL_RE.finditer(line):
            email = match.group(0)
            if not _allowed_email(email, allowed_emails):
                violations.append(_violation(path, line_no, "email", email, config))

        for pattern in TOKEN_PATTERNS:
            for match in pattern.finditer(line):
                preview = match.group(1) if match.lastindex else match.group(0)
                violations.append(_violation(path, line_no, "api_token", preview, config))

        for match in WINDOWS_PATH_RE.finditer(line):
            value = match.group(0)
            if not _allowed_absolute_path(value, allowed_paths):
                violations.append(_violation(path, line_no, "absolute_path", value, config))

        for match in LAN_IP_RE.finditer(line):
            violations.append(_violation(path, line_no, "lan_address", match.group(0), config))

        for match in MDNS_RE.finditer(line):
            violations.append(_violation(path, line_no, "lan_address", match.group(0), config))

        for match in CYRILLIC_PATH_RE.finditer(line):
            violations.append(_violation(path, line_no, "cyrillic_path", match.group(0), config))

        for pattern in customer_patterns:
            for match in pattern.finditer(line):
                violations.append(_violation(path, line_no, "customer_codename", match.group(0), config))
    return violations


def _scan_files(tracked: list[str], config: dict) -> tuple[list[dict], list[dict], int]:
    include_globs = config["scope"]["include_globs"]
    exclude_globs = config["scope"]["exclude_globs"]
    customer_patterns = _customer_patterns(config["patterns"]["customer_codenames"])
    violations: list[dict] = []
    notes: list[dict] = []
    scanned = 0

    for path in tracked:
        if not _is_included(path, include_globs) or _is_excluded(path, exclude_globs):
            continue
        full_path = PROJECT_ROOT / path
        if not full_path.is_file():
            notes.append({"path": path, "category": "missing_file", "message": "tracked path is not a file"})
            continue
        text, is_binary = _read_text_file(full_path)
        if is_binary:
            notes.append({"path": path, "category": "binary_skipped", "message": "binary file skipped"})
            continue
        if text is None:
            notes.append({"path": path, "category": "unreadable_file", "message": "file could not be read"})
            continue
        scanned += 1
        violations.extend(_content_violations(path, text, config, customer_patterns))

    return violations, notes, scanned


def _git_log_violations(config: dict) -> list[dict]:
    try:
        messages = _git(["log", "--format=%B", "-20"])
    except ReleaseGateError as exc:
        message = str(exc)
        if "does not have any commits yet" in message or "your current branch" in message:
            return []
        raise
    violations: list[dict] = []
    for line_no, line in enumerate(messages, start=1):
        match = CO_AUTHORED_BY_RE.match(line)
        if not match:
            continue
        preview = match.group(0)
        if AGENT_COAUTHOR_RE.search(match.group(1)) or "Co-Authored-By:" in line:
            violations.append(_violation("<git-log>", line_no, "co_authored_by", preview, config))
    return violations


def _summarize(violations: list[dict], notes: list[dict]) -> dict:
    counts = {"fail": 0, "warn": 0, "info": 0}
    for item in violations:
        severity = item["severity"]
        if severity in counts:
            counts[severity] += 1
    counts["info"] += sum(1 for item in notes if item.get("category") == "binary_skipped")
    return counts


def _sort_violations(violations: list[dict]) -> list[dict]:
    return sorted(
        violations,
        key=lambda item: (
            item["path"],
            -1 if item["line"] is None else item["line"],
            item["category"],
            item["preview"],
        ),
    )


def _human_location(item: dict) -> str:
    if item["line"] is None:
        return item["path"]
    return f"{item['path']}:{item['line']}"


def _print_human(result: dict, quiet: bool) -> None:
    fail_count = result["summary"]["fail"]
    warn_count = result["summary"]["warn"]
    info_count = result["summary"]["info"]
    should_print = not quiet or fail_count > 0
    if not should_print:
        return

    print(f"release-gate: scanning {result['files_scanned']} tracked files...")
    for item in result["violations"]:
        if item["severity"] == "ok":
            continue
        print(f"{_human_location(item)}  {item['category']}  {item['preview']}")

    if result["notes"] and not quiet:
        for note in result["notes"]:
            print(f"note: {note['path']}  {note['category']}  {note['message']}")

    suffix = "BLOCKED" if fail_count else "clean"
    if warn_count and not fail_count:
        suffix = "warnings"
    pieces = [f"{fail_count} fail", f"{warn_count} warn"]
    if info_count:
        pieces.append(f"{info_count} info")
    print(f"summary: {', '.join(pieces)} ({suffix})")


def _build_result(config: dict) -> dict:
    tracked = _tracked_files()
    violations: list[dict] = []

    for path in _forced_ignored_files():
        violations.append(_violation(path, None, "forced_ignored", "tracked despite .gitignore", config))

    violations.extend(_tracked_path_violations(tracked, config))
    content_violations, notes, scanned = _scan_files(tracked, config)
    violations.extend(content_violations)
    violations.extend(_git_log_violations(config))
    violations = [item for item in violations if item["severity"] != "ok"]
    violations = _sort_violations(violations)
    summary = _summarize(violations, notes)
    return {
        "status": "blocked" if summary["fail"] else "clean",
        "files_tracked": len(tracked),
        "files_scanned": scanned,
        "summary": summary,
        "violations": violations,
        "notes": notes,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hard-fail privacy scanner for release gates.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON output")
    parser.add_argument("--quiet", action="store_true", help="Suppress output on success")
    return parser.parse_args(argv)


def _print_error(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2))
    else:
        print(f"release-gate: error: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    _emit_utf8()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = _load_config(CONFIG_PATH)
        result = _build_result(config)
    except ReleaseGateError as exc:
        _print_error(str(exc), args.json)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(result, args.quiet)
    return 1 if result["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
