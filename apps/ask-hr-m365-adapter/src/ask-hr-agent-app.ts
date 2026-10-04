import type { Activity } from "@microsoft/agents-activity";
import {
  AgentApplication,
  type CloudAdapter,
  MemoryStorage,
  MessageFactory,
  type TurnContext,
  type TurnState,
} from "@microsoft/agents-hosting";

import type { AskHrAgentClient } from "./foundry-client.js";
import { formatAskHrResponse } from "./format-response.js";
import { processAskHrMessage } from "./process-message.js";
import { replaceStatusOrSend } from "./run-turn.js";

const previousResponseIdKey = "conversation.askHrPreviousResponseId";
const safeErrorMessage =
  "I couldn't verify your access to the requested HR policy information.";

export class AskHrAgentApplication extends AgentApplication<TurnState> {
  constructor(
    private readonly askHrAgentClient: AskHrAgentClient,
    agentAppId: string,
    adapter: CloudAdapter,
  ) {
    super({
      storage: new MemoryStorage(),
      agentAppId,
      adapter,
      startTypingTimer: false,
      longRunningMessages: true,
    });

    this.onConversationUpdate("membersAdded", this.handleWelcome);
    this.onActivity("message", this.handleMessage, ["search"]);
    this.onError(this.handleTurnError);
  }

  private readonly handleWelcome = async (
    context: TurnContext,
  ): Promise<void> => {
    if (!shouldSendWelcome(context.activity)) {
      return;
    }
    await context.sendActivity(
      MessageFactory.text(
        "Ask a question about Global, India, Finland, or Hong Kong HR policies.",
      ),
    );
  };

  private readonly handleMessage = async (
    context: TurnContext,
    state: TurnState,
  ): Promise<void> => {
    if (!context.activity.text?.trim()) {
      await context.sendActivity(
        MessageFactory.text("Enter an HR policy question."),
      );
      return;
    }

    const startedAt = Date.now();
    let finalMessage: ReturnType<typeof MessageFactory.text>;
    try {
      console.info("Ask HR request started", {
        activityId: context.activity.id,
      });
      const response = await processAskHrMessage({
        question: context.activity.text,
        userObjectId: context.activity.from?.aadObjectId,
        previousResponseId: state.getValue<string | undefined>(
          previousResponseIdKey,
        ),
        acquireSearchToken: async (): Promise<string | undefined> => {
          const token = await this.authorization.getToken(context, "search");
          if (token?.token) {
            console.info("Search OBO token acquired", {
              activityId: context.activity.id,
            });
          }
          return token?.token;
        },
        askHrAgentClient: this.askHrAgentClient,
      });
      state.setValue(previousResponseIdKey, response.responseId);
      finalMessage = MessageFactory.text(formatAskHrResponse(response));
      console.info("Ask HR request completed through Foundry", {
        activityId: context.activity.id,
        citationLabels: response.citations.map(
          (citation) => citation.label,
        ),
        durationMs: Date.now() - startedAt,
      });
    } catch (error) {
      console.error(
        "Ask HR request failed",
        error instanceof Error ? error.message : "Unknown error",
      );
      finalMessage = MessageFactory.text(safeErrorMessage);
    }

    await replaceStatusOrSend(context, finalMessage);
  };

  private readonly handleTurnError = async (
    context: TurnContext,
    error: Error,
  ): Promise<void> => {
    console.error("Unhandled Ask HR turn error", error.message);
    try {
      await replaceStatusOrSend(
        context,
        MessageFactory.text(safeErrorMessage),
      );
    } catch (deliveryError) {
      console.error(
        "Unable to deliver Ask HR error message",
        deliveryError instanceof Error
          ? deliveryError.message
          : "Unknown error",
      );
    }
  };
}

export const shouldSendWelcome = (activity: Activity): boolean => {
  const recipientId = activity.recipient?.id;
  return (
    activity.membersAdded?.some(
      (member) => Boolean(member.id && member.id !== recipientId),
    ) ?? false
  );
};
