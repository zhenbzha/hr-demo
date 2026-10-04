import type {
  AskHrAgentClient,
  AskHrAgentResponse,
} from "./foundry-client.js";

interface ProcessAskHrMessageOptions {
  question: string | undefined;
  userObjectId: string | undefined;
  previousResponseId?: string;
  acquireSearchToken: () => Promise<string | undefined>;
  askHrAgentClient: AskHrAgentClient;
}

export const processAskHrMessage = async (
  options: ProcessAskHrMessageOptions,
): Promise<AskHrAgentResponse> => {
  const question = options.question?.trim();
  if (!question) {
    throw new Error("An HR policy question is required");
  }
  if (!options.userObjectId) {
    throw new Error(
      "The Microsoft 365 activity did not contain a trusted Entra user object ID",
    );
  }

  const searchToken = await options.acquireSearchToken();
  if (!searchToken) {
    throw new Error(
      "Unable to acquire an Azure AI Search token for the signed-in user",
    );
  }

  return options.askHrAgentClient.ask({
    question,
    searchToken,
    userObjectId: options.userObjectId,
    previousResponseId: options.previousResponseId,
  });
};
