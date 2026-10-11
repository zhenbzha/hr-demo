from __future__ import annotations

import asyncio
import re

from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    Channels,
    SignInConstants,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    ApplicationOptions,
    AuthHandler,
    Authorization,
    ClaimsIdentity,
    MemoryStorage,
    MessageFactory,
    TurnContext,
    TurnState,
)
from microsoft_agents.testing import TestAdapter


OAUTH_CONNECTION = "ask-hr-search-sso"
USER_ID = "test-user"
BOT_ID = "test-bot"


class FakeConnections:
    def get_connection(self, _connection_name: str):
        return None

    def get_default_connection(self):
        return None

    def get_token_provider(self, _claims_identity, _service_url):
        return None

    def get_token_provider_from_activity(
        self,
        _claims_identity,
        _activity,
    ):
        return None

    def get_default_connection_configuration(self):
        return None


async def run_test() -> None:
    adapter = TestAdapter(channel_id=Channels.ms_teams)
    adapter.conversation.user.id = USER_ID
    adapter.conversation.agent.id = BOT_ID
    adapter.add_exchangeable_token(
        connection_name=OAUTH_CONNECTION,
        channel_id=Channels.ms_teams,
        user_id=USER_ID,
        exchangeable_item="teams-sso-token",
        token="delegated-search-token",
    )
    user_token_client = adapter._user_token_client
    exchange_token = user_token_client.exchange_token

    async def exchange_and_store(
        user_id,
        connection_name,
        channel_id,
        exchange_request,
    ):
        response = await exchange_token(
            user_id,
            connection_name,
            channel_id,
            exchange_request,
        )
        if response.token:
            user_token_client.add_user_token(
                connection_name=connection_name,
                channel_id=channel_id,
                user_id=user_id,
                token=response.token,
            )
        return response

    user_token_client.exchange_token = exchange_and_store

    connections = FakeConnections()
    storage = MemoryStorage()
    app = AgentApplication[TurnState](
        options=ApplicationOptions(
            adapter=adapter,
            storage=storage,
            authorization_handlers={
                "SEARCH": AuthHandler(
                    name="SEARCH",
                    title="HR policy access",
                    text="Continue with your Microsoft 365 account",
                    abs_oauth_connection_name=OAUTH_CONNECTION,
                )
            },
            start_typing_timer=False,
        ),
        authorization=Authorization(
            storage,
            connections,
            auth_handlers={
                "SEARCH": AuthHandler(
                    name="SEARCH",
                    title="HR policy access",
                    text="Continue with your Microsoft 365 account",
                    abs_oauth_connection_name=OAUTH_CONNECTION,
                )
            },
            auto_sign_in=True,
        ),
    )
    replay_completed = asyncio.Event()

    @app.message(re.compile(r".*", re.DOTALL))
    async def answer(context: TurnContext, _state: TurnState) -> None:
        status = await context.send_activity(
            MessageFactory.text("Checking the applicable HR policy...")
        )
        assert status and status.id
        token = await app.auth.get_token(context, "SEARCH")
        assert token.token == "delegated-search-token"
        final = MessageFactory.text("authorized answer")
        final.id = status.id
        await context.update_activity(final)
        replay_completed.set()

    claims = ClaimsIdentity(
        {"aud": BOT_ID, "appid": BOT_ID},
        is_authenticated=True,
        authentication_type="Bearer",
    )
    original = adapter.create_activity(
        "What is the maternity leave policy in Finland?"
    )
    await adapter.process_activity(claims, original, app.on_turn)

    oauth_activity = await adapter.get_next_reply_async()
    assert oauth_activity is not None
    assert oauth_activity.type == ActivityTypes.message
    assert oauth_activity.attachments
    oauth_card = oauth_activity.attachments[0].content
    exchange_id = oauth_card.token_exchange_resource.id

    token_exchange = Activity(
        type=ActivityTypes.invoke,
        name=SignInConstants.token_exchange_operation_name,
        channel_id=Channels.ms_teams,
        service_url=original.service_url,
        from_property=original.from_property,
        recipient=original.recipient,
        conversation=original.conversation,
        value={
            "id": exchange_id,
            "connectionName": OAUTH_CONNECTION,
            "token": "teams-sso-token",
        },
    )
    await adapter.process_activity(
        claims,
        token_exchange,
        app.on_turn,
    )

    invoke_activity = await asyncio.wait_for(
        adapter.get_next_reply_async(),
        timeout=2,
    )
    assert invoke_activity is not None
    assert invoke_activity.type == ActivityTypes.invoke_response
    assert invoke_activity.value["status"] == 200

    await asyncio.wait_for(replay_completed.wait(), timeout=5)
    await asyncio.sleep(0)
    replies = [
        activity
        for activity in adapter.activity_queue
        if activity.text == "authorized answer"
    ]
    assert len(replies) == 1


if __name__ == "__main__":
    asyncio.run(run_test())
    print("Activity OAuth/Search token flow test passed.")
