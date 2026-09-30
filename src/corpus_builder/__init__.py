"""Public API for PubMed discovery and PMC Open Access corpus building."""

from .api import build_corpus
from .config import BuildConfig
from .errors import ConfigurationError, CorpusBuilderError
from .identity import QueryContext
from .migration import import_run
from .models import BuildResult

__all__ = [
    "QueryContext",
    "import_run",
    "BuildConfig",
    "BuildResult",
    "ConfigurationError",
    "CorpusBuilderError",
    "build_corpus",
]
__version__ = "0.2.0"
