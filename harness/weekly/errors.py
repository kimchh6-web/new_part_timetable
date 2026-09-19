"""The one exception type the weekly seam raises.

Every refusal — a malformed request, a duplicated weekday, too many pins, or an
empty candidate set — is the same Python type carrying the contract's error
code, the HTTP status that code maps to, and a ``details`` payload. The HTTP
layer (owned elsewhere) can therefore be three lines long and needs no
knowledge of the rules that produced the refusal.
"""

from __future__ import annotations

from typing import Any

#: Contract error code -> HTTP status. ``TIMEOUT`` belongs to the transport
#: layer and is listed only so the mapping is complete in one place.
ERROR_STATUS: dict[str, int] = {
    "VALIDATION_ERROR": 400,
    "SCHEDULE_CONFLICT": 400,
    "PINNED_EXCEEDS_COUNT": 400,
    "NO_CANDIDATES": 422,
    "TIMEOUT": 504,
}


class WeeklyValidationError(Exception):
    """A refusal the caller can render verbatim.

    ``str(error)`` is the human message; :attr:`code`, :attr:`status` and
    :attr:`details` are what the HTTP layer serialises.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status) if status is not None else ERROR_STATUS.get(code, 400)
        self.details: dict[str, Any] = dict(details or {})

    def to_response(self) -> dict[str, Any]:
        """The contract's error envelope, ready to ``json.dumps``."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"WeeklyValidationError(code={self.code!r}, status={self.status!r})"


__all__ = ["ERROR_STATUS", "WeeklyValidationError"]
