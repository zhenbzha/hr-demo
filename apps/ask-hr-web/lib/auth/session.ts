import "server-only";

import type { NextRequest } from "next/server";
import { getServerSession } from "next-auth";
import { getToken } from "next-auth/jwt";

import { authOptions } from "@/lib/auth/options";
import { config } from "@/lib/config";

export function getCurrentSession() {
  return getServerSession(authOptions);
}

export function getCurrentToken(request: NextRequest) {
  return getToken({
    req: request,
    secret: config.nextAuthSecret,
  });
}
