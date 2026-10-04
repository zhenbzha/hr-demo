import { LogOut } from "lucide-react";

import { Button } from "@/components/ui/button";

export function SignInButton() {
  return (
    <Button asChild className="mt-8 h-12 w-full">
      <a href="/api/auth/login">
        <MicrosoftLogo />
        Sign in with Microsoft
      </a>
    </Button>
  );
}

export function SignOutButton({ compact = false }: { compact?: boolean }) {
  return (
    <form action="/api/auth/logout" method="post">
      <Button
        type="submit"
        variant="outline"
        size={compact ? "sm" : "default"}
        className={compact ? undefined : "mt-4 w-full"}
      >
        {!compact && <LogOut />}
        Sign out
      </Button>
    </form>
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
