from __future__ import annotations

import asyncio
from types import SimpleNamespace

from microsoft_agents.activity import ActivityTypes

from activity_authorization import ActivityAuthorization


async def test_fallback_response() -> None:
    authorization = object.__new__(ActivityAuthorization)

    async def base_intercept(_self, _context, _state):
        return SimpleNamespace(
            should_skip_turn=True,
            should_replay=False,
            continuation_activity=None,
        )

    original = ActivityAuthorization.__mro__[1]._on_turn_auth_intercept
    ActivityAuthorization.__mro__[1]._on_turn_auth_intercept = base_intercept
    try:
        context = SimpleNamespace(
            activity=SimpleNamespace(
                type=ActivityTypes.invoke,
                name="signin/tokenExchange",
                value={
                    "id": "exchange-id",
                    "connectionName": "ask-hr-search-sso",
                    "token": "not-a-jwt",
                },
            ),
            turn_state={},
        )
        result = await authorization._on_turn_auth_intercept(
            context,
            SimpleNamespace(),
        )
        assert result.should_skip_turn
        response = context.turn_state["ChannelAdapter.InvokeResponse"]
        assert response.type == ActivityTypes.invoke_response
        assert response.value["status"] == 412
    finally:
        ActivityAuthorization.__mro__[1]._on_turn_auth_intercept = original


async def test_success_response_wins() -> None:
    authorization = object.__new__(ActivityAuthorization)

    async def base_intercept(_self, context, _state):
        context.turn_state["ChannelAdapter.InvokeResponse"] = SimpleNamespace(
            value={"status": 200}
        )
        return SimpleNamespace(
            should_skip_turn=True,
            should_replay=True,
            continuation_activity=SimpleNamespace(),
        )

    original = ActivityAuthorization.__mro__[1]._on_turn_auth_intercept
    ActivityAuthorization.__mro__[1]._on_turn_auth_intercept = base_intercept
    try:
        context = SimpleNamespace(
            activity=SimpleNamespace(
                type=ActivityTypes.invoke,
                name="signin/tokenExchange",
                value={
                    "id": "exchange-id",
                    "connectionName": "ask-hr-search-sso",
                    "token": "not-a-jwt",
                },
            ),
            turn_state={},
        )
        result = await authorization._on_turn_auth_intercept(
            context,
            SimpleNamespace(),
        )
        assert result.should_replay
        response = context.turn_state["ChannelAdapter.InvokeResponse"]
        assert response.value["status"] == 200
    finally:
        ActivityAuthorization.__mro__[1]._on_turn_auth_intercept = original


async def test_signin_failure_acknowledged() -> None:
    authorization = object.__new__(ActivityAuthorization)

    async def base_intercept(_self, _context, _state):
        return SimpleNamespace(
            should_skip_turn=True,
            should_replay=False,
            continuation_activity=None,
        )

    original = ActivityAuthorization.__mro__[1]._on_turn_auth_intercept
    ActivityAuthorization.__mro__[1]._on_turn_auth_intercept = base_intercept
    try:
        context = SimpleNamespace(
            activity=SimpleNamespace(
                type=ActivityTypes.invoke,
                name="signin/failure",
                value={"error": "resourceMatchFailed"},
            ),
            turn_state={},
        )
        result = await authorization._on_turn_auth_intercept(
            context,
            SimpleNamespace(),
        )
        assert result.should_skip_turn
        response = context.turn_state["ChannelAdapter.InvokeResponse"]
        assert response.type == ActivityTypes.invoke_response
        assert response.value["status"] == 200
    finally:
        ActivityAuthorization.__mro__[1]._on_turn_auth_intercept = original


if __name__ == "__main__":
    asyncio.run(test_fallback_response())
    asyncio.run(test_success_response_wins())
    asyncio.run(test_signin_failure_acknowledged())
    print("Activity authorization tests passed.")
