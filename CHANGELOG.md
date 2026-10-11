# Changelog

All notable changes to Caspian Warehouse. Versions follow `pyproject.toml`; installers are on
[GitHub Releases](https://github.com/alirezamolayari-prog/Caspian_Warehouse/releases).

> **Before updating, take a backup** (Settings → پشتیبان‌گیری → «پشتیبان‌گیری اکنون»).
> Database changes run automatically on the first start of the new version and can't be undone
> from inside the app.

## Unreleased — owner approval phase

### Part A — consumption locations (محل مصرف)
- Floors and consumption locations of Caspian Phase 2 (141 plates and named places), seeded
  automatically from `data/seed/caspian_locations.csv`; codes are the keys shared with the future
  server and the Caspian Owner app.
- «محل مصرف» and «شرح مصرف» on issue vouchers: one keyboard-driven field (type `68`, Enter →
  «پلاک ۶۸ · همکف»); stairs, elevators and «مشاعات عمومی طبقه» ask for the floor; Phase 1 plates
  are refused. The floor is stored on each voucher, so later edits never change history.
  Posting an issue requires a place (setting, on by default).
- اطلاعات پایه → «محل‌های مصرف»: add, edit, deactivate, nicknames, CSV re-import with a preview,
  Excel export (admin only for changes).
- Report «مصرف به تفکیک محل» (by floor or location, date range, item, requester), Excel/PDF,
  scheduled sending; the AI assistant and the MCP server can answer consumption questions.

## 1.1.0 — 2026-10-04
- Daily automatic backup at a chosen time, with one catch-up run after a missed day.
- Similar-item hint in the New Item dialog loads the existing item instead of closing.
- Gemini speech-to-text; restore closes the app cleanly.
- QA rounds 1 and 2: duplicate-item prevention and PIN-protected merge, assistant chat history,
  concurrent document numbering, clearer AI error messages and more (see docs/QA_FIX_PLAN.md).

## 1.0.0 — 2026-09-28
- First public release.
