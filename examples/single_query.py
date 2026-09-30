"""Example caller. Run from the repository root with the package installed."""

from corpus_builder import BuildConfig, build_corpus

if __name__ == "__main__":
    result = build_corpus(
        query='("biosensors"[MeSH Terms]) AND microneedle*',
        output_dir="runs/biosensors",
        config=BuildConfig.from_toml("configs.toml"),
        on_event=lambda event: print(event["stage"]),
    )
    print(result.to_dict())
