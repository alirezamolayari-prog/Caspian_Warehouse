# QA fixes progress — branch `qa-fixes-2026-09`

Source list: [CASPIAN_FIXES.md](CASPIAN_FIXES.md) (manual QA of v1.0.0, 8 Mehr 1405).
Plan: Phase 1 (baseline + bug-class audit) → units U1–U13 (P0 → P1 → P2).

## Decisions (from the user, 2026-09-30)
- AI presets: Google Gemini, OpenRouter, Cerebras, Mistral, GitHub Models + "Custom (OpenAI-compatible)".
- MCP HTTP: separate `caspian-mcp --http` process, bearer token in Credential Manager, 127.0.0.1 unless LAN;
  every call audited to `Logs\mcp-audit.log` on that PC (DB stays read-only for the read tools).
- MCP draft-only write tools: yes, off by default (admin switch); never post/cancel/delete.
- Backups without a password: prompt at first run but allow skipping; then unencrypted backups with a warning.
- Defaults accepted with the plan: stocktake override = admin PIN; manual-entry grid in imports; brand/size fields
  deferred; future dates rejected; units allow decimals unless marked integer-only; prefixes ر ح ت ص م ا ب;
  printer prints and PDF saves count as prints (preview and drafts don't).

## Log

### Phase 1 — baseline (2026-09-30)
- `uv run pytest -q` (offscreen, SQLite): 222 passed, 6 skipped (MariaDB tests need credentials).
- Full suite with local MariaDB 11.8: **228 passed**. `uv run ruff check .`: all checks passed.
- No pre-existing failures to fix.

### Phase 1 — bug-class audit (2026-09-30)
- **#1 asyncSlot signatures** (`b9dbef4`): real cause is PySide 6.11 calling qasync's `(*args)` wrapper with *no*
  arguments; only bound methods survive. New `ui/tasks.py` (`spawn`/`callback`); `output_menu` uses it.
  Tests: `test_printing_ui.py`, AST guard `test_ui_guards.py::test_async_slots_are_methods` (only 2 offenders existed).
- **#2 silent errors** (`8ce9770`): exception-hook dialog was garbage-collected. `messages.show_unexpected_error`
  (Persian, log path, details, copy button); parentless boxes kept alive; dead boxes pruned. Tests: `test_messages_ui.py`.
- **#6 file names** (`a80b311`): `core/files.py` (`safe_filename`, Documents default, last folder in settings.json).
  Tests: `test_files.py`.
- **#35 blocking dialogs** (`957cbd7`): `ui/file_dialogs.py` awaitable save/open/print dialogs; reports Excel,
  PDF/print, stocktake sheets, file import use them. Tests: `test_file_dialogs_ui.py`, AST guard
  `test_no_blocking_dialogs_inside_coroutines`, Excel export test.
- Other bug classes audited: broad `except Exception` in ui/ all show a message; no other FK blocks draft deletion.

### U1 — draft deletion from imports (#3)
- Migration `5b7e2c41d9a3`: `import_batches.result_document_id` → `ON DELETE SET NULL` (down restores plain FK).
- `documents.delete_draft` → `imports.reopen_for_deleted_document`: batch back to OPEN, lines re-matched (no
  duplicate items on re-apply), audit `import.batch_reopened`.
- `db/migrate.py`: `downgrade()`; SQLite migrations run with `foreign_keys=OFF` + `foreign_key_check` — without it
  the table rebuild cascaded and deleted `import_lines` (proven by a probe; caught by the new round-trip test).
- Tests: `test_imports.py::test_deleting_import_draft_reopens_batch`, `test_migrations.py` (up→down→up keeps all
  rows on SQLite and MariaDB, schema matches models, DB-level SET NULL, delete_draft on MariaDB).
