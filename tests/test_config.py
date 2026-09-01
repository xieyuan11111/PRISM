import json
from pathlib import Path

import pytest

from prism.config import (
    FirecrawlConfig,
    LLMConfig,
    LLMProviderConfig,
    PathConfig,
    PrismConfig,
    SourceConfig,
)


def make_config() -> PrismConfig:
    return PrismConfig(
        paths=PathConfig(
            data_dir=Path("data"),
            cache_dir=Path("var/cache"),
            output_dir=Path("output"),
        ),
        llm=LLMConfig(
            providers={
                "primary": LLMProviderConfig(
                    model="provider/model-name",
                    api_key_env="PRIMARY_API_KEY",
                ),
                "review": LLMProviderConfig(
                    model="provider/review-model",
                    api_key_env="REVIEW_API_KEY",
                    base_url="https://llm.example.test/v1",
                ),
            },
            task_roles={"extract": "primary", "review": "review"},
        ),
        sources=SourceConfig(whitelist=["example.com", "news.example.org"]),
    )


def test_paths_resolve_relative_to_prism_home(monkeypatch, tmp_path):
    prism_home = tmp_path / "prism-home"
    monkeypatch.setenv("PRISM_HOME", str(prism_home))

    resolved = make_config().resolved_paths()

    assert resolved.data_dir == prism_home / "data"
    assert resolved.cache_dir == prism_home / "var/cache"
    assert resolved.output_dir == prism_home / "output"


def test_absolute_paths_are_not_rebased(tmp_path):
    absolute = (tmp_path / "absolute-data").resolve()
    paths = PathConfig(
        data_dir=absolute,
        cache_dir=Path("cache"),
        output_dir=Path("output"),
    )

    assert paths.resolve(tmp_path / "home").data_dir == absolute


def test_missing_prism_home_has_a_stable_default(monkeypatch):
    monkeypatch.delenv("PRISM_HOME", raising=False)

    assert PathConfig.prism_home() == Path.home() / ".prism"


def test_task_roles_must_reference_declared_providers():
    with pytest.raises(ValueError, match="missing"):
        LLMConfig(
            providers={"primary": LLMProviderConfig(model="provider/model")},
            task_roles={"extract": "missing"},
        )


@pytest.mark.parametrize("domain", ["", "https://example.com", "example.com/path"])
def test_source_whitelist_requires_bare_domains(domain):
    with pytest.raises(ValueError, match="domain"):
        SourceConfig(whitelist=[domain])


def test_source_whitelist_is_normalized_and_deduplicated():
    sources = SourceConfig(whitelist=["Example.COM", "example.com", "news.test"])

    assert sources.whitelist == ("example.com", "news.test")
    assert sources.allows("EXAMPLE.com")
    assert not sources.allows("other.test")


def test_config_round_trip_preserves_relative_paths_and_values(tmp_path):
    config = make_config()
    config_file = tmp_path / "prism.json"

    config.save(config_file)
    loaded = PrismConfig.load(config_file)

    assert loaded == config
    assert json.loads(config_file.read_text(encoding="utf-8"))["paths"]["data_dir"] == "data"


def test_config_round_trip_preserves_firecrawl_research_settings(tmp_path):
    config = PrismConfig(
        sources=SourceConfig(whitelist=["example.gov"]),
        firecrawl=FirecrawlConfig(
            enabled=True,
            api_key_env="LOCAL_FIRECRAWL_KEY",
            base_url="https://firecrawl.example.test",
            limit=7,
            timeout=4.5,
        ),
    )
    path = tmp_path / "config.json"

    config.save(path)

    assert PrismConfig.load(path) == config
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["firecrawl"]["enabled"] is True
    assert raw["firecrawl"]["api_key_env"] == "LOCAL_FIRECRAWL_KEY"


def test_firecrawl_config_is_disabled_by_default():
    config = PrismConfig()
    assert config.firecrawl.enabled is False
def test_config_rejects_unknown_keys():
    data = make_config().to_dict()
    data["unexpected"] = True

    with pytest.raises(ValueError, match="unexpected"):
        PrismConfig.from_dict(data)


def test_config_paths_use_portable_posix_serialization(tmp_path):
    config = make_config()
    config_file = tmp_path / "prism.json"

    config.save(config_file)

    data = json.loads(config_file.read_text(encoding="utf-8"))
    assert data["paths"]["cache_dir"] == "var/cache"


def test_empty_path_is_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        PathConfig(data_dir="")


def test_relative_prism_home_is_resolved_from_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRISM_HOME", "workspace/prism")

    resolved = PathConfig().resolve()

    assert resolved.data_dir == (tmp_path / "workspace/prism/data").resolve()
