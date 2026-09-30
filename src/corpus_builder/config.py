import math
import os
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import ConfigurationError


@dataclass(frozen=True)
class BuildConfig:
    api_key_env: str = "NCBI_API_KEY"
    require_api_key: bool = True
    requests_per_second: float = 10
    email: str = ""
    tool: str = "corpus-builder"
    formats: tuple[str, ...] = ("pdf", "xml")
    download_requests_per_second: float = 3
    timeout_seconds: float = 60
    max_attempts: int = 5
    backoff_seconds: float = 1
    resume: bool = True

    def validate(self) -> None:
        for name in ("api_key_env", "email", "tool"):
            if not isinstance(getattr(self, name), str):
                raise ConfigurationError(f"{name} must be a string")
        if not self.api_key_env or not self.tool:
            raise ConfigurationError("api_key_env and tool must not be empty")
        for name in ("require_api_key", "resume"):
            if type(getattr(self, name)) is not bool:
                raise ConfigurationError(f"{name} must be boolean")
        if not isinstance(self.formats, (tuple, list)) or not self.formats:
            raise ConfigurationError("formats must contain pdf and/or xml")
        if any(x not in ("pdf", "xml") for x in self.formats):
            raise ConfigurationError("Only pdf and xml formats are supported")
        if len(set(self.formats)) != len(self.formats):
            raise ConfigurationError("formats must not contain duplicates")
        for name in (
            "requests_per_second",
            "download_requests_per_second",
            "timeout_seconds",
            "backoff_seconds",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{name} must be a finite positive number")
        if self.requests_per_second > 10:
            raise ConfigurationError("NCBI requests_per_second must be <= 10")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 20:
            raise ConfigurationError("max_attempts must be an integer from 1 to 20")

    def resolve_key(self) -> str:
        self.validate()
        value = os.environ.get(self.api_key_env, "").strip()
        if self.require_api_key and not value:
            raise ConfigurationError(f"Environment variable {self.api_key_env} is missing or empty")
        if not value and self.requests_per_second > 3:
            raise ConfigurationError("Without an API key, requests_per_second must be <= 3")
        return value

    def public_dict(self) -> dict:
        result = asdict(self)
        result["formats"] = list(self.formats)
        return result

    @classmethod
    def from_toml(cls, path: str | Path) -> "BuildConfig":
        try:
            with Path(path).open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError("Cannot read configuration TOML") from exc
        mapping = {
            "ncbi": {
                k: k
                for k in ("api_key_env", "require_api_key", "requests_per_second", "email", "tool")
            },
            "download": {
                "formats": "formats",
                "requests_per_second": "download_requests_per_second",
            },
            "http": {k: k for k in ("timeout_seconds", "max_attempts", "backoff_seconds")},
            "run": {"resume": "resume"},
        }
        values = {}
        for section, entries in raw.items():
            if section not in mapping or not isinstance(entries, dict):
                raise ConfigurationError(f"Unknown or invalid section: {section}")
            for key, value in entries.items():
                if key not in mapping[section]:
                    raise ConfigurationError(f"Unknown setting: {section}.{key}")
                values[mapping[section][key]] = value
        config = cls(**values)
        config.validate()
        return config
