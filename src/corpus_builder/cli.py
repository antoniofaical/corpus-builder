import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

from .api import build_corpus
from .config import BuildConfig
from .errors import ConfigurationError
from .identity import QueryContext
from .migration import import_run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="PubMed discovery, query provenance and verified open-access texts"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--import-run", type=Path, help="Import a v1/v2 run without network calls")
    source.add_argument("--query", help="One boolean PubMed query")
    source.add_argument("--query-file", type=Path, help="UTF-8 file containing ONE query")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=Path("configs.toml"))
    parser.add_argument("--corpus-dir", type=Path, help="Shared catalog for multiple runs")
    parser.add_argument("--query-id")
    parser.add_argument("--query-version")
    parser.add_argument("--track")
    parser.add_argument("--technical-stratum")
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
        context = (
            QueryContext(args.query_id, args.query_version, args.track, args.technical_stratum)
            if any((args.query_id, args.query_version, args.track, args.technical_stratum))
            else None
        )
        if args.import_run:
            if not args.corpus_dir:
                raise ConfigurationError("--import-run requires --corpus-dir")
            if args.output_dir:
                raise ConfigurationError("--output-dir does not apply to --import-run")
            print(
                json.dumps(
                    import_run(args.import_run, args.corpus_dir, context=context),
                    ensure_ascii=False,
                )
            )
            return 0
        if not args.output_dir:
            raise ConfigurationError("Search requires --output-dir")
        config = BuildConfig.from_toml(args.config)
        if args.no_resume:
            config = replace(config, resume=False)
        query = (
            args.query
            if args.query is not None
            else args.query_file.read_text(encoding="utf-8-sig")
        )
        result = build_corpus(
            query, args.output_dir, config, context=context, corpus_dir=args.corpus_dir
        )
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
