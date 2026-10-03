import { ChatShell } from "@/components/chat/chat-shell";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { getCurrentSession } from "@/lib/auth/session";

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ authError?: string }>;
}) {
  const [session, params] = await Promise.all([
    getCurrentSession(),
    searchParams,
  ]);

  if (session?.account) {
    return (
      <ChatShell
        user={{
          name: session.account.name,
          username: session.account.username,
        }}
      />
    );
  }

  return (
    <main className="grid min-h-screen bg-slate-50 lg:grid-cols-[480px_1fr]">
      <section className="flex flex-col border-r border-slate-200 bg-white px-8 py-8 md:px-14 md:py-10">
        <Brand />
        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center">
          <p className="text-sm font-medium text-emerald-700">
            Employee support
          </p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight text-slate-950">
            Ask HR with confidence
          </h1>
          <p className="mt-4 text-base leading-7 text-slate-600">
            Get grounded answers from Global, India, Finland, and Hong Kong HR
            policies, with document and page citations.
          </p>

          {params.authError && (
            <p className="mt-6 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {params.authError}
            </p>
          )}

          <Button asChild className="mt-8 h-12 w-full">
            <a href="/api/auth/login">
              <MicrosoftLogo />
              Sign in with Microsoft
            </a>
          </Button>

          <p className="mt-4 text-xs leading-5 text-slate-400">
            Fictional demo policies for testing. Confirm employment decisions
            with your HR team.
          </p>
        </div>
      </section>

      <section className="hidden items-center justify-center overflow-hidden bg-[radial-gradient(circle_at_18%_12%,rgba(16,185,129,0.16),transparent_35%),radial-gradient(circle_at_84%_82%,rgba(14,116,144,0.14),transparent_38%),linear-gradient(140deg,#f8fafc,#ecfdf5)] p-12 lg:flex">
        <Card className="w-full max-w-3xl border-white/70 bg-white/90 p-6 shadow-2xl backdrop-blur">
          <div className="flex items-center gap-3 border-b border-slate-100 pb-5">
            <LogoMark />
            <div>
              <p className="font-semibold text-slate-950">Ask HR</p>
              <p className="text-sm text-slate-500">
                Regional policy assistant
              </p>
            </div>
          </div>
          <div className="space-y-7 px-4 py-10">
            <div className="ml-auto max-w-md rounded-3xl bg-slate-100 px-5 py-4 text-sm leading-6 text-slate-700">
              What are the Hong Kong MPF contribution rules?
            </div>
            <div className="max-w-xl">
              <p className="text-sm leading-7 text-slate-800">
                Hong Kong employees aged 18–64 who have been continuously
                employed for at least 60 days generally participate in MPF.
                Current rates and thresholds must be verified with Payroll.
              </p>
              <div className="mt-4 inline-flex rounded-xl border border-emerald-100 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
                Hong Kong HR Payroll, MPF and Benefits · Page 2
              </div>
            </div>
          </div>
          <div className="rounded-2xl border border-slate-200 px-4 py-3 text-sm text-slate-400">
            Ask an HR-related question
          </div>
        </Card>
      </section>
    </main>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-3">
      <LogoMark />
      <div>
        <p className="font-semibold text-slate-950">Ask HR</p>
        <p className="text-xs text-slate-500">Powered by Microsoft Foundry</p>
      </div>
    </div>
  );
}

function LogoMark() {
  return (
    <div className="grid h-10 w-10 place-items-center rounded-xl bg-emerald-700 text-sm font-bold text-white shadow-sm">
      HR
    </div>
  );
}

function MicrosoftLogo() {
  return (
    <svg viewBox="0 0 21 21" className="h-5 w-5" aria-hidden="true">
      <path fill="#f25022" d="M1 1h9v9H1z" />
      <path fill="#7fba00" d="M11 1h9v9h-9z" />
      <path fill="#00a4ef" d="M1 11h9v9H1z" />
      <path fill="#ffb900" d="M11 11h9v9h-9z" />
    </svg>
  );
}
