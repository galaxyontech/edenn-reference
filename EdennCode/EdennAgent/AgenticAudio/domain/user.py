"""The User entity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class User:
    """The session's creator.

    MVP: wraps the (currently optional, un-FK'd) ``creator_user_id`` so the rest
    of the system has a first-class type to hang ownership / quotas / auth on
    later, without yet introducing a users table.
    """

    id: Optional[str] = None

    @classmethod
    def from_session(cls, session: Any) -> "User":
        return cls(id=getattr(session, "creator_user_id", None))

    @property
    def is_anonymous(self) -> bool:
        return not self.id


__all__ = ["User"]
