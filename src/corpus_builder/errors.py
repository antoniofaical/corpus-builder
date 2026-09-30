"""Public, sanitized errors: never include credentials or HTTP response bodies."""


class CorpusBuilderError(Exception):
    """Base operational error."""


class ConfigurationError(CorpusBuilderError):
    """Invalid configuration, credentials, or incompatible existing run."""


class AuthenticationError(ConfigurationError):
    """Remote service rejected authentication."""


class RemoteError(CorpusBuilderError):
    """Permanent or exhausted remote-service failure."""


class RetryableError(RemoteError):
    """Temporary service failure or invalid response."""


class DiscoveryError(CorpusBuilderError):
    """Complete enumeration could not be verified."""
