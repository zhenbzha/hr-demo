from __future__ import annotations

import logging

import jwt
from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    InvokeResponse,
    TokenExchangeInvokeRequest,
    TokenExchangeInvokeResponse,
)
from microsoft_agents.hosting.core import (
    Authorization,
    ChannelAdapter,
    TurnContext,
    TurnState,
)


logger = logging.getLogger("ask-hr-activity.authorization")


class ActivityAuthorization(Authorization):
    async def _on_turn_auth_intercept(
        self,
        context: TurnContext,
        state: TurnState,
    ):
        is_invoke = context.activity.type == ActivityTypes.invoke
        is_token_exchange = (
            is_invoke
            and context.activity.name == "signin/tokenExchange"
        )
        is_signin_failure = (
            is_invoke
            and context.activity.name == "signin/failure"
        )
        if is_invoke:
            logger.info(
                "Custom authorization intercept started for invoke name=%s.",
                context.activity.name,
            )
        if is_token_exchange:
            request = TokenExchangeInvokeRequest.model_validate(
                context.activity.value
            )
            context.turn_state[ChannelAdapter.INVOKE_RESPONSE_KEY] = Activity(
                type=ActivityTypes.invoke_response,
                value=InvokeResponse(
                    status=412,
                    body=TokenExchangeInvokeResponse(
                        id=request.id,
                        connection_name=request.connection_name,
                        failure_detail=(
                            "Silent token exchange did not complete. "
                            "Proceed with the standard consent flow."
                        ),
                    ),
                ).model_dump(by_alias=True, exclude_none=True),
            )
        elif is_signin_failure:
            context.turn_state[ChannelAdapter.INVOKE_RESPONSE_KEY] = Activity(
                type=ActivityTypes.invoke_response,
                value=InvokeResponse(
                    status=200,
                    body={},
                ).model_dump(by_alias=True, exclude_none=True),
            )
        self._log_token_metadata(context)
        result = await super()._on_turn_auth_intercept(context, state)

        if not is_token_exchange:
            return result

        response = context.turn_state[ChannelAdapter.INVOKE_RESPONSE_KEY]
        if response.value.get("status") == 412:
            logger.warning(
                "Silent Teams token exchange did not complete; returning "
                "interactive consent fallback."
            )
        return result

    @staticmethod
    def _log_token_metadata(context: TurnContext) -> None:
        if (
            context.activity.type != ActivityTypes.invoke
            or context.activity.name != "signin/tokenExchange"
            or not isinstance(context.activity.value, dict)
        ):
            return
        token = context.activity.value.get("token")
        if not isinstance(token, str) or not token:
            logger.warning("Teams token exchange invoke did not contain a token.")
            return
        try:
            claims = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_aud": False,
                    "verify_exp": False,
                },
            )
        except jwt.PyJWTError:
            logger.warning("Teams token exchange invoke contained an invalid JWT.")
            return
        logger.info(
            "Teams token exchange metadata: aud=%s azp=%s tid=%s",
            claims.get("aud"),
            claims.get("azp") or claims.get("appid"),
            claims.get("tid"),
        )
