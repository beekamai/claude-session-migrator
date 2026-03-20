# Claude Session Migrator

Migrate your Claude Code (Desktop) chat history when switching accounts.

When you change your Claude account, old sessions disappear from the GUI — even though session files still exist on disk. This happens because Claude Desktop indexes sessions by account UUID.

This tool copies session metadata from your old account's folder to the new one, making all your old chats visible again.

## The Problem

```
Switched Claude accounts
  → Old chats gone from GUI
  → CLI --resume still works
  → But no way to browse old sessions in Desktop app
```

## How It Works

Claude Desktop stores session data in two places:

| Layer | Path | Account-tied? |
|-------|------|---------------|
| CLI sessions (`.jsonl`) | `~/.claude/projects/<project>/` | No |
| GUI metadata (`.json`) | `claude-code-sessions/<account>/<org>/` | **Yes** |

The tool:
1. Auto-detects the current account from `~/.claude/.credentials.json`
2. Finds all other (old) account folders
3. Copies `local_*.json` metadata files into the current account's folder
4. Rebuilds missing `sessions-index.json` for project directories

## Requirements

- Windows
- Python 3.10+
- Claude Desktop installed (regular or Windows Store)

## Usage

### Interactive (recommended)

```bash
python migrate.py
```

Auto-detects accounts via credentials, shows session counts, asks for confirmation, then migrates.

```
Found 2 account(s):

  [1] Account: 1e4bbbf8-ccb8-...
      Org:     5229773c-ad11-...
      Sessions: 15

  [2] Account: 4abf1d66-32b8-... (current)
      Org:     447ee970-18c7-...
      Sessions: 1

Detected from credentials:
  OLD: [1] — 15 sessions
  NEW: [2] — 1 sessions (current)

Use this? [Y/n] or enter numbers like '1 2' (old new):
```

### List accounts

```bash
python migrate.py --list
```

### Explicit migration

```bash
python migrate.py --old <old-account-uuid> --new <new-account-uuid>
```

### Only rebuild session indexes

```bash
python migrate.py --rebuild-indexes
```

Generates `sessions-index.json` for projects that have `.jsonl` session files but are missing the index.

### Debug

```bash
python migrate.py --list -v
```

Shows resolved paths and environment info — useful if sessions aren't being found.

## Safety

- **Non-destructive** — only copies files, never deletes or modifies originals
- **Idempotent** — skips files that already exist in the destination
- **No dependencies** — pure Python stdlib, no pip install needed

## After Migration

1. **Fully close** Claude Desktop (system tray -> Exit)
2. Reopen Claude Desktop
3. Your old sessions should appear in the chat list

## How Claude Desktop Stores Sessions

The tool auto-detects both installation types:

| Installation | Session path |
|---|---|
| Regular (`.exe`) | `%AppData%\Claude\claude-code-sessions\` |
| Windows Store | `%LocalAppData%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code-sessions\` |

```
claude-code-sessions/
├── <account-uuid-1>/          <- old account
│   └── <org-uuid>/
│       ├── local_abc123.json
│       ├── local_def456.json
│       └── ...
└── <account-uuid-2>/          <- current account
    └── <org-uuid>/
        └── local_ghi789.json  <- only sessions created after switch
```

Each `local_*.json` contains metadata (session ID, working directory, title, model) and points to the actual `.jsonl` conversation files in `~/.claude/projects/`.

## License

MIT
