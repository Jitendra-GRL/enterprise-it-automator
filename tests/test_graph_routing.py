from app.agent.graph import (
    _extract_json_array,
    _extract_username,
    route_after_plan,
    route_after_step_check,
)


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    async def ainvoke(self, messages):
        return _FakeResponse(self.reply)


async def test_extract_username_plain():
    assert await _extract_username(_FakeLLM("jsmith"), "ticket text") == "jsmith"


async def test_extract_username_strips_quotes_and_whitespace():
    assert await _extract_username(_FakeLLM('  "jsmith"  '), "ticket text") == "jsmith"


async def test_extract_username_takes_first_line_only():
    assert await _extract_username(_FakeLLM("jsmith\nextra prose"), "ticket text") == "jsmith"


async def test_extract_username_none_returns_none():
    assert await _extract_username(_FakeLLM("NONE"), "ticket text") is None


async def test_extract_username_empty_returns_none():
    assert await _extract_username(_FakeLLM("   "), "ticket text") is None


def test_extract_json_array_plain():
    assert _extract_json_array('[{"tool": "get_user", "args": {}}]') == [
        {"tool": "get_user", "args": {}}
    ]


def test_extract_json_array_with_markdown_fence():
    raw = '```json\n[{"tool": "get_user", "args": {}}]\n```'
    assert _extract_json_array(raw) == [{"tool": "get_user", "args": {}}]


def test_extract_json_array_with_surrounding_prose():
    raw = 'Here is my plan:\n[{"tool": "get_user", "args": {}}]\nLet me know if this works.'
    assert _extract_json_array(raw) == [{"tool": "get_user", "args": {}}]


def test_extract_json_array_empty_plan():
    assert _extract_json_array("[]") == []


def test_extract_json_array_raises_on_garbage():
    import pytest

    with pytest.raises(ValueError):
        _extract_json_array("I refuse to produce JSON today.")


def test_route_after_plan_goes_to_finalize_on_error():
    state = {"error": "boom", "plan": []}
    assert route_after_plan(state) == "finalize"


def test_route_after_plan_goes_to_finalize_on_empty_plan():
    state = {"error": None, "plan": []}
    assert route_after_plan(state) == "finalize"


def test_route_after_plan_goes_to_route_step_when_plan_exists():
    state = {"error": None, "plan": [{"tool": "get_user", "args": {}, "reasoning": ""}]}
    assert route_after_plan(state) == "route_step"


def test_route_after_step_check_finalizes_when_plan_exhausted():
    state = {"plan": [{"tool": "get_user", "args": {}}], "plan_index": 1}
    assert route_after_step_check(state) == "finalize"


def test_route_after_step_check_awaits_approval_for_sensitive_tool():
    state = {"plan": [{"tool": "disable_user", "args": {"username": "x"}}], "plan_index": 0}
    assert route_after_step_check(state) == "await_approval"


def test_route_after_step_check_executes_non_sensitive_tool_directly():
    state = {"plan": [{"tool": "grant_access", "args": {"username": "x", "resource": "vpn"}}], "plan_index": 0}
    assert route_after_step_check(state) == "execute_step"
