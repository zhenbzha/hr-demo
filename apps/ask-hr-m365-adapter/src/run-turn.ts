import { type Activity, ActivityTypes } from "@microsoft/agents-activity";
import {
  type AgentApplication,
  MessageFactory,
  type TurnContext,
  type TurnState,
} from "@microsoft/agents-hosting";

export const statusActivityIdKey = "askHrStatusActivityId";

interface ImmediateStatusOptions {
  channelAuthenticatedAt?: number;
}

export const runWithImmediateStatus = async (
  context: TurnContext,
  agent: AgentApplication<TurnState>,
  options: ImmediateStatusOptions = {},
): Promise<void> => {
  if (context.activity.type === ActivityTypes.Message) {
    const statusStartedAt = performance.now();
    try {
      const status = await context.sendActivity(
        MessageFactory.text("Reviewing the relevant HR policies..."),
      );
      context.activity.channelData = {
        ...context.activity.channelData,
        [statusActivityIdKey]: status?.id,
      };
      console.info("Initial status delivered", {
        activityId: context.activity.id,
        statusSendMs: Math.round(performance.now() - statusStartedAt),
        authToStatusMs:
          options.channelAuthenticatedAt === undefined
            ? undefined
            : Math.round(
                performance.now() - options.channelAuthenticatedAt,
              ),
      });
    } catch (error) {
      console.warn("Unable to deliver initial status message", {
        activityId: context.activity.id,
        error: error instanceof Error ? error.message : "Unknown error",
      });
    }
  }

  await agent.run(context);
};

export const replaceStatusOrSend = async (
  context: TurnContext,
  activity: Activity,
): Promise<void> => {
  const statusId = context.activity.channelData?.[
    statusActivityIdKey
  ] as string | undefined;
  if (statusId) {
    activity.id = statusId;
    try {
      await context.updateActivity(activity);
      return;
    } catch (error) {
      console.warn("Unable to replace status message", {
        activityId: context.activity.id,
        error: error instanceof Error ? error.message : "Unknown error",
      });
      activity.id = undefined;
    }
  }

  await context.sendActivity(activity);
};
