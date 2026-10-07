"""`complete()` drops the optional parameters an endpoint rejects, and only those."""

from types import SimpleNamespace

import pytest

from app.core.errors import ReviewerError
from app.core.llm_client import complete


class FakeClient:
    """OpenAI-shaped client whose endpoint rejects some parameters."""

    def __init__(self, rejects: dict[str, str]) -> None:
        self.rejects = rejects
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        for param, message in self.rejects.items():
            if param in kwargs:
                raise RuntimeError(message)
        response_message = SimpleNamespace(content='{"findings": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=response_message)])


def _complete(client):
    return complete(client, model="m", system_prompt="s", user_prompt="u", temperature=0.0)


def test_endpoint_supporting_everything_is_called_once():
    client = FakeClient(rejects={})
    assert _complete(client) == '{"findings": []}'
    assert len(client.calls) == 1
    assert client.calls[0]["temperature"] == 0.0


def test_response_format_is_dropped_but_temperature_kept():
    client = FakeClient(rejects={"response_format": "400: response_format not supported"})
    _complete(client)
    assert "response_format" not in client.calls[-1]
    assert client.calls[-1]["temperature"] == 0.0


def test_newer_anthropic_models_drop_both():
    """databricks-claude-sonnet-5-5 rejects structured output, then temperature."""
    client = FakeClient(
        rejects={
            "response_format": "INVALID_PARAMETER_VALUE: Structured output is not supported",
            "temperature": "BAD_REQUEST: Model does not support the temperature parameter.",
        }
    )
    assert _complete(client) == '{"findings": []}'
    assert len(client.calls) == 3
    assert "response_format" not in client.calls[-1]
    assert "temperature" not in client.calls[-1]


def test_unrelated_failure_still_raises():
    client = FakeClient(rejects={"model": "503: endpoint unavailable"})
    with pytest.raises(ReviewerError):
        _complete(client)
    # It tried without response_format once, then gave up — temperature kept.
    assert len(client.calls) == 2
    assert client.calls[-1]["temperature"] == 0.0


def test_each_call_records_its_token_usage_for_the_console():
    import mlflow
    from mlflow.tracing.constant import SpanAttributeKey

    class UsageClient(FakeClient):
        def _create(self, **kwargs):
            response = super()._create(**kwargs)
            response.usage = SimpleNamespace(
                prompt_tokens=120, completion_tokens=30, total_tokens=150
            )
            return response

    _complete(UsageClient(rejects={}))
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())

    [span] = trace.data.spans
    assert span.name == "llm:m"
    assert span.attributes["model"] == "m"
    assert span.get_attribute(SpanAttributeKey.CHAT_USAGE) == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
    }
    assert trace.info.token_usage["total_tokens"] == 150
