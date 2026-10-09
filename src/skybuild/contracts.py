"""Shared bootstrap contracts; importing this module has no side effects."""

from dataclasses import dataclass
import unicodedata


def valid_identifier(value: object, maximum: int = 200) -> bool:
    """Keep opaque IDs addressable as one URL path segment."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and value not in {".", ".."}
        and not any(char in "/\\%" or unicodedata.category(char) == "Cc" for char in value)
    )


@dataclass(frozen=True)
class Principal:
    principal_id: str
    is_admin: bool
    grants: dict[str, frozenset[str]]


class DomainError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
