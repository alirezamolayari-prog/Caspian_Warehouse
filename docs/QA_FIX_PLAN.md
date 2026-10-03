# QA round 1 — fix plan (branch `fix/qa-round-1`)

## Context
A manual end-to-end QA of the build from `qa-fixes-2026-09` found 23 issues: the AI assistant cannot do a
whole job (receiver lost, duplicates, ambiguous items, misleading status/voice), plus data-integrity,
validation, printing and polish problems. Goal: fix all 23 without breaking anything, then merge into `main`
(which brings in qa-fixes-2026-09 too — decided by the user) and push; no tag/release, no version bump.

## Setup
- `git status` clean → `git checkout -b fix/qa-round-1 qa-fixes-2026-09`.
- First commit: `docs/QA_FIX_PLAN.md` (this plan, per issue: files + test), updated with ✅ as phases land.
- Per phase: tests first → fix → `uv run ruff check`, full pytest (offscreen, + local MariaDB via the
  keyring runner), `uv run caspian --smoke-test` → one commit (`git commit -F`), push the branch.

## Phase 1 — AI assistant (#1–#8) ✅
Verified causes: `ai/tools.py::_create_stock_draft` has no person arg and only builds an import batch;
`imports.evaluate` auto-matches `best >= 99` even when the 2nd score is equal; `Gateway.chat` raises a
generic error and drops `last_attempts`; `assistant_page.on_mic` auto-sends; `ProviderDialog._fill_models`
re-sets the text but `setEditText` after `clear()` + list makes the first item win.
1. **Person resolution** (`services/ai/tools.py`): new helper `_resolve_person(name)` — `master.search_persons`
   + rapidfuzz on normalized names; one clear match → id; none / several close (Δ<8) → `{"needs_choice":
   "person", "candidates":[…]}` so the model asks. `create_stock_draft` gets `person`.
2. **Act like a user**: new tools `create_document` (RECEIPT/ISSUE/TRANSFER/LOAN_OUT/LOAN_RETURN; person,
   warehouse(s), lines by code/name/unit, resolves items via the same matcher) and `post_document(doc_id)`;
   both call `documents.create_document` / `post_document`, so stock, dates, units, freeze, permissions
   all apply. Returns number_text + link id.
   - **Service guard**: `Actor.require_human(what)` (raises `PermissionDenied`, Persian) called in
     `documents.cancel_document`, `delete_draft`, `items` deactivate/delete/merge, `users.set_active /
     change_role / rename_user / reset_password / create_user`, `master` delete/deactivate, `imports.
     discard_batch`, `stocktake.cancel/approve`, `backup.restore_backup`, `fiscal` close (protected.consume
     already refuses AI — keep). Tests for each (service level, `admin.as_ai()`).
   - **Setting** «اجازه ثبت نهایی سند توسط دستیار» (`app_settings.ai_allow_post`, default ON) in
     `services/ai/config.py` (get/set, admin `ai.configure`, audited) + checkbox in Settings → AI.
     Enforced in `documents.post_document_in` when `actor.is_ai`. When OFF, `post_document` tool is not
     offered and the prompt says "drafts only".
   - Audit already adds `via_ai: True`; add `on_behalf_of` (user display name) — test it.
   - `SYSTEM_PROMPT` rewritten: may create and post, never cancel/delete/…; real menu names
     (داشبورد، کالاها، اطلاعات پایه، اسناد انبار، ورود اطلاعات، انبارگردانی، گزارش‌ها، دستیار هوشمند،
     کاربران، تنظیمات); ask when a tool returns `needs_choice`.
3. **Ambiguous items** (`services/imports.py::evaluate`): top two scores within 2 points (incl. both 100,
   exact-name duplicates) → CONFLICT with all candidates; exact code/barcode still matches. Tool returns
   candidates with code, name, stock → model asks «کدام جارو؟». Tests: two «جارو» items.
4. **No duplicates per turn** (`ai/assistant.py`): `ToolContext.created` registry keyed by a normalized
   signature (tool, doc type, person, warehouse, sorted lines); a repeated create in the same `send()`
   returns the first result with `"duplicate": true`. `send()` wraps the loop: on `AIUnavailable`/error
   after side effects, reply lists what was created (number + «باز کردن» link) instead of only the error.
   `AssistantReply.created_documents` → UI links open the document.
5. **Honest availability** (`gateway.py`): `chat()` raises `AIUnavailable` whose text lists each attempt
   (`Groq: محدودیت درخواست (429)`, `مدل پیدا نشد (404)`, `زمان انتظار`…), reusing `_OUTCOME_TEXT` +
   detail. Status line in `assistant_page` says «N سرویس تنظیم شده» (configured), and after a call
   shows the last outcome — not "ready".
6. **Voice** (`core/recorder.py`, `ui/assistant_page.py`): `rms(wav_bytes)` + `is_silent()` (duration <0.6 s
   or RMS below threshold) → «صدایی شنیده نشد»; transcript goes into the input box (focused, cursor at
   end), never auto-sent. Tests with synthetic WAVs.
7. **Model list** (`ui/settings_page.py`): `_fill_models` keeps the current model; if not in the fetched
   list, keep it and show «مدل فعلی در فهرست این سرویس نیست» (warning status). Test.
8. **Busy + timeout** for «از متن…» and chat: `TextImportDialog`/assistant set busy (inputs disabled,
   status «در حال پردازش…»), run under `asyncio.wait_for(…, AI_TOTAL_TIMEOUT=60)`, fallback to the
   offline parser on timeout; event loop stays free (already async; verify no sync work). Test: slow
   fake gateway → dialog busy, then offline result.

## Phase 2 — Data integrity & business logic (#9–#15) ✅
9. **Newer DB** (`db/migrate.py`, `app.py`, `db/bootstrap.py`): before upgrading, read current revision;
   if unknown to this app's script directory → raise `SchemaTooNew`; `connect_database` shows the Persian
   message and exits without touching the DB. Test: stamp a fake revision in SQLite.
10. **Data health check** (new `services/health.py`, read-only): future-dated documents, documents outside
    the open fiscal year, fractional balances in integer-only units (via `StockBalance` + base unit).
    Dashboard warning card/line when any (click → dialog listing them with how to fix via documents);
    Settings → backup/tools button «بررسی سلامت داده». No data modified. Tests.
11. **Burn rate** (`services/reports.py::burn_rates`): `only_needing_order` = on_hand ≤ configured reorder
    point OR (suggested qty > 0 and on_hand ≤ suggested point) — same rule as `items.count_summary`.
    Test: item with reorder point and zero consumption appears.
12. **Confirmations**: imports «حذف پیش‌نویس», stocktake «لغو» → `messages.confirm(danger=True)`. UI tests.
13. **Duplicate item name**: `items.find_same_name(name, exclude_id)`; `ItemDialog.submit` asks
    «کالای فعال دیگری با همین نام (کد …) وجود دارد. ذخیره شود؟» (Cancelled if no). Tests.
14. **Phone** (`services/master.save_person`): normalize Persian digits; allow digits, leading +,
    spaces, dashes, parentheses; 4–20 digits; Persian error. Tests.
15. **Editor order** (`DocumentDialog.submit`): `collect()` + service-side pre-validation
    (`documents.validate_input`, no write) before the confirm. Test: no lines → error, no confirm.

## Phase 3 — Printing & UI polish (#16–#23) ✅
16. `ui/document_print.py`: fill «تعداد اقلام», put labels/values in separate RTL spans with RLM
    (`\u200F`) around colons and LTR numbers, footer uses full names (`print_sheet` returns display
    names), A5 base 10pt / A4 11pt. Render check + test on HTML.
17. Burn-rate table: text columns Stretch with min widths (code 70, name 180, unit 60), filter row in a
    `FlowLayout`/two rows so the checkbox isn't clipped. UI test on min widths.
18. `FormDialog`: clear status on any edit (connect in `_track_edits`), reserve the status label height
    (`setMinimumHeight` / retain size when hidden via `QSizePolicy.setRetainSizeWhenHidden`). Test.
19. Persian digits for display: item/warehouse codes in tables (`to_persian_digits` in display only),
    import title «متن تایپ‌شده ۱۴۰۵/۰۷/۱۱», line note «ورود اطلاعات — ردیف ۱». Stored codes unchanged.
20. Bidi: helper `core/text.ltr(s)` wrapping with LRI/PDI (`\u2066…\u2069`); used in window titles
    (change role, user dialogs) and in error texts that embed server/English messages. Test helper.
21. «سند جدید» menu: `QToolButton`/`menu.popup` anchored at the button's bottom-right inside the window
    (`setLayoutDirection(RTL)` on the menu). UI test: menu geometry inside main window.
22. Backups: size `KB` under 1 MB; verify message for plain backups «سالم است (بدون رمز)»; file names
    without the Persian label (label stays in the header; existing files still listed — names are not
    parsed except the date pattern). Tests.
23. New item saved → non-blocking status in the Items page («کالای «…» ذخیره شد») via a small
    `widgets.Toast` (auto-hides). Test.

## Finish ✅
Full suite + ruff + smoke; update `CLAUDE.md` (features: AI can post when allowed, health check; known
issues) and `docs/USER_GUIDE.md` (assistant, health check, voice confirm); tick `docs/QA_FIX_PLAN.md`;
merge `fix/qa-round-1` into `main` (merge commit, no rewrite), `git push origin main` and the branch; poll
CI slowly (anonymous API) and fix if red. No tag, no version bump. Summary per issue.

## Risks
- Letting the AI post: guarded in services (setting + permissions + freeze + require_human for destructive
  actions); tests prove AI cannot cancel/delete/deactivate/restore/close.
- Ambiguity rule may turn some previous auto-matches into CONFLICT; existing import tests are kept; any
  test whose data had genuine ties is only adjusted if the new behaviour is the requested one.

---

# QA round 2 (branch `fix/qa-round-2`)

Step 0 keyring isolation; bugs 1 numbering, 2 health vs carried loans, 3 gateway text, 4 six-month
simulation; feature A one item per real product (+ merge); feature B assistant chat history (30 days).
Details: one commit per item, see the log of `fix/qa-round-2` and the summary below as items land.

- [x] Step 0 — in-memory keyring for every test (`tests/conftest.py`), guard `tests/test_keyring_guard.py`
