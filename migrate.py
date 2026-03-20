"""
Claude Session Migrator — migrate GUI session history between Claude accounts (Windows).

When you switch Claude accounts, your old chat sessions disappear from the GUI
because Claude Desktop indexes them by account UUID. This tool copies session
metadata from the old account folder to the new one so they show up again.

Usage:
    python migrate.py              # interactive — auto-detects accounts
    python migrate.py --list       # just list accounts and session counts
    python migrate.py --old <UUID> --new <UUID>   # explicit migration
    python migrate.py --rebuild-indexes            # only rebuild sessions-index.json
"""

import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


APPDATA = os.environ.get("APPDATA", "")
if not APPDATA:
    APPDATA = str(Path.home() / "AppData" / "Roaming")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def find_sessions_dir() -> str:
    """
    Locate claude-code-sessions directory.

    Claude Desktop can be installed as a regular app (%AppData%/Claude/)
    or as a Windows Store app (%LocalAppData%/Packages/Claude_*/LocalCache/Roaming/Claude/).
    Store apps virtualize AppData, so the folder only exists inside the package.
    """
    standard = os.path.join(APPDATA, "Claude", "claude-code-sessions")
    if os.path.isdir(standard):
        return standard

    packages = os.path.join(LOCALAPPDATA, "Packages")
    if os.path.isdir(packages):
        for pkg in os.listdir(packages):
            if pkg.startswith("Claude_"):
                store_path = os.path.join(
                    packages, pkg, "LocalCache", "Roaming", "Claude", "claude-code-sessions"
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
            accounts.append({
                "account_uuid": acct,
                "org_uuid": org,
                "path": org_path,
                "session_count": len(sessions),
                "is_current": org == current_org,
            })
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


def copy_sessions(old_path: str, new_path: str) -> tuple[int, int]:
    """Copy local_*.json from old account to new. Returns (copied, skipped)."""
    copied = 0
    skipped = 0

    for f in glob.glob(os.path.join(old_path, "local_*.json")):
        fname = os.path.basename(f)
        dest = os.path.join(new_path, fname)
        if os.path.exists(dest):
            skipped += 1
        else:
            shutil.copy2(f, dest)
            copied += 1

    return copied, skipped


def parse_session_file(jsonl_path: str) -> dict:
    """Extract metadata from a .jsonl session file."""
    first_prompt = "No prompt"
    msg_count = 0
    first_ts = None
    last_ts = None

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
    }


def rebuild_indexes() -> tuple[int, int]:
    """Generate sessions-index.json for projects missing one. Returns (created, skipped)."""
    if not os.path.isdir(PROJECTS_DIR):
        print(f"Projects directory not found: {PROJECTS_DIR}")
        return 0, 0

    created = 0
    skipped = 0

    for proj in sorted(os.listdir(PROJECTS_DIR)):
        proj_path = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(proj_path):
            continue

        index_path = os.path.join(proj_path, "sessions-index.json")
        jsonl_files = glob.glob(os.path.join(proj_path, "*.jsonl"))

        if not jsonl_files:
            continue

        if os.path.exists(index_path):
            skipped += 1
            continue

        entries = []
        for jf in jsonl_files:
            session_id = os.path.basename(jf).replace(".jsonl", "")
            try:
                meta = parse_session_file(jf)
            except Exception as e:
                print(f"  WARN: Could not parse {jf}: {e}")
                continue

            win_path = str(Path(jf).resolve()).replace("/", "\\")

            entries.append({
                "sessionId": session_id,
                "fullPath": win_path,
                "fileMtime": int(meta["mtime"] * 1000),
                "firstPrompt": meta["first_prompt"],
                "summary": "",
                "messageCount": meta["msg_count"],
                "created": meta["first_ts"],
                "modified": meta["last_ts"],
                "gitBranch": "",
                "projectPath": "",
                "isSidechain": False,
            })

        if entries:
            index_data = {"version": 1, "entries": entries}
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False, indent=2)
            created += 1
            print(f"  CREATED index: {proj} ({len(entries)} sessions)")

    return created, skipped


def interactive_migrate(accounts: list[dict]) -> None:
    """Guide the user through account selection and migration."""
    if len(accounts) < 2:
        print("Need at least 2 accounts to migrate. Found:", len(accounts))
        return

    list_accounts(accounts)

    """
    Determine old/new accounts. The current account (from credentials.json)
    is the NEW one — we want to copy sessions INTO it from all others.
    Session count is unreliable after a previous migration.
    """
    current = [a for a in accounts if a["is_current"]]
    others = [a for a in accounts if not a["is_current"]]

    if current and others:
        likely_new = current[0]
        likely_old = max(others, key=lambda a: a["session_count"])
        print(f"Detected from credentials:")
    else:
        sorted_by_count = sorted(accounts, key=lambda a: a["session_count"], reverse=True)
        likely_old = sorted_by_count[0]
        likely_new = sorted_by_count[-1]
        print(f"Best guess (could not read credentials):")

    print(f"  OLD: [{accounts.index(likely_old) + 1}] — {likely_old['session_count']} sessions")
    print(f"  NEW: [{accounts.index(likely_new) + 1}] — {likely_new['session_count']} sessions (current)")
    print()

    confirm = input("Use this? [Y/n] or enter numbers like '1 2' (old new): ").strip()

    if confirm.lower() in ("n", "no"):
        print("Aborted.")
        return
    elif confirm.lower() in ("", "y", "yes"):
        old, new = likely_old, likely_new
    else:
        try:
            parts = confirm.split()
            old = accounts[int(parts[0]) - 1]
            new = accounts[int(parts[1]) - 1]
        except (IndexError, ValueError):
            print("Invalid input. Expected 'y', 'n', or two numbers like '1 2'.")
            return

    print(f"\nMigrating sessions:")
    print(f"  FROM: {old['account_uuid']}/{old['org_uuid']} ({old['session_count']} sessions)")
    print(f"  TO:   {new['account_uuid']}/{new['org_uuid']} ({new['session_count']} sessions)")

    confirm2 = input("\nProceed? [Y/n]: ").strip()
    if confirm2.lower() not in ("", "y", "yes"):
        print("Aborted.")
        return

    copied, skipped = copy_sessions(old["path"], new["path"])
    print(f"\nSession metadata: {copied} copied, {skipped} skipped (already existed)")

    print("\nRebuilding project indexes...")
    idx_created, idx_skipped = rebuild_indexes()
    print(f"Indexes: {idx_created} created, {idx_skipped} skipped")

    print("\n" + "=" * 50)
    print("Done! Restart Claude Desktop to see your old chats.")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Claude Code GUI sessions between accounts (Windows)"
    )
    parser.add_argument("--list", action="store_true", help="List accounts and exit")
    parser.add_argument("--old", help="Old account UUID")
    parser.add_argument("--new", help="New account UUID")
    parser.add_argument(
        "--rebuild-indexes",
        action="store_true",
        help="Only rebuild missing sessions-index.json files",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show debug info (paths, env vars)",
    )
    args = parser.parse_args()

    if args.verbose:
        print(f"APPDATA: {APPDATA}")
        print(f"SESSIONS_DIR: {SESSIONS_DIR}")
        print(f"SESSIONS_DIR exists: {os.path.isdir(SESSIONS_DIR)}")
        print(f"PROJECTS_DIR: {PROJECTS_DIR}")
        print()

    if sys.platform != "win32":
        print("ERROR: This tool only supports Windows.")
        sys.exit(1)

    if args.rebuild_indexes:
        print("Rebuilding project indexes...")
        created, skipped = rebuild_indexes()
        print(f"\nDone: {created} created, {skipped} skipped")
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

        copied, skipped = copy_sessions(old_acct["path"], new_acct["path"])
        print(f"Session metadata: {copied} copied, {skipped} skipped")

        print("Rebuilding project indexes...")
        idx_created, idx_skipped = rebuild_indexes()
        print(f"Indexes: {idx_created} created, {idx_skipped} skipped")
        print("\nDone! Restart Claude Desktop.")
        return

    interactive_migrate(accounts)


if __name__ == "__main__":
    main()
