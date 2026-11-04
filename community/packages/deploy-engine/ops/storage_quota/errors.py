"""Stable errors suitable for the unprivileged JSON protocol."""


class QuotaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
