"""Permission codes and the default roles that bundle them."""

import enum


class Perm(enum.StrEnum):
    ITEMS_VIEW = "items.view"
    ITEMS_EDIT = "items.edit"
    ITEMS_DEACTIVATE = "items.deactivate"  # protected
    PERSONS_EDIT = "persons.edit"
    WAREHOUSES_EDIT = "warehouses.edit"
    DOCUMENTS_VIEW = "documents.view"
    DOCUMENTS_EDIT = "documents.edit"
    DOCUMENTS_POST = "documents.post"
    IMPORT_RUN = "import.run"
    IMPORT_OVERWRITE = "import.overwrite"  # protected
    ITEMS_MERGE = "items.merge"  # protected
    STOCKTAKE_RUN = "stocktake.run"
    STOCKTAKE_APPROVE = "stocktake.approve"
    REPORTS_VIEW = "reports.view"
    AI_USE = "ai.use"
    AI_CONFIGURE = "ai.configure"
    USERS_MANAGE = "users.manage"
    ROLES_CHANGE = "roles.change"  # protected
    BACKUP_CREATE = "backup.create"
    BACKUP_RESTORE = "backup.restore"  # protected
    YEAR_CLOSE = "year.close"  # protected
    SETTINGS_EDIT = "settings.edit"


# code -> (display name, permissions)
DEFAULT_ROLES: dict[str, tuple[str, frozenset[Perm]]] = {
    "admin": ("مدیر سیستم", frozenset(Perm)),
    "manager": (
        "مدیر انبار",
        frozenset(Perm)
        - {Perm.USERS_MANAGE, Perm.ROLES_CHANGE, Perm.BACKUP_RESTORE, Perm.YEAR_CLOSE,
           Perm.AI_CONFIGURE, Perm.SETTINGS_EDIT},
    ),
    "storekeeper": (
        "انباردار",
        frozenset({
            Perm.ITEMS_VIEW, Perm.ITEMS_EDIT, Perm.DOCUMENTS_VIEW, Perm.DOCUMENTS_EDIT,
            Perm.DOCUMENTS_POST, Perm.IMPORT_RUN, Perm.STOCKTAKE_RUN, Perm.REPORTS_VIEW,
            Perm.AI_USE,
        }),
    ),
    "viewer": (
        "مشاهده‌گر",
        frozenset({Perm.ITEMS_VIEW, Perm.DOCUMENTS_VIEW, Perm.REPORTS_VIEW}),
    ),
}
