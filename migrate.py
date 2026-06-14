"""
Claude Session Migrator — migrate GUI session history between Claude accounts.

Supports Windows and macOS.

When you switch Claude accounts, your old chat sessions disappear from the GUI
because Claude Desktop indexes them by account UUID. This tool copies session
metadata from the old account folder to the new one so they show up again.

Usage:
    python migrate.py              # interactive — auto-detects accounts
    python migrate.py --list       # just list accounts and session counts
    python migrate.py --old <UUID> --new <UUID>   # explicit migration
    python migrate.py --rebuild-indexes            # only rebuild sessions-index.json
    python migrate.py --dry-run    # preview changes without writing
    python migrate.py --yes        # accept auto-detected migration, no prompts
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

APPDATA = os.environ.get("APPDATA", "")
if not APPDATA:
    APPDATA = str(Path.home() / "AppData" / "Roaming")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def find_sessions_dir() -> str:
    """
    Locate claude-code-sessions directory.

    macOS: Claude Desktop stores data under
    ~/Library/Application Support/Claude/claude-code-sessions.

    Windows: Claude Desktop can be installed as a regular app (%AppData%/Claude/)
    or as a Windows Store app (%LocalAppData%/Packages/Claude_*/LocalCache/Roaming/Claude/).
    Store apps virtualize AppData, so the folder only exists inside the package.
    """
    if IS_MACOS:
        return str(
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude-code-sessions"
        )

    standard = os.path.join(APPDATA, "Claude", "claude-code-sessions")
    if os.path.isdir(standard):
        return standard

    packages = os.path.join(LOCALAPPDATA, "Packages")
    if os.path.isdir(packages):
        for pkg in os.listdir(packages):
            if pkg.startswith("Claude_"):
                store_path = os.path.join(
                    packages,
                    pkg,
                    "LocalCache",
                    "Roaming",
                    "Claude",
                    "claude-code-sessions",
                )
                if os.path.isdir(store_path):
                    return store_path

    return standard


SESSIONS_DIR = find_sessions_dir()


def get_current_org_uuid() -> str | None:
    """Read the active org UUID from ~/.claude/.credentials.json."""
    cred_path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    try:
        with open(cred_path, "r", encoding="utf-8") as f:
            return json.load(f).get("organizationUuid")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def get_accounts() -> list[dict]:
    """Discover all account/org pairs and their session counts."""
    if not os.path.isdir(SESSIONS_DIR):
        return []

    current_org = get_current_org_uuid()
    accounts = []
    for acct in sorted(os.listdir(SESSIONS_DIR)):
        acct_path = os.path.join(SESSIONS_DIR, acct)
        if not os.path.isdir(acct_path):
            continue
        for org in sorted(os.listdir(acct_path)):
            org_path = os.path.join(acct_path, org)
            if not os.path.isdir(org_path):
                continue
            sessions = glob.glob(os.path.join(org_path, "local_*.json"))
            accounts.append(
                {
                    "account_uuid": acct,
                    "org_uuid": org,
                    "path": org_path,
                    "session_count": len(sessions),
                    "is_current": org == current_org,
                }
            )
    return accounts


def list_accounts(accounts: list[dict]) -> None:
    """Print discovered accounts."""
    if not accounts:
        print("No Claude accounts found.")
        print(f"Expected path: {SESSIONS_DIR}")
        return

    print(f"\nFound {len(accounts)} account(s):\n")
    for i, a in enumerate(accounts):
        tag = " (current)" if a["is_current"] else ""
        print(f"  [{i + 1}] Account: {a['account_uuid']}{tag}")
        print(f"      Org:     {a['org_uuid']}")
        print(f"      Sessions: {a['session_count']}")
        print()


def copy_sessions(
    old_path: str, new_path: str, dry_run: bool = False
) -> tuple[int, int]:
    """Copy local_*.json from old account to new. Returns (copied, skipped)."""
    copied = 0
    skipped = 0

    for f in glob.glob(os.path.join(old_path, "local_*.json")):
        fname = os.path.basename(f)
        dest = os.path.join(new_path, fname)
        if os.path.exists(dest):
            skipped += 1
        else:
            if not dry_run:
                shutil.copy2(f, dest)
            copied += 1

    return copied, skipped


def parse_session_file(jsonl_path: str) -> dict:
    """Extract metadata from a .jsonl session file."""
    first_prompt = "No prompt"
    msg_count = 0
    first_ts = None
    last_ts = None
    git_branch = ""
    cwd = ""

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                msg_count += 1
                ts = d.get("timestamp")
                if ts and not first_ts:
                    first_ts = ts
                if ts:
                    last_ts = ts
                # gitBranch / cwd appear on most entries; keep the first seen.
                if not git_branch and d.get("gitBranch"):
                    git_branch = d["gitBranch"]
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if (
                    d.get("type") == "queue-operation"
                    and d.get("content")
                    and first_prompt == "No prompt"
                ):
                    first_prompt = d["content"][:200]
            except (json.JSONDecodeError, KeyError):
                pass

    mtime = os.path.getmtime(jsonl_path)
    if not first_ts:
        first_ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    if not last_ts:
        last_ts = first_ts

    return {
        "first_prompt": first_prompt,
        "msg_count": msg_count,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "mtime": mtime,
        "git_branch": git_branch,
        "cwd": cwd,
    }


def build_index_entry(jsonl_path: str) -> dict:
    """Build a single sessions-index.json entry from a .jsonl session file."""
    session_id = os.path.basename(jsonl_path).replace(".jsonl", "")
    meta = parse_session_file(jsonl_path)

    # Claude Desktop stores the path using the OS-native separator:
    # backslashes on Windows, forward slashes on macOS.
    full_path = str(Path(jsonl_path).resolve())
    if IS_WINDOWS:
        full_path = full_path.replace("/", "\\")

    return {
        "sessionId": session_id,
        "fullPath": full_path,
        "fileMtime": int(meta["mtime"] * 1000),
        "firstPrompt": meta["first_prompt"],
        "summary": "",
        "messageCount": meta["msg_count"],
        "created": meta["first_ts"],
        "modified": meta["last_ts"],
        "gitBranch": meta["git_branch"],
        "projectPath": meta["cwd"],
        "isSidechain": False,
    }


def rebuild_indexes(dry_run: bool = False) -> tuple[int, int, int]:
    """
    Create or update sessions-index.json for every project.

    Missing indexes are created; existing indexes gain entries for any sessions
    they don't yet list (existing entries are left untouched). Returns
    (created, updated, skipped).
    """
    if not os.path.isdir(PROJECTS_DIR):
        print(f"Projects directory not found: {PROJECTS_DIR}")
        return 0, 0, 0

    created = 0
    updated = 0
    skipped = 0

    for proj in sorted(os.listdir(PROJECTS_DIR)):
        proj_path = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(proj_path):
            continue

        index_path = os.path.join(proj_path, "sessions-index.json")
        jsonl_files = glob.glob(os.path.join(proj_path, "*.jsonl"))

        if not jsonl_files:
            continue

        existing_entries: list[dict] = []
        known_ids: set[str] = set()
        if os.path.exists(index_path):
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                existing_entries = data.get("entries", [])
                known_ids = {
                    sid
                    for e in existing_entries
                    if (sid := e.get("sessionId"))
                }
            except (json.JSONDecodeError, OSError) as e:
                print(f"  WARN: Could not read {index_path}: {e} — rewriting")
                existing_entries = []
                known_ids = set()

        new_entries = []
        for jf in jsonl_files:
            session_id = os.path.basename(jf).replace(".jsonl", "")
            if session_id in known_ids:
                continue
            try:
                new_entries.append(build_index_entry(jf))
            except Exception as e:
                print(f"  WARN: Could not parse {jf}: {e}")
                continue

        if not new_entries:
            if existing_entries:
                skipped += 1
            continue

        is_new = not existing_entries
        merged = existing_entries + new_entries
        action = "CREATE" if is_new else "UPDATE"
        print(
            f"  {'WOULD ' if dry_run else ''}{action} index: {proj} "
            f"(+{len(new_entries)} sessions, {len(merged)} total)"
        )

        if not dry_run:
            index_data = {"version": 1, "entries": merged}
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False, indent=2)

        if is_new:
            created += 1
        else:
            updated += 1

    return created, updated, skipped


def run_migration(sources: list[dict], new: dict, dry_run: bool = False) -> None:
    """Copy sessions from each source account into `new`, then rebuild indexes."""
    prefix = "[dry-run] " if dry_run else ""
    total_copied = 0
    total_skipped = 0
    for old in sources:
        copied, skipped = copy_sessions(old["path"], new["path"], dry_run=dry_run)
        total_copied += copied
        total_skipped += skipped
        print(
            f"  {prefix}{old['account_uuid']}/{old['org_uuid']}: "
            f"{copied} copied, {skipped} skipped"
        )
    print(
        f"\n{prefix}Session metadata: {total_copied} copied, "
        f"{total_skipped} skipped (already existed)"
    )

    print(f"\n{prefix}Rebuilding project indexes...")
    created, updated, idx_skipped = rebuild_indexes(dry_run=dry_run)
    print(
        f"{prefix}Indexes: {created} created, {updated} updated, "
        f"{idx_skipped} unchanged"
    )

    print("\n" + "=" * 50)
    if dry_run:
        print("Dry run complete. No files were written. Re-run without --dry-run.")
    else:
        print("Done! Restart Claude Desktop to see your old chats.")
    print("=" * 50)


def interactive_migrate(
    accounts: list[dict], assume_yes: bool = False, dry_run: bool = False
) -> None:
    """Guide the user through account selection and migration."""
    if len(accounts) < 2:
        print("Need at least 2 accounts to migrate. Found:", len(accounts))
        return

    list_accounts(accounts)

    # Determine source/destination accounts. The current account (from
    # credentials.json) is the destination — we copy sessions INTO it from every
    # other account. Session count is unreliable after a previous migration.
    current = [a for a in accounts if a["is_current"]]
    others = [a for a in accounts if not a["is_current"]]

    if current and others:
        new = current[0]
        sources = others
        print("Detected from credentials:")
    else:
        # No credentials — assume the smallest account is the (new) destination
        # and migrate every other account into it.
        sorted_by_count = sorted(
            accounts, key=lambda a: a["session_count"], reverse=True
        )
        new = sorted_by_count[-1]
        sources = sorted_by_count[:-1]
        print("Best guess (could not read credentials):")

    for s in sources:
        print(f"  FROM: [{accounts.index(s) + 1}] — {s['session_count']} sessions")
    print(
        f"  INTO: [{accounts.index(new) + 1}] — {new['session_count']} sessions (current)"
    )
    print()

    if not assume_yes:
        confirm = input(
            "Use this? [Y/n], or enter numbers like '1 2' (source dest): "
        ).strip()
        if confirm.lower() in ("n", "no"):
            print("Aborted.")
            return
        elif confirm.lower() in ("", "y", "yes"):
            pass
        else:
            try:
                parts = confirm.split()
                new = accounts[int(parts[-1]) - 1]
                sources = [accounts[int(p) - 1] for p in parts[:-1]]
            except (IndexError, ValueError):
                print(
                    "Invalid input. Expected 'y', 'n', or numbers like '1 2' "
                    "(one or more sources followed by the destination)."
                )
                return

        confirm2 = input("\nProceed? [Y/n]: ").strip()
        if confirm2.lower() not in ("", "y", "yes"):
            print("Aborted.")
            return

    print("\nMigrating sessions...")
    run_migration(sources, new, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Claude Code GUI sessions between accounts (Windows/macOS)"
    )
    parser.add_argument("--list", action="store_true", help="List accounts and exit")
    parser.add_argument("--old", help="Old account UUID")
    parser.add_argument("--new", help="New account UUID")
    parser.add_argument(
        "--rebuild-indexes",
        action="store_true",
        help="Only rebuild missing/incomplete sessions-index.json files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing any files",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompts (non-interactive)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show debug info (paths, env vars)",
    )
    args = parser.parse_args()

    if args.verbose:
        print(f"platform: {sys.platform}")
        if IS_WINDOWS:
            print(f"APPDATA: {APPDATA}")
            print(f"LOCALAPPDATA: {LOCALAPPDATA}")
        print(f"SESSIONS_DIR: {SESSIONS_DIR}")
        print(f"SESSIONS_DIR exists: {os.path.isdir(SESSIONS_DIR)}")
        print(f"PROJECTS_DIR: {PROJECTS_DIR}")
        print()

    if not (IS_WINDOWS or IS_MACOS):
        print(
            f"ERROR: Unsupported platform '{sys.platform}'. Only Windows and macOS are supported."
        )
        sys.exit(1)

    if args.rebuild_indexes:
        print("Rebuilding project indexes...")
        created, updated, skipped = rebuild_indexes(dry_run=args.dry_run)
        print(f"\nDone: {created} created, {updated} updated, {skipped} unchanged")
        return

    accounts = get_accounts()

    if args.list:
        list_accounts(accounts)
        return

    if args.old and args.new:
        old_acct = next((a for a in accounts if a["account_uuid"] == args.old), None)
        new_acct = next((a for a in accounts if a["account_uuid"] == args.new), None)
        if not old_acct:
            print(f"ERROR: Old account {args.old} not found")
            sys.exit(1)
        if not new_acct:
            print(f"ERROR: New account {args.new} not found")
            sys.exit(1)

        run_migration([old_acct], new_acct, dry_run=args.dry_run)
        return

    interactive_migrate(accounts, assume_yes=args.yes, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
