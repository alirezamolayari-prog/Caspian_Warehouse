"""Idempotent reference data every installation needs."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.core.permissions import DEFAULT_ROLES
from caspian.db.models import AppSetting, Role, RolePermission, Unit, Warehouse

DEFAULT_UNITS = ("عدد", "جعبه", "کارتن", "بسته", "متر", "متر مربع", "کیلوگرم", "لیتر", "دست", "رول")


async def seed_reference_data(session: AsyncSession) -> None:
    await _seed_roles(session)
    await _seed_units(session)
    await _seed_warehouse(session)
    await _seed_settings(session)
    await session.flush()


async def _seed_roles(session: AsyncSession) -> None:
    existing = {r.code: r for r in (await session.scalars(select(Role))).all()}
    for code, (name, perms) in DEFAULT_ROLES.items():
        role = existing.get(code)
        if role is None:
            role = Role(code=code, name=name, is_system=True)
            role.permissions = [RolePermission(permission=p.value) for p in sorted(perms)]
            session.add(role)
        elif code == "admin":
            # Admin always holds every permission, including ones added in later versions.
            have = {rp.permission for rp in role.permissions}
            role.permissions.extend(
                RolePermission(permission=p.value) for p in sorted(perms) if p.value not in have
            )


async def _seed_units(session: AsyncSession) -> None:
    have = set((await session.scalars(select(Unit.name))).all())
    session.add_all(Unit(name=n) for n in DEFAULT_UNITS if n not in have)


async def _seed_warehouse(session: AsyncSession) -> None:
    if await session.scalar(select(Warehouse.id).limit(1)) is None:
        session.add(Warehouse(code="01", name="انبار مرکزی"))


async def _seed_settings(session: AsyncSession) -> None:
    if await session.get(AppSetting, "company_name") is None:
        session.add(AppSetting(key="company_name", value="بازار مبلمان کاسپین"))
