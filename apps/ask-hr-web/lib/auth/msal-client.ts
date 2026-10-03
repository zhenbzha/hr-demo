import {
  type AccountInfo,
  ConfidentialClientApplication,
  CryptoProvider,
  LogLevel,
} from "@azure/msal-node";

import { config } from "@/lib/config";

export type SerializedAccount = Pick<
  AccountInfo,
  | "homeAccountId"
  | "environment"
  | "tenantId"
  | "username"
  | "localAccountId"
  | "name"
>;

export function createMsalClient() {
  return new ConfidentialClientApplication({
    auth: {
      clientId: config.entraClientId,
      authority: `https://login.microsoftonline.com/${config.entraTenantId}`,
      clientSecret: config.entraClientSecret,
    },
    system: {
      loggerOptions: {
        logLevel: LogLevel.Warning,
        piiLoggingEnabled: false,
        loggerCallback: () => undefined,
      },
    },
  });
}

export const cryptoProvider = new CryptoProvider();
