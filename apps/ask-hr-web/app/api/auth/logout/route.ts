import { NextResponse } from "next/server";

import { config } from "@/lib/config";

export const runtime = "nodejs";

export async function GET() {
  const response = NextResponse.redirect(new URL("/", config.appBaseUrl));
  response.cookies.delete(config.sessionCookieName);
  return response;
}
