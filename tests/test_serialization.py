from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from waypoint.serialization import jsonable, message_to_dict


def test_human_message_shape() -> None:
    out = message_to_dict(HumanMessage("hi", id="m1"))
    assert out == {"id": "m1", "type": "human", "content": "hi"}


def test_tool_calls_are_flattened() -> None:
    message = AIMessage(
        content="",
        id="m2",
        tool_calls=[{"name": "now", "args": {"timezone": "UTC"}, "id": "c1"}],
    )
    out = message_to_dict(message)
    assert out["tool_calls"] == [{"id": "c1", "name": "now", "args": {"timezone": "UTC"}}]


def test_tool_message_keeps_its_name() -> None:
    out = message_to_dict(ToolMessage(content="2026", tool_call_id="c1", name="now"))
    assert out["name"] == "now"


def test_jsonable_walks_nested_structures() -> None:
    value = {"messages": [HumanMessage("a")], "nested": {"tuple": (1, 2)}}
    out = jsonable(value)
    assert out["messages"][0]["content"] == "a"
    assert out["nested"]["tuple"] == [1, 2]


def test_jsonable_never_explodes_on_unknown_objects() -> None:
    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    assert jsonable(Weird()) == "<weird>"
