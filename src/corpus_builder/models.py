from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BuildResult:
    run_id: str
    status: str
    discovery_complete: bool
    counts: dict[str, int]
    output_dir: Path
    report_path: Path
    manifest_path: Path
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "discovery_complete": self.discovery_complete,
            "counts": self.counts,
            "output_dir": str(self.output_dir),
            "report_path": str(self.report_path),
            "manifest_path": str(self.manifest_path),
            "errors": list(self.errors),
        }
