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
