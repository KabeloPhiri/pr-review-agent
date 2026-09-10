import pytest

from app.core.registry import Registry


def test_register_and_create():
    registry: Registry[str] = Registry("thing")

    @registry.register("upper")
    class Upper:
        def __init__(self, value):
            self.value = value.upper()

    assert registry.available() == ["upper"]
    assert registry.create("upper", "abc").value == "ABC"


def test_unknown_name_lists_alternatives():
    registry: Registry[str] = Registry("thing")
    registry.register("one")(lambda: "one")

    with pytest.raises(ValueError) as exc:
        registry.create("two")
    assert "Unknown thing: 'two'" in str(exc.value)
    assert "one" in str(exc.value)


def test_duplicate_registration_is_rejected():
    registry: Registry[str] = Registry("thing")
    registry.register("dup")(lambda: "a")
    with pytest.raises(ValueError):
        registry.register("dup")(lambda: "b")


def test_shipped_plugins_are_discoverable():
    from app.services.quality import known_quality_connectors
    from app.services.review import known_reviewers
    from app.services.scm import known_scm_connectors

    assert "azure_devops" in known_scm_connectors()
    assert "fake" in known_scm_connectors()
    assert "noop" in known_quality_connectors()
    assert "llm" in known_reviewers()
