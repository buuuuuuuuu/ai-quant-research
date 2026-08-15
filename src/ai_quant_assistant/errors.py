class QuantResearchError(Exception):
    """Base exception for expected research failures."""


class ProviderError(QuantResearchError):
    """A market-data provider could not return usable data."""


class DataValidationError(QuantResearchError):
    """Input data does not satisfy the normalized schema."""


class InsufficientDataError(QuantResearchError):
    """There is not enough aligned data to compute the requested study."""

