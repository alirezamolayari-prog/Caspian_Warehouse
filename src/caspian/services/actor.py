"""Who is performing an operation. Every service call receives an Actor."""

from dataclasses import dataclass

from caspian.core.permissions import Perm
from caspian.db.models import User
from caspian.services.errors import PermissionDenied


@dataclass(frozen=True)
class Actor:
    user_id: int | None
    username: str
    full_name: str
    role_code: str
    permissions: frozenset[str]
    # Set when the AI assistant acts on a user's behalf. AI actors can only read and
    # prepare drafts; protected actions refuse them outright.
    is_ai: bool = False

    @classmethod
    def from_user(cls, user: User) -> "Actor":
        return cls(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            role_code=user.role.code,
            permissions=frozenset(p.permission for p in user.role.permissions),
        )

    @property
    def display_name(self) -> str:
        return self.full_name or self.username

    @property
    def is_admin(self) -> bool:
        return self.role_code == "admin"

    def can(self, perm: Perm) -> bool:
        return perm.value in self.permissions

    def require(self, perm: Perm) -> None:
        if not self.can(perm):
            raise PermissionDenied("شما مجوز انجام این کار را ندارید.")

    def require_human(self, what: str) -> None:
        """Destructive or protected actions are never done by the AI assistant (on any channel)."""
        if self.is_ai:
            raise PermissionDenied(f"دستیار هوشمند اجازه {what} را ندارد؛ این کار را خودتان از برنامه "
                                   "انجام دهید.")

    def as_ai(self) -> "Actor":
        """The same user's identity, restricted for use by the AI assistant."""
        return Actor(self.user_id, self.username, self.full_name, self.role_code,
                     self.permissions, is_ai=True)


SYSTEM = Actor(None, "system", "سیستم", "system", frozenset())
