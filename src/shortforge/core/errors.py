"""Error taxonomy. The worker decides retry vs. fail purely from the exception type."""


class ShortForgeError(Exception):
    """Base class."""


class RetryableError(ShortForgeError):
    """Transient: network blips, rate limits, timeouts. The message is redelivered with backoff."""


class FatalError(ShortForgeError):
    """Permanent: bad input, invariant violation. Retrying cannot help; the job fails."""


class ProviderError(RetryableError):
    """A single provider failed. Provider chains catch this and fall through to the next one."""


class AllProvidersFailed(RetryableError):
    """Every provider in a chain failed. Retryable at stage level (e.g. a rate-limit window passes)."""

    def __init__(self, kind: str, errors: dict[str, str]):
        self.kind = kind
        self.errors = errors
        detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
        super().__init__(f"all {kind} providers failed -> {detail}")


class ValidationFailed(ShortForgeError):
    """Output produced but rejected by a guardrail. Chains treat this like a provider failure."""


class LeaseBusy(ShortForgeError):
    """Another worker currently holds the stage lease. The delivery should be retried later."""
