# PyInstaller spec: one folder with two programs sharing the same libraries.
#   CaspianWarehouse.exe  - the desktop app (no console)
#   caspian-mcp.exe       - read-only MCP server for external AI clients (stdio)
# Build:  uv run pyinstaller packaging/caspian.spec --noconfirm

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

datas = [
    (str(SRC / "caspian" / "resources"), "caspian/resources"),
    # Alembic loads migration scripts from disk, so they ship as files.
    (str(SRC / "caspian" / "db" / "migrations"), "caspian/db/migrations"),
]
hiddenimports = [
    *collect_submodules("keyring.backends"),
    "sqlalchemy.dialects.mysql.aiomysql",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "aiomysql",
    "aiosqlite",
    *collect_submodules("caspian"),
    *collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli")),
    "alembic.runtime.migration",
    "alembic.operations",
    "alembic.ddl.mysql",
    "alembic.ddl.sqlite",
]
excludes = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick"]


def analysis(script):
    return Analysis([str(script)], pathex=[str(SRC)], datas=datas, hiddenimports=hiddenimports,
                    excludes=excludes, noarchive=False)


app = analysis(ROOT / "packaging" / "run_app.py")
mcp = analysis(ROOT / "packaging" / "run_mcp.py")

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="CaspianWarehouse",
              console=False, icon=str(ROOT / "packaging" / "caspian.ico"),
              version=str(ROOT / "packaging" / "version_info.txt"))
mcp_exe = EXE(PYZ(mcp.pure), mcp.scripts, [], exclude_binaries=True, name="caspian-mcp",
              console=True, icon=str(ROOT / "packaging" / "caspian.ico"))

COLLECT(app_exe, app.binaries, app.datas, mcp_exe, mcp.binaries, mcp.datas,
        name="CaspianWarehouse", strip=False, upx=False)
