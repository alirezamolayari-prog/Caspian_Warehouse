# Caspian Warehouse — Roadmap

Offline-first Windows inventory system (Python + PySide6 + MariaDB) with an AI draft generator.
Each milestone ends with passing tests and a push to `main`.

## Guiding rules (apply to every milestone)

- **Draft-first:** nothing from an importer or the AI is posted without human approval.
- **Enforcement lives in the service layer and DB grants**, never only in the UI.
- **Protected actions** (delete/deactivate, import overwrite, merge, restore, role change, year close)
  require confirmation + Admin PIN, checked by a service-layer guard.
- **Every document/transaction records `created_by`**; every change goes to the audit log.
- **Offline-first:** the app never requires internet; AI features degrade gracefully.
- **Persian-first UI:** RTL, Jalali dates, Persian/Arabic character normalization, bundled Vazirmatn,
  dark + light themes.

## Stack

| Concern | Choice |
|---|---|
| UI | PySide6-Essentials (Qt 6) + qasync (asyncio in the Qt event loop) |
| DB | MariaDB (prod), SQLAlchemy 2 async + Alembic; SQLite for fast unit tests |
| Dates | jdatetime (Jalali) |
| Excel/Word | openpyxl, python-docx |
| Fuzzy matching | rapidfuzz |
| AI HTTP | httpx (async), OpenAI-compatible protocol |
| Scheduler | APScheduler |
| Crypto | cryptography (AES-GCM), argon2-cffi (passwords/PIN) |
| Backup | mariadb-dump / mariadb client (GPL, free) + AES-GCM container |
| Packaging | PyInstaller + Inno Setup |
| Tests | pytest, pytest-qt, pytest-asyncio; ruff for lint |

## Milestones

### M0 — Foundation & app shell ✅
Project layout, uv/pyproject, ruff/pytest, logging, user settings store, bundled Vazirmatn,
theme engine (light/dark/follow-system), RTL main window with sidebar navigation,
Jalali + Persian text utilities, GitHub Actions CI (Windows).

### M1 — Data layer ✅
Async SQLAlchemy engine/session, Alembic migrations, first-run DB connection wizard
(single-user localhost or LAN server). Core schema: users, roles, permissions, audit_log,
warehouses, categories, units, unit conversions, items, barcodes, persons (supplier/customer/
employee), documents + lines, stock ledger. `created_by`/`created_at` mixin.

### M2 — Auth, users & protected actions ✅
Login screen, argon2 hashing, default `admin/admin` with forced change, switch user,
roles/permissions, Admin PIN, `@protected_action` guard + PIN dialog, audit trail service,
user management page.

### M3 — Master data UI ✅
Items (with barcodes, units, conversions, reorder point), categories, warehouses, persons.
Fast normalized Persian search, activate/deactivate (protected).

### M4 — Inventory transactions ✅
Receipt, issue, transfer and adjustment documents; posting to the stock ledger; balances
per warehouse; UoM conversion on entry (box → piece); loaned/returnable assets
(loan out, return, outstanding list).

### M5 — Draft-first import pipeline ✅
Generic draft batch + lines with status NEW / EXISTING_MATCH / CONFLICT / ERROR / IGNORED.
Sources: Excel/CSV (column mapping, saved mappings), Word tables (.docx), rapid barcode scan.
Duplicate detection + fuzzy matching, review grid, approve → post (overwrite = protected).

### M6 — Blind stocktake ✅
Count session, printable count sheets without system quantities (PDF), blind entry,
discrepancy report, adjustment document on approval (protected).

### M7 — Reports & reorder analysis ✅
Stock balance, movement/cardex, user activity, loans outstanding — on screen and Excel export.
Manual reorder points + deterministic burn-rate analysis (avg daily usage, coverage months,
suggested order qty) and low-stock dashboard.

### M8 — AI gateway
Provider management page (OpenAI-compatible, Groq, Hugging Face, Ollama/local GGUF),
keys encrypted at rest (Windows DPAPI), priority list, silent fallback on 429/timeout/5xx,
connectivity detection, test-connection button.

### M9 — AI assistant
Always-available chatbox + in-app voice button (record → speech-to-text via configured
provider). Read-only tool calling (stock, items, reports). Persian text → import drafts.
Reorder suggestions with narrative, supplier purchase-request message drafts.
AI can never call write/protected services — enforced by tool registry.

### M10 — MCP, automation & messaging
Read-only MCP server backed by a SELECT-only MariaDB user. Telegram + SMTP email senders.
Scheduler. `.md` instructions → proposed scheduled tasks → human approval → active.

### M11 — Backup & restore
Encrypted backups (mariadb-dump + AES-GCM, `.cwbak`), manual and scheduled, retention,
verify, restore (protected).

### M12 — Fiscal year-end & archives
One schema per fiscal year. Year-end wizard: mandatory backup → choose carry-over (persons,
items, opening stock) → purge stale items option → fresh-start option → export and wipe
audit logs → previous year made read-only. Year switcher + archive queries for reports/AI.

### M13 — Multi-user LAN, packaging & polish
LAN setup guide + concurrency handling (row versioning), installer (PyInstaller + Inno Setup)
with optional bundled MariaDB setup, performance pass, final UX polish, user manual.

## Plan review notes

- Dependency order checked: auth (M2) precedes anything that writes; transactions (M4) precede
  import posting (M5), stocktake (M6) and reports (M7); gateway (M8) precedes AI features (M9–M10);
  backup (M11) precedes year-end (M12), which requires a backup.
- Scheduled backups need the scheduler from M10; M11 wires them in.
- Burn-rate math is deterministic in M7 so it works offline; AI only adds narrative in M9.
- In-app OCR removed by decision: images are converted to Excel/Word externally, then imported (M5).
- No global hotkey by decision: voice is a button inside the app.
