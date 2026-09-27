"""Which fiscal years are closed, and where their archives are. (No heavy imports: the
documents service consults this on every change.)"""

from dataclasses import asdict, dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from caspian.db.database import Database
from caspian.db.models import AppSetting
from caspian.services.errors import ValidationError


@dataclass(frozen=True)
class ArchiveInfo:
    year: int
    database: str
    closed_at: str
    closed_by: str
    backup: str


@dataclass
class FiscalState:
    closed_through: int | None = None
    archives: list[dict] = field(default_factory=list)

    def archive_list(self) -> list[ArchiveInfo]:
        return [ArchiveInfo(**a) for a in sorted(self.archives, key=lambda a: a["year"], reverse=True)]


async def read_state(s: AsyncSession) -> FiscalState:
    row = await s.get(AppSetting, "fiscal")
    data = row.value if row and isinstance(row.value, dict) else {}
    return FiscalState(data.get("closed_through"), list(data.get("archives", [])))


async def write_state(s: AsyncSession, state: FiscalState) -> None:
    row = await s.get(AppSetting, "fiscal")
    if row is None:
        s.add(AppSetting(key="fiscal", value=asdict(state)))
    else:
        row.value = asdict(state)


async def load_state(db: Database) -> FiscalState:
    async with db.session() as s:
        return await read_state(s)


async def ensure_open_year(s: AsyncSession, fiscal_year: int) -> None:
    closed = (await read_state(s)).closed_through
    if closed is not None and fiscal_year <= closed:
        raise ValidationError(f"سال مالی {fiscal_year} بسته شده است؛ اسناد آن فقط در بایگانی قابل "
                              "مشاهده‌اند.")
