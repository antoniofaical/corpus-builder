import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

from .api import build_corpus
from .config import BuildConfig
from .errors import ConfigurationError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PubMed search to a verified PMC OA corpus")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query", help="One boolean PubMed query")
    source.add_argument("--query-file", type=Path, help="UTF-8 file containing ONE query")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("configs.toml"))
    parser.add_argument("--no-resume", action="store_true")
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
        query = (
            args.query
            if args.query is not None
            else args.query_file.read_text(encoding="utf-8-sig")
        )
        result = build_corpus(query, args.output_dir, config)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Filesystem error; check input/output paths and available storage", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; completed work is preserved for resume", file=sys.stderr)
        return 130
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.status == "completed" else 1
