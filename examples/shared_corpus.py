"""One query per call; a future orchestrator can call this entrypoint repeatedly."""

from corpus_builder import BuildConfig, QueryContext, build_corpus

if __name__ == "__main__":
    result = build_corpus(
        query='("biosensors"[MeSH Terms]) AND microneedle*',
        output_dir="runs/Q01-v1",
        corpus_dir="corpus",
        context=QueryContext("Q01", "1", "T1", "hardware"),
        config=BuildConfig.from_toml("configs.toml"),
    )
    print(result.to_dict())
