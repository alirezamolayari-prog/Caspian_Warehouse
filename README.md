# Caspian Warehouse

Offline-first, AI-assisted inventory management system for Windows.
Persian (RTL) interface, Jalali calendar, MariaDB backend, dark and light themes.

- User guide (Persian): [docs/USER_GUIDE.md](docs/USER_GUIDE.md)
- Network (multi-user) setup: [docs/LAN_SETUP.md](docs/LAN_SETUP.md)
- Milestone plan: [docs/ROADMAP.md](docs/ROADMAP.md)

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync            # install dependencies
uv run caspian     # start the app
uv run pytest      # run tests
uv run ruff check  # lint
```

### Database

The app needs a MariaDB server (10.6+; developed on 11.8 LTS). On first start it asks for
the connection details, creates the database if needed and runs migrations automatically.
The DB password is stored in Windows Credential Manager, never in a file.

Tests use SQLite by default. To also run the MariaDB integration test, point it at a
throwaway database (it will be wiped):

```powershell
$env:CASPIAN_TEST_MARIADB_URL = "mariadb+aiomysql://user:pass@localhost:3306/caspian_test?charset=utf8mb4"
uv run pytest
```

After changing models, create a migration:

```powershell
uv run alembic revision --autogenerate -m "describe change"
```

### Building the installer

```powershell
powershell -File packaging\build.ps1 -MariaDbBin "C:\Program Files\MariaDB 11.8\bin"
```

Produces `dist\CaspianWarehouse\` (PyInstaller, with the MariaDB client tools used for
backups) and, if Inno Setup 6 is installed, `dist\installer\CaspianWarehouse-Setup-<version>.exe`.
The frozen build is verified with `CaspianWarehouse.exe --smoke-test`. CI builds the installer
on every push to `main` and attaches it to the workflow run as an artifact.

### Read-only MCP server

External AI clients can query the inventory through the Model Context Protocol:

```json
{"mcpServers": {"caspian-warehouse": {"command": "caspian-mcp"}}}
```

It only exposes read tools (item search, stock, reorder analysis, documents, loans,
reports). Create the dedicated read-only database user from *Settings → AI* to also enable
ad-hoc `SELECT` queries; that user can't read the `users` table and every MCP connection
runs in a read-only transaction.

## Fonts

Bundles [Vazirmatn](https://github.com/rastikerdar/vazirmatn) by Saber Rastikerdar,
licensed under the SIL Open Font License 1.1 (see `src/caspian/resources/fonts/OFL.txt`).
