class ApiError(Exception):
    """Rendered by the app as `{error: {code, message, retryable}}` with `status`."""

    def __init__(self, status: int, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable
