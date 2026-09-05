import { Button } from "@nous-research/ui/ui/components/button";

import type { DashboardClarifyPrompt } from "@/lib/api";

interface DashboardHandoffCardProps {
  answering: boolean;
  computerUrl: string | null;
  error: string | null;
  onChoice: (choiceIndex: number) => void;
  prompt: DashboardClarifyPrompt;
}

export function DashboardHandoffCard({
  answering,
  computerUrl,
  error,
  onChoice,
  prompt,
}: DashboardHandoffCardProps) {
  return (
    <section
      aria-label="Manager is waiting for you"
      className="absolute inset-x-2 bottom-2 z-30 max-h-[80%] overflow-y-auto rounded-lg border border-white/30 bg-black/95 p-4 text-white shadow-2xl sm:inset-x-3 sm:bottom-3 sm:mx-auto sm:max-w-xl"
      data-clarify-request-id={prompt.request_id}
    >
      <div className="text-xs font-semibold uppercase tracking-[0.16em] text-white/60">
        Human handoff
      </div>
      <h2 className="mt-2 text-base font-semibold leading-snug">
        The manager is waiting while you use the computer.
      </h2>
      <p className="mt-2 text-sm leading-relaxed text-white/85">
        {prompt.question}
      </p>
      {prompt.retry_message && (
        <p className="mt-3 text-xs leading-relaxed text-amber-200" role="status">
          {prompt.retry_message}
        </p>
      )}
      {computerUrl && (
        <>
          <a
            className="mt-4 flex min-h-11 w-full items-center justify-center rounded border border-white/40 bg-white/10 px-4 py-2 text-sm font-semibold text-white hover:bg-white/20 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
            href={computerUrl}
            rel="noopener noreferrer"
            target="_blank"
          >
            Open computer
          </a>
          <p className="mt-3 text-xs leading-relaxed text-white/60">
            Opening the computer does not resume the manager. Return here and choose an answer when the human step is done.
          </p>
        </>
      )}
      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        {prompt.choices.map((choice, index) => (
          <Button
            key={`${prompt.request_id}-${index}`}
            className="min-h-11 w-full px-4 py-2 text-sm"
            disabled={answering}
            onClick={() => onChoice(index)}
            aria-label={`Answer manager: ${choice}`}
          >
            {answering ? "Sending…" : choice}
          </Button>
        ))}
      </div>
      {error && (
        <p className="mt-3 text-xs text-red-300" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
