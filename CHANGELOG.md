# Changelog

All notable changes to Caspian Warehouse. Versions follow `pyproject.toml`; installers are on
[GitHub Releases](https://github.com/alirezamolayari-prog/Caspian_Warehouse/releases).

> **Before updating, take a backup** (Settings → پشتیبان‌گیری → «پشتیبان‌گیری اکنون»).
> Database changes run automatically on the first start of the new version and can't be undone
> from inside the app.

## Unreleased — owner approval phase

### Fixes from the pre-launch test report (docs/TEST_REPORT.md)
- **A deactivated user, or one whose role changed, is stopped at once** — even in a window that was
  already open on another PC. Every change re-checks the account in the database; the window returns
  to the login screen with the reason (within a minute when idle).
- **Changing a unit's factor no longer posts old drafts wrongly.** A draft saved with «1 کارتن = 24»
  is refused after the factor became 12, with a message saying so; open the draft, check it and save
  it again.
- **Unusual dates are questioned, not refused.** Saving or posting a document dated outside the
  current fiscal year, or more than 30 days ago, asks «با همین تاریخ ادامه می‌دهید؟» first.

### Part A — consumption locations (محل مصرف)
- Floors and consumption locations of Caspian Phase 2 (141 plates and named places), seeded
  automatically from `data/seed/caspian_locations.csv`; codes are the keys shared with the future
  server and the Caspian Owner app.
- «محل مصرف» and «شرح مصرف» on issue vouchers: one keyboard-driven field (type `68`, Enter →
  «پلاک ۶۸ · طبقه همکف»); stairs, elevators and «مشاعات عمومی طبقه» ask for the floor; Phase 1 plates
  are refused. The floor is stored on each voucher, so later edits never change history.
  Posting an issue requires a place (setting, on by default).
- اطلاعات پایه → «محل‌های مصرف»: add, edit, deactivate, nicknames, CSV re-import with a preview,
  Excel export (admin only for changes).
- Report «مصرف به تفکیک محل» (by floor or location, date range, item, requester), Excel/PDF,
  scheduled sending; the AI assistant and the MCP server can answer consumption questions.

### Fixes after testing Part A
- **The assistant never posts any more.** It only prepares drafts; a confirmation card in the chat
  («بله، ثبت کن» / «نه») lets you post it under your own name or throw it away. Ambiguous items,
  quantities, people, places and floors are asked, not guessed. The setting «اجازه ثبت نهایی سند توسط
  دستیار» was removed.
- Printed vouchers keep «محل مصرف: پلاک ۶۸ · طبقه همکف» and colons in the right place.
- The voucher editor lists all problems at once, marks the fields red and asks «آیا مطمئنید» only
  when everything is in order.
- Links to documents in the assistant's replies, and a «باز کردن سند» button on the card.
- Floors always read «طبقه …» (parking: «پارکینگ P1»); item codes show Persian digits.
- Dates after today are refused by the database server's clock too; a warning appears when this
  PC's clock is off.

### Printed document form
- Logo and company name in the header (branding in one module: `caspian/branding.py`), also on the
  login dialog and in the sidebar.
- Recipient's phone number, «جمع مقدار» per unit, centred number columns with the widest «نام کالا»,
  four signature boxes («مدیریت» added), «چاپ‌کننده» and the print date and time in the footer.
- Right-to-left text now prints correctly on real printers (pages are painted with the screen's
  text engine), and «ذخیره PDF» fills the page instead of a small corner.

## 1.1.0 — 2026-10-04
- Daily automatic backup at a chosen time, with one catch-up run after a missed day.
- Similar-item hint in the New Item dialog loads the existing item instead of closing.
- Gemini speech-to-text; restore closes the app cleanly.
- QA rounds 1 and 2: duplicate-item prevention and PIN-protected merge, assistant chat history,
  concurrent document numbering, clearer AI error messages and more (see docs/QA_FIX_PLAN.md).

## 1.0.0 — 2026-09-28
- First public release.
