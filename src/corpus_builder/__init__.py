"""Public API for PubMed discovery and PMC Open Access corpus building."""

from .api import build_corpus
from .batch import BatchResult, QuerySpec, load_queries, run_batch
from .config import BuildConfig
from .errors import ConfigurationError, CorpusBuilderError
from .identity import QueryContext
from .migration import import_run
from .models import BuildResult

__all__ = [
    "BatchResult",
    "QuerySpec",
    "load_queries",
    "run_batch",
    "QueryContext",
    "import_run",
    "BuildConfig",
    "BuildResult",
    "ConfigurationError",
    "CorpusBuilderError",
    "build_corpus",
]
__version__ = "0.3.0"
