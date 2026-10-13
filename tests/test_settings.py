"""Environment parsing.

These exist because two of these cases got as far as a Docker container before
anyone noticed: pydantic-settings JSON-decodes complex fields before validators
run, and compose's ``${VAR:-}`` sends an empty string rather than nothing.
"""

import pytest

from waypoint.settings import Settings


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # _env_file="" keeps a developer's local .env out of the test.
    return Settings(_env_file="")  # type: ignore[call-arg]


def test_cors_origins_accepts_a_bare_url(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, CORS_ORIGINS="http://localhost:8080")
    assert settings.cors_origins == ["http://localhost:8080"]


def test_cors_origins_accepts_a_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, CORS_ORIGINS="http://localhost:5173, https://app.example.com")
    assert settings.cors_origins == ["http://localhost:5173", "https://app.example.com"]


def test_cors_origins_is_comma_separated_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """NoDecode trades JSON syntax for the plain form people actually type.

    Documented in docs/CONFIGURATION.md: this field is a comma-separated list.
    """
    settings = _settings(monkeypatch, CORS_ORIGINS='["http://a"]')
    assert settings.cors_origins != ["http://a"]


def test_blank_cors_origins_disables_cross_origin_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings(monkeypatch, CORS_ORIGINS="  ,  ").cors_origins == []


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "BOOTSTRAP_API_KEY", "EMBEDDINGS_MODEL", "DATABASE_URL", "REDIS_URL"],
)
def test_empty_env_values_read_as_unset(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """`FOO: ${FOO:-}` in compose must not become the string ''."""
    settings = _settings(monkeypatch, **{name: ""})
    assert getattr(settings, name.lower()) is None


def test_nested_limits_use_double_underscore(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, LIMITS__RPM="120", STREAM__MAXLEN="50")
    assert settings.limits.rpm == 120
    assert settings.stream.maxlen == 50


def test_the_full_compose_environment_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly what compose.yaml sets, with every optional value left blank."""
    settings = _settings(
        monkeypatch,
        ENV="production",
        JSON_LOGS="true",
        DATABASE_URL="postgresql://agents:agents@postgres:5432/agents",
        REDIS_URL="redis://redis:6379/0",
        SECRET_KEY="change-me",
        ANTHROPIC_API_KEY="",
        BOOTSTRAP_API_KEY="",
        MODEL="claude-sonnet-4-5",
        FAKE_MODEL="false",
        CORS_ORIGINS="http://localhost:8080",
        LIMITS__RPM="60",
        LIMITS__CONCURRENT_RUNS="4",
        BUDGET__ENABLED="false",
        BUDGET__TENANT_USD_PER_DAY="",
        BUDGET__GLOBAL_USD_PER_DAY="",
        BUDGET__TENANTS="",
        EMBEDDINGS_MODEL="",
    )
    assert settings.is_production
    assert settings.pg_dsn is not None
    assert settings.redis_dsn is not None
    assert settings.cors_origins == ["http://localhost:8080"]
    assert settings.anthropic_api_key is None


def test_the_shipped_placeholder_is_recognised(monkeypatch: pytest.MonkeyPatch) -> None:
    """It must never be seeded: it is printed in .env.example."""
    settings = _settings(monkeypatch, BOOTSTRAP_API_KEY="ak_dev000000.change-this-local-key")
    assert settings.bootstrap_is_placeholder is True


def test_a_real_key_is_not_a_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, BOOTSTRAP_API_KEY="ak_abc123abc123.a-real-secret")
    assert settings.bootstrap_is_placeholder is False


def test_temperature_is_omitted_unless_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newer Anthropic models 400 on `temperature`, so it must not be a default."""
    from waypoint.agents.models import anthropic_options

    settings = _settings(monkeypatch)
    assert settings.model_temperature is None
    assert "temperature" not in anthropic_options(settings)


def test_temperature_from_the_environment_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    from waypoint.agents.models import anthropic_options

    settings = _settings(monkeypatch, MODEL_TEMPERATURE="0.7")
    assert anthropic_options(settings)["temperature"] == 0.7


def test_a_call_site_can_ask_for_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    from waypoint.agents.models import anthropic_options

    options = anthropic_options(_settings(monkeypatch), temperature=1.0)
    assert options["temperature"] == 1.0


def test_a_call_site_overrides_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from waypoint.agents.models import anthropic_options

    settings = _settings(monkeypatch, MODEL_TEMPERATURE="0.2")
    assert anthropic_options(settings, temperature=0.9)["temperature"] == 0.9


def test_the_model_can_be_overridden_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from waypoint.agents.models import anthropic_options

    options = anthropic_options(_settings(monkeypatch, MODEL="claude-x"), model="claude-y")
    assert options["model"] == "claude-y"


def test_out_of_range_temperature_fails_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Exception, match="less than or equal to 1"):
        _settings(monkeypatch, MODEL_TEMPERATURE="2.0")


def test_compose_fallbacks_match_the_settings_defaults() -> None:
    """``${KEY:-x}`` in a compose file must not quietly override a code default.

    compose.yaml kept ``RESEARCH__MAX_ROUNDS:-2`` after the default became 1
    (cost case study), so Bob ran two rounds with no such line in its .env.
    Fields whose default is None or built by a factory are derived, and skipped.
    """
    import re
    from pathlib import Path

    from pydantic import BaseModel
    from pydantic_core import PydanticUndefined

    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"^\s+([A-Z][A-Z0-9_]*__[A-Z0-9_]+): \$\{\1:-([^}]*)\}\s*$", re.M)
    checked, wrong = 0, []
    for name in ("compose.yaml", "compose.prod.yaml", "compose.dev.yaml"):
        path = root / name
        if not path.exists():
            continue
        for key, fallback in pattern.findall(path.read_text(encoding="utf-8")):
            section, field = key.lower().split("__", 1)
            model = Settings.model_fields[section].annotation
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                continue
            default = model.model_fields[field].default
            if default is None or default is PydanticUndefined:
                continue
            checked += 1
            if isinstance(default, bool):
                same = fallback == str(default).lower()
            elif isinstance(default, int | float):
                same = fallback != "" and float(fallback) == float(default)
            else:
                same = fallback == str(default)
            if not same:
                wrong.append(f"{name}: {key}:-{fallback} but the default is {default!r}")
    assert checked >= 10
    assert not wrong, wrong
