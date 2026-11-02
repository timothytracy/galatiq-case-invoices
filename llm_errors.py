"""The model-call error. Shared by llm_client and tool_loop."""


class LLMError(Exception):
    """One failed model call. log is copied onto the attempt record."""

    def __init__(
        self,
        error_type: str,
        retryable: bool,
        message: str = "",
        *,
        log: dict | None = None,
    ) -> None:
        super().__init__(message or error_type)
        self.error_type = error_type
        self.retryable = retryable
        self.log = {} if log is None else log
