import pytest

from app.core.config import env_overrides, parse_repo_config, resolve
from app.core.errors import ConfigError
from app.core.models import GateLevel


def test_defaults_load_from_bundled_yaml():
    config = resolve()
    assert config.scm == "azure_devops"
    assert config.reviewer == "llm"
    assert config.severity_gate is GateLevel.ERROR
    # Empty is the "use every standard in app/defaults/standards" default, so
    # dropping a new markdown file in there needs no config change.
    assert config.analyzers == []


def test_precedence_env_then_repo_then_request():
    environ = {
        "PRREVIEW_MODEL_ENDPOINT": "from-env",
        "PRREVIEW_MAX_FILES": "5",
        "PRREVIEW_ANALYZERS": "python, sql",
        "UNRELATED": "ignored",
    }
    config = resolve(environ=environ)
    assert config.model_endpoint == "from-env"
    assert config.max_files == 5
    assert config.analyzers == ["python", "sql"]

    config = resolve(environ=environ, repo_config={"model_endpoint": "from-repo"})
    assert config.model_endpoint == "from-repo"
    assert config.max_files == 5  # env still applies where the repo is silent

    config = resolve(
        environ=environ,
        repo_config={"model_endpoint": "from-repo"},
        request_overrides={"model_endpoint": "from-request"},
    )
    assert config.model_endpoint == "from-request"


def test_env_overrides_ignores_unknown_fields():
    assert env_overrides({"PRREVIEW_NOT_A_FIELD": "x"}) == {}


def test_unknown_repo_key_is_a_clear_error():
    with pytest.raises(ConfigError) as exc:
        resolve(repo_config={"reviewr": "llm"})
    assert "reviewr" in str(exc.value)


def test_malformed_repo_yaml_is_reported():
    with pytest.raises(ConfigError):
        parse_repo_config("severity_gate: [unclosed")


def test_missing_repo_config_is_not_an_error():
    assert parse_repo_config(None) == {}
    assert parse_repo_config("   ") == {}


def test_fingerprint_changes_with_config():
    base = resolve()
    changed = resolve(request_overrides={"model_endpoint": "other"})
    assert base.fingerprint() != changed.fingerprint()
    assert base.fingerprint() == resolve().fingerprint()
