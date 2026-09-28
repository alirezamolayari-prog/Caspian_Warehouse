from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

# Single source of truth: the version in pyproject.toml (installed package metadata;
# the PyInstaller build bundles the same metadata).
try:
    __version__ = _package_version("caspian-warehouse")
except PackageNotFoundError:  # running from a bare source tree
    __version__ = "0.0.0"

APP_NAME = "CaspianWarehouse"
APP_DISPLAY_NAME = "انبار کاسپین"
