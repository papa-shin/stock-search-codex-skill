from __future__ import annotations


class StockError(Exception):
    """A user-facing error that never contains configuration values."""

    def __init__(self, code: str, safe_message: str, exit_code: int) -> None:
        self.code = code
        self.safe_message = safe_message
        self.exit_code = exit_code
        super().__init__(f"{code}: {safe_message}")
