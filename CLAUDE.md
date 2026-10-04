# CLAUDE.md — Caspian Warehouse (انبار کاسپین)

Offline-first, AI-assisted inventory management desktop app for Windows. Persian RTL UI,
Jalali calendar, MariaDB backend. Built for Caspian Furniture Market, sold/shared as a general
product. Public repo: https://github.com/alirezamolayari-prog/Caspian_Warehouse (proprietary
license — see LICENSE). Current release: **v1.0.0** (installer on GitHub Releases).

## Features (all implemented)
- Users/roles/permissions, login lockout, **admin PIN** for protected actions (delete/deactivate
  item, import overwrite, merge, create similar item, restore backup, change role, close fiscal
  year), full audit log.
- Items (units + conversions, barcodes per unit, reorder points, returnable/loan items),
  warehouses, persons, categories; normalized Persian search.
- **One item per real product** (`services/items.py`): exact normalized duplicate names refused;
  similar names (rapidfuzz ≥ `SIMILAR_SCORE` 85, numbers must match) raise `SimilarItems` unless a
  `CREATE_SIMILAR_ITEM` PIN approval (with reason) is given — enforced for dialog, opening stock, imports,
  renames, never for the AI. Existing duplicates: health finding + Items filter + PIN-protected
  `merge_items` (moves lines/ledger/balances/barcodes/units, deactivates sources). Never auto-modified.
- Documents: receipt, issue, transfer, adjustment, opening, loan out/return; draft → post
  (stock ledger + balances, row locks) → cancel (reversal rows; history never deleted).
- **Draft-first imports** (Excel/CSV/Word, barcode scan, typed text) with row statuses
  NEW / EXISTING_MATCH / CONFLICT / ERROR / IGNORED; nothing is written until a human applies.
- Blind stocktake (counters never see system qty; `stock.view` permission), discrepancy report.
- Reports (stock value, cardex, burn-rate reorder analysis, loans, user activity) → Excel/PDF.
- AI gateway (OpenAI-compatible, Groq, Gemini, OpenRouter, Cerebras, Mistral, Hugging Face, Ollama,
  custom; silent fallback on 429/timeouts, offline → local only), chat + in-app voice button (transcript
  is confirmed by the user), tool calling. The AI works like a user: creates documents and, if the admin
  setting `ai_allow_post` is on (default), posts them through the normal services. It **never** does
  destructive/protected actions: `Actor.require_human()` in services refuses cancel/delete, deactivation,
  user management, stocktake approve/cancel, import discard, restore for `actor.is_ai`.
  Gateway errors show a short Persian reason per HTTP status; raw server text only in the log.
- Assistant chat history (`services/ai/history.py`, table `assistant_messages`): saved in the background
  after each reply (≤ 2,000 chars, no audio); own rows only, admins all; the AI never reads it; retention
  `ai_history_days` (default 30) purged in batches by `SchedulerRunner` (startup + daily).
- Read-only data health check (`services/health.py`, dashboard warning); startup refuses a DB migrated
  by a newer app (`migrate.SchemaTooNew`) without touching it.
- Read-only MCP server (`caspian-mcp`), Telegram/SMTP messaging to admin-configured recipients,
  scheduled tasks from `.md` instructions (proposed → admin approves → runs on that PC only).
- Encrypted backups (`.bak`: mariadb-dump + gzip + AES-256-GCM/scrypt), PIN-protected restore.
- Daily automatic backup (Settings → پشتیبان‌گیری): one `TaskKind.BACKUP` task per PC with
  `params.auto_daily` (`scheduler.set_daily_backup`, cron `M H * * *`, approved for `MACHINE`, label
  «خودکار»). `SchedulerRunner` waits `catch_up_delay` (60 s) after login, then `scheduler.catch_up` runs each
  missed task once (skipped if the next run is < 1 h away) before the normal `run_due` loop.
- Fiscal year-end wizard: backup → archive DB `<name>_<year>` (SELECT-only) → opening balances.
  Loans carried over by the close are not health-check findings.
- Document numbering/posting retries on unique/deadlock conflicts (`documents._with_retries`), then a
  Persian error; `tests/test_concurrency.py` covers SQLite and MariaDB.

## Stack & structure
Python 3.12+ (dev on 3.14), uv, PySide6-Essentials + qasync, SQLAlchemy 2 async + Alembic,
aiomysql (MariaDB) / aiosqlite (tests), keyring, argon2, httpx, openpyxl, python-docx,
rapidfuzz, cryptography, mcp v2. Lint: ruff (line length 110). Tests: pytest, pytest-qt.

```
src/caspian/
  app.py            startup (DB connect → login → main window), --smoke-test
  setup_cli.py      installer modes: --provision <pwfile>, --check-db
  mcp_server.py     read-only MCP server (entry point caspian-mcp)
  core/             settings, secrets (keyring), jalali, text normalize, numbers, cron, security
  db/               models.py, database.py, migrate.py, bootstrap.py, seed.py, provision.py,
                    readonly.py, migrations/versions/*  (Alembic, run automatically at startup)
  services/         business logic (all permission/PIN/AI checks live HERE, not in the UI)
    ai/             gateway, assistant, tools, config, offline text_parser
  ui/               PySide6 pages/dialogs; theme.py (light/dark tokens + QSS), widgets.py
  resources/        Vazirmatn fonts, Lucide icons
tests/              pytest; conftest.py (template SQLite DB, fast hashing, isolated settings)
packaging/          caspian.spec, installer.iss, build.ps1, test_installer.ps1, licenses/
.github/workflows/  ci.yml (tests on push/PR), release.yml (tags → build/test/publish)
docs/               USER_GUIDE.md (Persian), LAN_SETUP.md, ROADMAP.md, screenshots/
```

## Run / test / build
```powershell
uv sync
uv run caspian                 # the app (uv run caspian --smoke-test: builds every page headless)
$env:QT_QPA_PLATFORM="offscreen"; uv run pytest   # ~420 tests (SQLite)
$env:CASPIAN_SLOW_TESTS="1"; uv run pytest tests/sim   # 6-month simulation (~1 min)
uv run ruff check
# Optional MariaDB integration tests (the test DB is wiped):
$env:CASPIAN_TEST_MARIADB_URL="mariadb+aiomysql://caspian:<pw>@localhost:3306/caspian_test?charset=utf8mb4"
$env:CASPIAN_TEST_MARIADB_ROOT="root:<pw>"
# Installer (needs Inno Setup 6):
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -DownloadMariaDb   # or -SkipServer
# -> dist\installer\CaspianWarehouse-Setup-<version>.exe ; test it (elevated shell):
packaging\test_installer.ps1 -Installer <exe> [-UpgradeFrom <older exe>] [-WithDatabase -DbPassword <pw>]
```
Model change → `uv run alembic revision --autogenerate -m "..."` (replace generated
`sa.text('(CURRENT_TIMESTAMP)')` with `sa.func.now()`); `test_migrations_match_models` guards this.

## Database & where data lives
- MariaDB 10.6+ (11.8 LTS). App account `caspian` with rights on DB `caspian` only; created by
  the installer (`--provision`) or the first-run dialog ("create dedicated app user" from root).
- User data is **never** in the program folder:
  settings + logs `%LOCALAPPDATA%\CaspianWarehouse\` (settings.json, Logs\caspian.log),
  backups + year-end audit exports `Documents\Caspian Backups`,
  passwords/API keys in Windows Credential Manager (keyring service `CaspianWarehouse`,
  e.g. `db:caspian@localhost:3306`, `mariadb:root@localhost:3306`, `ai:provider:<id>`),
  business data in MariaDB. Shared settings (AI providers, messaging, fiscal state) are in the
  `app_settings` table; secrets stay per PC.
- Dev machine: MariaDB service at `E:\MariaDB\11.8` (data `E:\MariaDB\data`); Inno Setup at
  `E:\Tools\InnoSetup6`.

## Release process
1. Version lives **only** in `pyproject.toml` (`caspian.__version__` reads package metadata;
   the spec/installer derive theirs from it).
2. Bump `version`, run tests, commit + push to `main`.
3. `git tag vX.Y.Z` + `git push origin vX.Y.Z` (tag must equal the version or the run fails).
4. `release.yml` on windows-latest: tests → build (downloads MariaDB MSI, checksum-verified) →
   installs on the clean runner with the DB server, `--check-db`, uninstall → publishes the GitHub
   Release with `CaspianWarehouse-Setup-X.Y.Z.exe` + `SHA256SUMS.txt`
   (https://github.com/alirezamolayari-prog/Caspian_Warehouse/releases/latest).
   Pushing a `release-*` branch = same pipeline without publishing (dry run).

## Rules
- **Keep user data safe:** never write user data into the install folder; upgrades/uninstall must
  not touch settings, backups, credentials or the database. Never change the installer `AppId`.
- Tests and scripts must never overwrite the real settings file (conftest isolates it; the smoke
  test uses a non-saving Settings). conftest installs an in-memory keyring backend for every test
  (`MemoryKeyring`, guarded by `tests/test_keyring_guard.py`) — never remove it; an earlier suite deleted
  a real API key from Credential Manager.
- Enforce permissions, PIN approvals and AI restrictions in `services/`, not only in the UI.
  AI tools may create/post documents only via the normal services; any new destructive service must call
  `actor.require_human(...)`. Posted documents are cancelled, never deleted.
- Every schema change needs an Alembic migration. User-facing text is Persian (RTL).
- Run `uv run ruff check` and the full test suite (plus MariaDB tests when touching DB code)
  before pushing; check CI after pushing. Keep sources LF.
- Windows PowerShell 5.1 mangles `"` in native args: commit with `git commit -F <file>`
  (write the file with `[IO.File]::WriteAllText`, no BOM). Anonymous GitHub API allows only
  60 requests/hour — poll CI slowly.
- In Inno Setup silent mode wizard callbacks still run — never use plain `MsgBox` there.
- UI: `@asyncSlot` only on methods (closures: `ui/tasks.callback/spawn`); no blocking `exec()`/static
  dialogs inside coroutines (`ui/file_dialogs`, `exec_dialog`); printed table cells via
  `printing.rtl_cells`; QTest `keyClicks` with Persian text kills the test process — type Latin.

## State, known issues, TODOs
Done: roadmap M0–M13 (docs/ROADMAP.md) and the installer/release pipeline; v1.0.0 published;
QA rounds (docs/PROGRESS.md, docs/QA_FIX_PLAN.md) merged to main, not yet released.
Only tested with mocks / not end-to-end yet:
- real AI provider keys (the assistant creating/posting documents end-to-end), Telegram/SMTP sending,
  voice with real audio (silence threshold `recorder.MIN_RMS`);
- LAN with two real PCs; the interactive (clicked-through) installer wizard — CI covers silent.

Known limitations:
- Installer creates the app DB account for `localhost` only; LAN clients need the manual SQL
  in docs/LAN_SETUP.md (idea: an in-app "enable network access" admin action).
- If setup is elevated with a *different* admin account, auto-provisioning is skipped and the
  first-run dialog is used instead.
- Installer isn't code-signed (SmartScreen warning); installer UI is English only.
- GitHub Actions warn about Node 20 (checkout@v4, setup-uv@v6, upload-artifact@v4) — bump majors.

Ideas: code signing; in-app update check; Persian installer translation; dashboard charts;
inventory valuation methods (FIFO/weighted average); purchase/sales invoices; mobile barcode
scanning; real screenshots in docs/screenshots.
