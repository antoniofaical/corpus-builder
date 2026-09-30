"""Public API for PubMed discovery and PMC Open Access corpus building."""

from .api import build_corpus
from .config import BuildConfig
from .errors import ConfigurationError, CorpusBuilderError
from .models import BuildResult

__all__ = ["BuildConfig", "BuildResult", "ConfigurationError", "CorpusBuilderError", "build_corpus"]
__version__ = "0.1.0"
