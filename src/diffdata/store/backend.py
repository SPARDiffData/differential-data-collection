"""The one storage interface (STO-11): everything that sends files goes through a `Store`, so
the pilot's Google Cloud Storage bucket can be swapped for another service without touching
the other stages.

A store can only add. It has no way to overwrite, replace or delete, so no caller can do
any of those by mistake (STO-3).
"""

from typing import Literal, Protocol

Added = Literal["added", "exists"]


class StoreError(Exception):
    """Sending to the store failed. The message names no person and quotes no data."""


class SignInError(StoreError):
    """The store couldn't sign in. The message says how to fix it, and never holds a token."""


class Store(Protocol):
    def add(self, name: str, data: bytes, content_type: str) -> Added:
        """Add `data` as `name`, only if nothing has that name yet. Returns "added", or
        "exists" if the name was already taken: the stored file is left as it was."""
        ...


class FakeStore:
    """A store in memory, for tests. It behaves like the bucket's create-only upload."""

    def __init__(self, objects: dict[str, bytes] | None = None, fail_after: int | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.sent: list[str] = []  # every name it was asked to add, in order
        self.fail_after = fail_after  # stop with a StoreError after this many adds

    def add(self, name: str, data: bytes, content_type: str) -> Added:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise StoreError("The store stopped answering.")
        self.sent.append(name)
        if name in self.objects:
            return "exists"
        self.objects[name] = data
        return "added"
