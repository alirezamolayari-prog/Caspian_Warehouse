# Third-party notices

Caspian Warehouse is proprietary software (see [LICENSE](LICENSE)). It bundles or uses the
following third-party components, each under its own license. Full license texts are in
[`packaging/licenses/`](packaging/licenses) and are installed to `licenses\` in the
application folder.

| Component | Use | License |
|---|---|---|
| **MariaDB Server** 11.8 (optional component of the installer) and the MariaDB client tools `mariadb-dump`, `mariadb` | database server, backups | GPL-2.0 — `MariaDB-COPYING-GPL-2.0.txt`, notices in `MariaDB-THIRDPARTY.txt` |
| **Qt 6 / PySide6 / Shiboken6** | user interface | LGPL-3.0 — `LGPL-3.0.txt`, `GPL-3.0.txt` |
| **Python** (bundled runtime) | runtime | PSF License |
| **Vazirmatn** font, © Saber Rastikerdar | Persian font | SIL Open Font License 1.1 — `src/caspian/resources/fonts/OFL.txt` |
| **Lucide** icons | icons | ISC — `src/caspian/resources/icons/LICENSE.txt` |
| SQLAlchemy, Alembic, aiomysql, PyMySQL, aiosqlite, qasync, jdatetime, platformdirs, keyring, openpyxl, python-docx, lxml, rapidfuzz, httpx, argon2-cffi, cryptography, mcp (Model Context Protocol SDK) and their dependencies | libraries | MIT / BSD / Apache-2.0 (see each package's metadata) |

## Source code of GPL/LGPL components

- MariaDB source code: https://mariadb.org/download/?t=source (the bundled version is the
  11.8 LTS release named in the installer file list). On request we will provide the exact
  source code of the MariaDB version we distribute for at least three years.
- Qt source code: https://download.qt.io/official_releases/qt/ ; PySide6:
  https://code.qt.io/cgit/pyside/pyside-setup.git/
- Qt/PySide6 libraries are shipped as separate, unmodified DLLs in the application folder, so
  they can be replaced with compatible versions.
