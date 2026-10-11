from __future__ import annotations

import logging
import re

from azure.ai.agentserver.activity import ActivityAgentServerHost
from azure.ai.agentserver.activity import FoundryStorage, get_hosted_agent_env
from azure.ai.agentserver.core import AgentConfig
from microsoft_agents.activity import Activity, ActivityTypes, InvokeResponse
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.core import (
    AgentApplication,
    ApplicationOptions,
    HttpAdapterBase,
    MemoryStorage,
    MessageFactory,
    RestChannelServiceClientFactory,
    TurnContext,
    TurnState,
)

import activity_core
from activity_authorization import ActivityAuthorization
from activity_format import format_activity_answer


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("ask-hr-activity")

connection_config = get_hosted_agent_env()
resolved_config = load_configuration_from_env(connection_config)
storage = (
    FoundryStorage()
    if AgentConfig.from_env().is_hosted
    else MemoryStorage()
)
connection_manager = MsalConnectionManager(**resolved_config)
authorization = ActivityAuthorization(
    storage,
    connection_manager,
    **resolved_config,
)
adapter = HttpAdapterBase(
    channel_service_client_factory=RestChannelServiceClientFactory(
        connection_manager
    )
)
app = AgentApplication[TurnState](
    options=ApplicationOptions(
        adapter=adapter,
        storage=storage,
        start_typing_timer=False,
    ),
    authorization=authorization,
)
host = ActivityAgentServerHost(agent_app=app)
logger.info(
    "Activity authorization implementation: %s.%s",
    type(app.auth).__module__,
    type(app.auth).__name__,
)


@app.activity(ActivityTypes.conversation_update)
async def on_conversation_update(
    context: TurnContext,
    _state: TurnState,
) -> None:
    recipient_id = context.activity.recipient.id if context.activity.recipient else None
    for member in context.activity.members_added or []:
        if member.id and member.id != recipient_id:
            await context.send_activity(
                MessageFactory.text(
                    "Ask a question about Global, India, Finland, or Hong Kong "
                    "HR policies."
                )
            )
            return


@app.activity(ActivityTypes.invoke)
async def on_invoke(
    context: TurnContext,
    _state: TurnState,
) -> None:
    # Authorization middleware processes Teams signin/tokenExchange invokes
    # before routing. A matching route is still required to avoid a 501 after
    # the middleware completes the exchange.
    response = InvokeResponse(status=200, body={})
    await context.send_activity(
        Activity(
            type=ActivityTypes.invoke_response,
            value=response.model_dump(by_alias=True, exclude_none=True),
        )
    )


@app.message(re.compile(r".*", re.DOTALL))
async def on_message(
    context: TurnContext,
    _state: TurnState,
) -> None:
    question = (context.activity.text or "").strip()
    if not question:
        await context.send_activity(
            MessageFactory.text("Enter an HR policy question.")
        )
        return
    if len(question) > activity_core.MAX_QUESTION_LENGTH:
        await context.send_activity(
            MessageFactory.text(
                f"Message must be {activity_core.MAX_QUESTION_LENGTH} "
                "characters or fewer."
            )
        )
        return

    status = await context.send_activity(
        MessageFactory.text("Checking the applicable HR policy...")
    )
    if not status or not status.id:
        raise RuntimeError("Unable to create the Ask HR status activity")

    async def update_status(text: str) -> None:
        activity = MessageFactory.text(text)
        activity.id = status.id
        await context.update_activity(activity)

    try:
        token_response = await app.auth.get_token(context, "SEARCH")
        if not token_response or not token_response.token:
            raise PermissionError(
                "Unable to acquire an Azure AI Search token for the signed-in user"
            )

        answer, citations = await activity_core.answer_question(
            question,
            user_search_token=token_response.token,
            on_progress=update_status,
        )
        await update_status(format_activity_answer(answer, citations))
    except Exception as error:
        logger.exception("Ask HR Activity turn failed", exc_info=error)
        await update_status(
            "I couldn't verify your access to the requested HR policy information."
        )


@app.error
async def on_error(context: TurnContext, error: Exception) -> None:
    logger.exception("Ask HR Activity turn failed", exc_info=error)
    await context.send_activity(
        MessageFactory.text(
            "I couldn't verify your access to the requested HR policy information."
        )
    )


if __name__ == "__main__":
    host.run()
