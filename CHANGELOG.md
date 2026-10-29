# Changelog

All notable changes to Caspian Warehouse. Versions follow `pyproject.toml`; installers are on
[GitHub Releases](https://github.com/alirezamolayari-prog/Caspian_Warehouse/releases).

> **Before updating, take a backup** (Settings → پشتیبان‌گیری → «پشتیبان‌گیری اکنون»).
> Database changes run automatically on the first start of the new version and can't be undone
> from inside the app.

## 1.2.0 (2026-10-29)

### Server sync
- **A request is no longer stuck in «در صف ارسال» after the server refused the account.** 401/403
  (wrong password, account waiting for approval, rejected, disabled, password must be changed, too
  many tries) is an *account* problem: the queue waits and is sent automatically once the account is
  fixed. Requests an older version had marked failed for this reason are put back in the queue on
  the first start.
- A request the server really refused gets a working «ارسال مجدد» button; it is sent again with the
  same id, so the server never makes a second request.
- MariaDB «Record has changed since last read» (error 1020, default since MariaDB 11.6) and
  deadlocks are retried quietly. Writes to requests are short transactions; opening a request no
  longer reads and then writes the row. `innodb_snapshot_isolation` is left on: it does not exist
  on MariaDB 10.6–11.3 (setting it would break those installs) and it protects stock from lost
  updates.
- «آزمایش اتصال» logs in **and** makes an authorised call, and names the exact problem
  (wrong password, waiting for approval, rejected, disabled, must change password, too many tries
  with the waiting time, server unreachable).
- Clicking «سرور: …» in the status bar retries at once and opens the connection settings (admins);
  its tooltip shows the last problem in Persian.

### Personal accounts (register with an SMS code, log in with your own password)
- New sign-in window: «شماره موبایل» + «رمز عبور» (optional «مرا روی این کامپیوتر به خاطر بسپار»),
  «ثبت‌نام» in three steps (Persian name + mobile → 6-digit SMS code with countdown and «ارسال
  دوباره» → password with the rules ticked live) and «رمز را فراموش کرده‌ام». A new account waits for
  the admin's approval in the server panel; «تغییر رمز» works without SSH, also when the server
  requires it.
- Names are cleaned and checked exactly like the server (ي/ى→ی, ك→ک, no kashida or vowel marks,
  ZWNJ kept inside names such as «اسماعیل‌زاده»); mobile numbers are accepted with Persian/Arabic
  digits, spaces, dashes, +98 or 0098. Every server answer is shown as one Persian sentence.
- The first server sign-in on a PC makes a local user with the minimal role «کاربر تازه»; the admin
  gives permissions on «کاربران» (which now shows each person's mobile).
- Every request and chat message is sent under the account of the person who registered it — also
  after they log out (their login is kept in the Windows credential store). If it has expired, the
  request shows «منتظر ورود …» until they sign in again; it is never sent under someone else's name.
  Requests of people without a personal account (and older ones) still go through the shared account.
- Offline: someone who signed in on this PC before can sign in with the same password while the
  server is unreachable (strong hash, same lockout rules); requests queue as usual.
- Once a server is set up, a local username works only for admins (emergency fallback).
- Settings ← اتصال به سرور: the shared account is now «حساب مشترک (قدیمی)» and may stay empty; the
  server address is under «تنظیمات پیشرفته», prefilled with https://api.caspianmoblserver.ir.

### Goods requests and the issue slip (حواله)
- **A slip of an approved request is finalised only when it is really printed.** «پیش‌نمایش (بدون
  ثبت)» shows it with a big diagonal «پیش‌نویس» and nothing changes; «چاپ» asks once, opens the
  printer choice, and only then finalises and prints with the final number. Cancelling the printer
  or the PDF file leaves it a draft. If the printer fails after finalising, the next print is still
  the original; reprints carry «المثنی»; the approver's name never appears.
- A locked slip looks locked: no search box, no «✕» on rows, greyed quantities and a clear note
  «اقلام این حواله از درخواست تأییدشده آمده و قابل تغییر نیست».
- Requests list: «در صف» instead of «—» until the server answers; new «ثبت‌کننده» (the server's
  name, or the local author while queued) and «وضعیت ارسال» columns; «توضیحات» keeps only the
  user's text; the full error is in the tooltip and the details window; «درخواست‌های من» filter.
  The details title reads «درخواست در صف ارسال — …» / «درخواست شمارهٔ ۱۲ — …».
- Chat: Jalali date with the time, «در صف ارسال» then «ارسال شد» on our messages, owners only as
  «مالک», bubbles redrawn in the current theme.
- New request form: common places (toilets of each floor, parking, floors) before typing,
  «+ افزودن محل جدید» for location managers, «پلاک ۶ مربوط به فاز ۱ است؛ این نرم‌افزار فقط برای
  فاز ۲ است»; live item suggestions while typing (code — name — unit — stock); a quantity above the
  stock is marked on its row (not blocked: the owner decides); the connection notice is a coloured
  bar with «ورود / تنظیم الان».

### Everyday screens
- The assistant is called «دستیار کاسپین» everywhere; the small sparkle button at the top is gone
  (it only repeated the sidebar entry).
- Numbers are typed and shown with Persian digits in every box (also the small number boxes in
  settings and reports), with «٬» between thousands while typing (۱۵۰٬۰۰۰); Latin digits typed are
  converted. The stocktake list shows «۰ از ۶».
- Settings: leaving a tab or the page with unsaved changes asks first.
- Dark mode: chat bubbles and toasts follow the theme (also after switching); the logo's white
  background is made transparent instead of sitting in a white box.
- Dashboard: «در انتظار تأیید مالک» and «تأییدشده — منتظر چاپ حواله» cards open the requests list
  with that filter; a filter applied from a card shows as «فیلتر: … ✕» and is cleared with one click.
- Text import: «۵ تا», «۵ دونه», «۵ عدد» or no unit use the item's base unit (shown in the review);
  after «اعمال» there are just two choices: «ثبت نهایی» and «بعداً (پیش‌نویس بماند)».
- Stocktake: uncounted rows are marked when «ثبت نهایی شمارش» is refused, and the final step asks
  for confirmation.
- Reports appear by themselves when a tab opens or a filter changes (the heavy cardex and burn-rate
  analysis keep their button). «مصرف به تفکیک محل» lists floors from the roof down to the basement,
  then parking P1–P4. «ارزش تقریبی» uses the voucher's own price, or the last receipt that really had
  a price (a newer receipt entered without a price no longer makes it ۰).
- Backups (Settings ← پشتیبان‌گیری): warnings when the folder is on the Windows drive, the drive is
  low on space, the daily backup is off, or the last backup is older than 7 days — each with a button
  to the setting. Nothing is changed automatically.

### «دستیار کاسپین» through Caspian Server
- The assistant now works through Caspian Server with each person's own account — no AI provider or
  key is needed on the PC any more («هیچ سرویس هوش مصنوعی تنظیم نشده است» is gone). The app sends
  the conversation and its tools, runs every tool itself on its own data, and sends the results back;
  the server adds its own instructions. At most five tool rounds per question.
- «سهم امروز شما: … از …» is shown on the assistant page. Every server answer is a Persian sentence:
  daily limit used up (yours or everyone's), conversation too long, empty message, assistant not
  available, app/role not allowed, password must be changed, not signed in, server unreachable.
- Local AI providers stay as an optional advanced fallback (Settings ← هوش مصنوعی (پیشرفته) →
  «استفاده از سرویس محلی»), off by default.
- Unchanged rules: the assistant only prepares drafts and requests that you confirm, never approves
  for an owner, never posts or prints a slip, never deletes or does protected work.

### Error messages
- The unexpected-error window is Persian only: one sentence, the log path, and «کپی جزئیات برای
  پشتیبانی» (copies the technical details). No English, SQL or «Show Details…». The same error
  within a few seconds shows a single window.

## 1.2.0rc1 — release candidate (2026-10-25)

First installer for go-live **without Caspian Server**: everything works with the server address
empty or the server unreachable; goods requests wait on the PC and are sent later.

### Pre-launch bug hunt (docs/BUG_LIST.md)
- Dialogs always open at least as large as their contents (the document editor was squeezed).
- Persian digits beside line tables, in the version, the backup list and the AI tab.
- Full cell text on hover in every list; «ثبت‌کننده» shows the full name.
- The server tab and the first-run database dialog explain, in Persian, that the server may stay empty
  and what single-PC and network mode mean.

### Works without the server
- A hanging or missing Caspian Server never holds up the app (timeouts; receipts post at once).
- The requests page says plainly that requests are kept on this PC; no red status while no server is set.

### Fixes from the pre-launch test report (docs/TEST_REPORT.md)
- **A deactivated user, or one whose role changed, is stopped at once** — even in a window that was
  already open on another PC. Every change re-checks the account in the database; the window returns
  to the login screen with the reason (within a minute when idle).
- **Changing a unit's factor no longer posts old drafts wrongly.** A draft saved with «1 کارتن = 24»
  is refused after the factor became 12, with a message saying so; open the draft, check it and save
  it again.
- **Unusual dates are questioned, not refused.** Saving or posting a document dated outside the
  current fiscal year, or more than 30 days ago, asks «با همین تاریخ ادامه می‌دهید؟» first.
- Persian digits in every message («ردیف ۱»، «ساعت ۲۰:۰۰»، years, counts, minutes…); a test guards it.
- Security updates: certifi 2026.10.23, opentelemetry-api 1.45.1, setuptools 84.2.1.

### Goods requests approved by the owners (درخواست کالا, Caspian Server)
- New page «درخواست کالا» (permission `requests.manage`, given to admin and manager): requester picked
  from persons (quick «new person» with name and phone), place from the location picker, items from
  the item list. Requests are saved at once and sent in the background; offline they wait «در صف
  ارسال» and go out automatically, never twice (`client_request_id`).
- Statuses, chat per request, notification and a badge when an owner decides; who decided is never
  shown. Rejected requests can be corrected and sent again.
- Approval creates a draft issue voucher with exactly the approved lines; it can't be edited, deleted,
  printed or posted before approval. Printing posts it (after a question) and reports «issued» with the
  voucher number to the server.
- Settings → «اتصال به سرور» (admin): URL and account; the password stays in Windows Credential
  Manager. Status bar «سرور: متصل / قطع».
- The assistant can prepare a request (sent only after «بله، بفرست») and list statuses.

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
