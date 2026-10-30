"""What the channel modules share with the notifier."""

from __future__ import annotations


class RateLimited(RuntimeError):
    """The service asked us to wait: try again after this many seconds, not sooner."""

    def __init__(self, message: str, after: float):
        super().__init__(message)
        self.after = after


def retry_after(headers, default: float = 60) -> float:
    try:
        return max(1.0, float(headers.get("retry-after", default)))
    except ValueError:  # the HTTP-date form: rare, and waiting the default is safe
        return default
