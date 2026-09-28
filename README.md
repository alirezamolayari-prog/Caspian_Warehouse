# Caspian Warehouse · انبار کاسپین

Offline-first, AI-assisted inventory management for Windows — Persian (right-to-left)
interface, Jalali calendar, dark and light themes. Built for Caspian Furniture Market and
suitable for any shop or warehouse.

**[⬇ Download the latest version](https://github.com/alirezamolayari-prog/Caspian_Warehouse/releases/latest)**

![Dashboard](docs/screenshots/dashboard.png)

## Features

- **Items & stock** — barcodes, multiple units (box of 24 → pieces), categories, reorder points,
  several warehouses, fast Persian search.
- **Warehouse documents** — receipts, issues, transfers, adjustments, loaned/returnable items;
  scanner-friendly entry; posted documents are never deleted, only cancelled with history kept.
- **Draft-first imports** — Excel, CSV, Word, barcode scanning or typed text; every row is
  reviewed (new / match / conflict / error) before anything is saved.
- **Blind stocktake** — printable count sheets without system quantities, discrepancy report,
  automatic adjustment on approval.
- **Reports** — stock value, item history (cardex), open loans, user activity, usage-based
  reorder suggestions; Excel and PDF export.
- **AI assistant (optional)** — chat or voice; answers from your data, prepares drafts, writes
  purchase requests. It can never post, delete or bypass the admin PIN. Works with
  OpenAI-compatible services, Groq, Hugging Face or a local model (Ollama) — the rest of the
  app is fully offline.
- **Security** — user accounts and roles, admin PIN for sensitive actions, full audit trail.
- **Backups & year-end** — encrypted backups (scheduled or manual), PIN-protected restore,
  fiscal year closing with read-only archives.
- **Multi-user** — several PCs on a local network share one database.

| | |
|---|---|
| ![Items](docs/screenshots/items-dark.png) | ![Document editor](docs/screenshots/document-editor.png) |
| ![Import review](docs/screenshots/import-review.png) | ![Reorder analysis](docs/screenshots/reorder-analysis.png) |

## Download & Install

**Requirements:** Windows 10 or 11 (64-bit). No Python or other tools needed.

1. Open the **[latest release](https://github.com/alirezamolayari-prog/Caspian_Warehouse/releases/latest)**
   and download `CaspianWarehouse-Setup-<version>.exe` under **Assets**.
2. Run it. If Windows shows *"Windows protected your PC"*, click **More info → Run anyway**
   (the installer is not code-signed yet).
3. On the **main (or only) computer**, keep **"Install the database server (MariaDB) on this
   PC"** ticked and choose a database password. **Write this password down** — it's needed
   for year-end closing and for restoring on a new computer.
   On other computers of a network, untick it and follow the [network guide](docs/LAN_SETUP.md).
4. Start **Caspian Warehouse** from the Start Menu. Log in with **admin / admin**; you'll be
   asked to choose a new password and an admin PIN.
5. In **Settings → Backups**, choose a backup password (and write it down, too).

**Updating:** download and run the new installer — your data and settings are kept.
**Uninstalling:** *Settings → Apps* → Caspian Warehouse. Your data (database, settings,
backups in *Documents\Caspian Backups*) is kept.

Full guide (Persian): [docs/USER_GUIDE.md](docs/USER_GUIDE.md)

### دانلود و نصب (فارسی)

1. از صفحه **[آخرین نسخه](https://github.com/alirezamolayari-prog/Caspian_Warehouse/releases/latest)** فایل
   `CaspianWarehouse-Setup-<نسخه>.exe` را از بخش **Assets** دانلود کنید.
2. آن را اجرا کنید. اگر ویندوز پیام «Windows protected your PC» داد، روی **More info** و سپس **Run anyway** بزنید.
3. روی **رایانه اصلی** گزینه نصب پایگاه داده (MariaDB) را فعال بگذارید و یک رمز برای پایگاه داده انتخاب و **یادداشت** کنید.
   روی سایر رایانه‌های شبکه این گزینه را غیرفعال کنید و [راهنمای شبکه](docs/LAN_SETUP.md) را ببینید.
4. برنامه را از منوی Start باز کنید و با **admin / admin** وارد شوید؛ سپس رمز جدید و PIN مدیر را تعیین کنید.
5. برای به‌روزرسانی، نسخه جدید را نصب کنید؛ اطلاعات شما حفظ می‌شود.

[راهنمای کامل کاربری](docs/USER_GUIDE.md)

## For developers

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync            # install dependencies
uv run caspian     # start the app
uv run pytest      # run tests
uv run ruff check  # lint
```

**Database.** The app uses MariaDB (10.6+, developed on 11.8 LTS). On first start it asks for the
connection details (or creates its own account from an admin one), creates the database and runs
migrations. Passwords are stored in Windows Credential Manager, never in files. Tests use SQLite;
to also run the MariaDB integration tests, point them at a throwaway database (it will be wiped):

```powershell
$env:CASPIAN_TEST_MARIADB_URL = "mariadb+aiomysql://user:pass@localhost:3306/caspian_test?charset=utf8mb4"
$env:CASPIAN_TEST_MARIADB_ROOT = "root:<admin password>"   # optional, for account/archive tests
uv run pytest
```

After changing models: `uv run alembic revision --autogenerate -m "describe change"`.

**Where the app keeps data** (outside the program folder, so upgrades never touch it):
settings and logs in `%LOCALAPPDATA%\CaspianWarehouse`, backups in `Documents\Caspian Backups`,
secrets in Windows Credential Manager, business data in MariaDB.

### Building the installer

Needs [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -DownloadMariaDb
# -> dist\installer\CaspianWarehouse-Setup-<version>.exe  (+ SHA256SUMS.txt)
```

Use `-SkipServer` for an app-only installer. `packaging\test_installer.ps1` (elevated) checks
install, Apps & Features entry, upgrade, uninstall and — with `-WithDatabase` — the bundled
database server.

### Releasing

The version lives only in `pyproject.toml`. To publish:

1. Set `version = "1.0.0"` in `pyproject.toml`, commit and push to `main`.
2. `git tag v1.0.0` and `git push origin v1.0.0`.

The **Release** workflow tests, builds, installs the installer on a clean Windows machine and
publishes a GitHub Release with the installer attached. Pushing a branch named `release-*`
runs the same checks without publishing.

### Read-only MCP server

External AI clients can query the inventory through the Model Context Protocol:

```json
{"mcpServers": {"caspian-warehouse": {"command": "C:\\Program Files\\Caspian Warehouse\\caspian-mcp.exe"}}}
```

It only exposes read tools (item search, stock, reorder analysis, documents, loans, reports).
Create the read-only database user from *Settings → AI* to also enable ad-hoc `SELECT` queries;
that user can't read the `users` table and every MCP connection is read-only.

## License

Proprietary — © 2026 Caspian Furniture Market. You may download and use the official installers
free of charge; the source code is published for transparency only. See [LICENSE](LICENSE).
Third-party components and their licenses: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
