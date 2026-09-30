"""CLI for sequential batches: python -m corpus_builder.orchestrator."""

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

from .batch import load_queries, run_batch
from .config import BuildConfig
from .errors import ConfigurationError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a JSON list of PubMed queries sequentially")
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--corpus-dir", type=Path)
    parser.add_argument("--config", type=Path, default=Path("configs.toml"))
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--recheck-completed", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config = BuildConfig.from_toml(args.config)
        if args.no_resume:
            config = replace(config, resume=False)
        result = run_batch(
            load_queries(args.queries),
            args.output_dir,
            config,
            corpus_dir=args.corpus_dir,
            recheck_completed=args.recheck_completed,
        )
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Filesystem error; inspect paths, storage and the batch checkpoint", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; resume the same command to continue the batch", file=sys.stderr)
        return 130
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
