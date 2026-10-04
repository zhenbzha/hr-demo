import "server-only";

import { randomUUID } from "node:crypto";

import type { NextAuthOptions } from "next-auth";
import AzureADProvider, {
  type AzureADProfile,
} from "next-auth/providers/azure-ad";

import { config } from "@/lib/config";

type AskHrAzureProfile = AzureADProfile & {
  name?: string;
  oid?: string;
  preferred_username?: string;
};

export const authOptions = {
  providers: [
    AzureADProvider<AskHrAzureProfile>({
      clientId: config.entraClientId,
      clientSecret: config.entraClientSecret,
      tenantId: config.entraTenantId,
      checks: ["pkce", "state"],
      authorization: {
        params: {
          prompt: "select_account",
          scope: "openid profile email",
        },
      },
      profile(profile) {
        return {
          id: profile.oid || profile.sub,
          name: profile.name || null,
          email: profile.preferred_username || profile.email || null,
          image: null,
        };
      },
    }),
  ],
  secret: config.nextAuthSecret,
  session: {
    strategy: "jwt",
    maxAge: 8 * 60 * 60,
  },
  callbacks: {
    jwt({ token }) {
      token.agentSessionId ||= randomUUID();
      return token;
    },
  },
} satisfies NextAuthOptions;
