import json

import pytest

from corpus_builder import BuildConfig, ConfigurationError
from corpus_builder.cli import main


def test_toml_mapping_and_custom_env(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("""[ncbi]
api_key_env = "MY_KEY"
requests_per_second = 10
[download]
formats = ["xml"]
requests_per_second = 2
""")
    monkeypatch.setenv("MY_KEY", "fixture_secret")
    config = BuildConfig.from_toml(path)
    assert config.resolve_key() == "fixture_secret"
    assert config.formats == ["xml"]
    assert config.download_requests_per_second == 2
    assert "fixture_secret" not in json.dumps(config.public_dict())


@pytest.mark.parametrize(
    "body",
    [
        "[unknown]\nx=1",
        "[ncbi]\napi_key='secret'",
        "[ncbi]\nrequests_per_second=11",
        "[ncbi]\nrequests_per_second=nan",
        "[http]\nmax_attempts=0",
        "[download]\nformats=[]",
        "[run]\nresume='yes'",
        "[download]\nformats=['html']",
    ],
)
def test_invalid_config_is_rejected(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body)
    with pytest.raises(ConfigurationError):
        BuildConfig.from_toml(path)


def test_cli_uses_same_core_and_emits_json(fake, tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text("[ncbi]\nrequire_api_key=false\nrequests_per_second=3\n")
    query_file = tmp_path / "query.txt"
    query_file.write_text('\ufeff"fixture query"[Title/Abstract]', encoding="utf-8")
    code = main(
        [
            "--config",
            str(config),
            "--query-file",
            str(query_file),
            "--output-dir",
            str(tmp_path / "run"),
            "--quiet",
        ]
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["counts"]["files_verified"] == 2
