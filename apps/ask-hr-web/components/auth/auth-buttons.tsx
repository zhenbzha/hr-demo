"use client";

import { LogOut } from "lucide-react";
import { signIn, signOut } from "next-auth/react";

import { Button } from "@/components/ui/button";

export function SignInButton() {
  return (
    <Button
      type="button"
      className="mt-8 h-12 w-full"
      onClick={() => void signIn("azure-ad", { callbackUrl: "/" })}
    >
      <MicrosoftLogo />
      Sign in with Microsoft
    </Button>
  );
}

export function SignOutButton({ compact = false }: { compact?: boolean }) {
  return (
    <Button
      type="button"
      variant="outline"
      size={compact ? "sm" : "default"}
      className={compact ? undefined : "mt-4 w-full"}
      onClick={() => void signOut({ callbackUrl: "/" })}
    >
      {!compact && <LogOut />}
      Sign out
    </Button>
  );
}

function MicrosoftLogo() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-4 w-4">
      <path fill="#f25022" d="M2 2h9v9H2z" />
      <path fill="#7fba00" d="M13 2h9v9h-9z" />
      <path fill="#00a4ef" d="M2 13h9v9H2z" />
      <path fill="#ffb900" d="M13 13h9v9h-9z" />
    </svg>
  );
}
