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

### U2 — document print/preview + print tracking (#4, #5)
- Migration `8d31f0a6c2e4`: `documents.print_count` (default 0), `last_printed_at`, `last_printed_by_id`.
- Service: `documents.print_sheet()`, `record_print()` (audit `document.printed` with copy no. and printer/pdf;
  drafts refused), `DOC_PREFIX` / `number_text()` (ر ح ت ص م ا ب — also used later by #26).
- `ui/document_print.py`: A5/A4 form (header, company, type + number, date, warehouses, party, lines, amounts,
  totals, three signature boxes), «کپی / المثنی (نسخه n)» from the 2nd copy, «پیش‌نویس — فاقد اعتبار» for drafts,
  «ابطال شده» for cancelled; QPrintPreviewDialog awaited (counted only if printed); PDF export counted.
- «چاپ / پیش‌نمایش» menu in the documents list and the document dialog; list status shows «چاپ‌شده (n)».
- Found while checking the rendering: QTextDocument always lays table columns out left-to-right, so every
  printed report had its columns mirrored. `printing.rtl_cells()` emits cells in visual order (reports, stocktake
  sheets, document forms); report tables now span the page. A test fails if Qt ever starts mirroring itself.
- Tests: `test_documents.py` (sheet, record_print, drafts), `test_document_print_ui.py`, `test_printing_ui.py`.
- Note: CI runs only on `main`/pull requests; it will run when a PR is opened for this branch.

### U3 — stock integrity: freeze, carton scan, import → post (#7, #8, #9)
- #7 `documents._stocktake_override`: posting or cancelling a document that moves an item counted by an OPEN or
  COUNTED stocktake (source or destination warehouse) raises `StocktakeFrozen`; items outside a category-scoped
  stocktake and other warehouses are unaffected; the stocktake's own adjustment is exempt. New protected action
  `STOCKTAKE_OVERRIDE` (admin PIN, never AI); audited on `document.posted/cancelled` with `approved_by_id`.
  UI: `documents_page.with_stocktake_override` offers the PIN dialog and retries (list, editor, cancel, import).
  Existing test `test_movements_after_snapshot_are_flagged` now posts through the override (its movement is only
  possible that way now).
- #8 `stocktake.scan()` → line + factor of the scanned barcode's unit; a carton scan adds `factor` base units.
- #9 after applying a stock import: «ثبت نهایی همین حالا» / «باز کردن سند» / «بعداً» (`messages.ask`, awaitable);
  `documents.pending_incoming/pending_hint`: issue/transfer/loan forms and the "insufficient stock" error say
  «۵ عدد در پیش‌نویس ر-۱ منتظر ثبت نهایی است».
- Tests: `test_stocktake.py` (freeze, scope, override, AI refused, approve exempt, scan factor),
  `test_documents.py` (pending), `test_stocktake_ui.py` (carton ×2 = 24), `test_imports_ui.py` (post/open/later),
  `test_documents_ui.py` (PIN override, pending hint), `test_messages_ui.py` (ask).

### U4 — searchable pickers + single item search (#10, #11)
- `widgets.SearchableCombo`: editable combo + completer matching normalized text (Arabic ي/ك = Persian ی/ک,
  ZWNJ, digits), contains-match, Enter selects (never submits the form), text reverts if nothing matches,
  `unmatched(text)` signal, optional «+ افزودن …» entry kept last (`enable_add`).
- Used for persons (documents, imports; «+ افزودن شخص جدید» opens the person form, `master_page.person_picker`),
  warehouses, item group (form + filter), stocktake scope, category parent, all report filters.
- #11 cardex: one field (code / name / barcode); Enter on a barcode or code looks it up; the item list follows the
  selected fiscal year and keeps the selection.
- Tests: `test_widgets_ui.py`, cardex/filters in `test_reports_ui.py`, add-person in `test_documents_ui.py`;
  two existing tests that drove the removed cardex search box now use the new field (same assertions).

### U5 — opening stock on the new-item form (#12)
- `items.create_item_with_opening(data, OpeningStock(warehouse, qty, price))`: item + OPENING document in one
  transaction, posted when the user may post (else left as a draft, and the user is told); never writes stock.
- Item form (new items, users who may create documents): «موجودی اولیه» quantity / warehouse / price.
- Tests: `test_items.py` (posted OPENING + balance + cardex row, draft without post right, no qty = no document,
  invalid qty rolls back the item too), `test_items_ui.py` (form → posted OPENING; edit form has no fields).

### U6 — imports: manual entry + similar-item suggestions (#13, #14)
- #13 «ورود دستی…»: editable grid (کد، نام، بارکد، مقدار، واحد، فی) → batch with source `MANUAL` → the same review;
  link «ثبت مستقیم رسید ورود» opens a new receipt in «اسناد انبار». `ImportSource.MANUAL` needs no DDL
  (non-native enum, VARCHAR(20)).
- #14 review: choices «همان کالا: X (۹۵٪)» (top 3 with %), «کالای جدید: …», «نسخه جدید از: X (برند/سایز دیگر)»
  → name prefilled «X - », editable (`imports.create_variant`, `variant_name`); approximate matches keep their
  alternatives; the code column shows the matched item's code (muted) when the file had none.
- Deferred (optional in the spec): structured `brand` / `size` fields on items (would need a migration).
- Tests: `test_imports.py` (manual source, match code + alternatives, variant), `test_imports_ui.py` (grid,
  matched code, variant flow; labels updated in the existing review test).

### U7 — user administration from the «مدیر سیستم» menu (#15)
- Header menu: «پروفایل من» (username, name, role + change password), «تغییر رمز عبور», «مدیریت کاربران» and
  «کاربر جدید» (users.manage only), PIN, switch user, «خروج از حساب», about.
- `users.rename_user` (unique, validated, audited `user.renamed`); «ویرایش کاربر» edits login name + full name;
  renaming yourself updates the header.
- `create_user` reports a taken name before password rules and no longer burns an admin approval on a duplicate;
  `users.username_taken`; the new-user form warns as soon as the name is typed.
- Last-admin guards already existed (`set_active`, `change_role`); added a test for deactivation by another manager.
- Tests: `test_auth.py` (rename, duplicate first, last admin), `test_ui_shell.py` (menu, profile, rename, early check).

### U8 — AI providers (#16)
- Checked against official docs on 2026-09-30: Gemini `https://generativelanguage.googleapis.com/v1beta/openai`
  (`gemini-3.8-flash`), OpenRouter `https://openrouter.ai/api/v1` (`openrouter/free` = free-model router; optional
  `HTTP-Referer` / `X-Title` sent), Cerebras `https://api.cerebras.ai/v1` (`gpt-oss-120b`), Mistral
  `https://api.mistral.ai/v1` (`mistral-small-latest`); Groq `llama-3.3-70b-versatile` + `whisper-large-v3` still
  production. **GitHub Models was not added: GitHub retired it on 2026-07-30** (catalog + inference API gone).
- New `ProviderKind`s GEMINI, OPENROUTER, CEREBRAS, MISTRAL, CUSTOM («سفارشی (سازگار با OpenAI)», key optional); no
  DDL needed (non-native enum, VARCHAR(20)); free-tier presets listed first.
- 401 → «کلید API نامعتبر است (401)», 403 → «دسترسی از منطقه شما مسدود است؛ VPN…», 404 → address/model; the test
  connection shows the server's own message with the key masked.
- «دریافت لیست مدل‌ها» (`Gateway.list_models`, GET /models) fills a searchable, still free-typed model field;
  OpenRouter «فقط رایگان» filter (`:free`, zero pricing, `openrouter/free`). Empty AI tab explains how to start.
- "List was empty at test time": no code defect found; the log shows no save attempt. Every preset now has a
  save → list regression test.
- Tests: `test_ai_gateway.py` (401 vs 403 + server text + key masked, presets, save/list all kinds, OpenRouter headers,
  model list + free flag, list errors, dialog fetch + filter + save).

### U9 — MCP (#17)
- `caspian-mcp --http [--port 8765] [--lan]`: Streamable HTTP at `/mcp` (stateless, JSON), bound to 127.0.0.1
  unless LAN is ticked (0.0.0.0); every request needs `Authorization: Bearer <token>` (constant-time compare,
  401 + `WWW-Authenticate: Bearer` otherwise); the SDK's DNS-rebinding protection stays on for localhost. Random
  token (`secrets.token_urlsafe(32)`) in Credential Manager (`mcp:http_token`), generated from Settings.
- Audit: every tool call (tool, args, caller = `stdio` / `http <ip>`, ok/error) and every rejected request goes to
  `Logs\mcp-audit.log` on that PC (rotating); the read path stays on the read-only DB connection.
- Draft-only write tool `create_draft_document` (RECEIPT/ISSUE, lines by item code), only when an admin ticks
  «اجازه ساخت پیش‌نویس سند از MCP» (`app_settings.mcp_allow_drafts`, audited, AI can't change it); runs as an AI
  actor with `documents.edit` only → posting/cancel/protected actions impossible; description «ساخته‌شده از MCP».
- Settings card: «تست MCP» (spawns the exe over stdio and lists tools), HTTP port / LAN / start–stop + «در حال
  اجرا / متوقف» + address, token show/copy/new, «کپی تنظیمات MCP» = JSON **and**
  `claude mcp add caspian-warehouse -- "<…>\caspian-mcp.exe"`. The started server stops with the app.
- Tests: `test_mcp.py` (token required incl. wrong/Basic, audit lines with caller, DNS-rebinding refusal, drafts only
  when enabled, never post), `test_automation_ui.py` (copy text, token, switch, test-button errors, real stdio probe).
  Live check on this PC: stdio probe lists the 6 read tools. Tests write logs to a temp folder (conftest).
