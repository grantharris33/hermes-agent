// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardHandoffCard } from "./DashboardHandoffCard";

const prompt = {
  choices: ["Continue", "Wait"],
  question: "Complete the sign-in in the shared browser, then return here.",
  request_id: "server-request-1",
};

describe("DashboardHandoffCard", () => {
  let host: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  afterEach(async () => {
    await act(async () => root?.unmount());
    host?.remove();
  });

  async function render(computerUrl: string | null, onChoice = vi.fn()) {
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () =>
      root.render(
        <DashboardHandoffCard
          answering={false}
          computerUrl={computerUrl}
          error={null}
          onChoice={onChoice}
          prompt={prompt}
        />,
      ),
    );
    return onChoice;
  }

  it("opens the configured computer without answering the manager", async () => {
    const onChoice = await render("https://computer.example.test/");
    const link = host.querySelector<HTMLAnchorElement>('a[target="_blank"]');

    expect(link?.href).toBe("https://computer.example.test/");
    expect(link?.rel).toContain("noopener");
    expect(host.textContent).toContain("Opening the computer does not resume");
    expect(onChoice).not.toHaveBeenCalled();
  });

  it("submits only the exact server-owned choice index", async () => {
    const onChoice = await render(null);
    expect(host.querySelector('a[target="_blank"]')).toBeNull();
    const button = Array.from(host.querySelectorAll("button")).find(
      (candidate) => candidate.textContent === "Continue",
    );
    await act(async () => button?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onChoice).toHaveBeenCalledWith(0);
  });

  it("keeps a failed recovered handoff visibly actionable", async () => {
    const onChoice = vi.fn();
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    await act(async () =>
      root.render(
        <DashboardHandoffCard
          answering={false}
          computerUrl={null}
          error={null}
          onChoice={onChoice}
          prompt={{ ...prompt, retry_message: "The manager could not resume yet. Retry." }}
        />,
      ),
    );

    expect(host.querySelector('[role="status"]')?.textContent).toContain("could not resume");
    expect(host.textContent).toContain("Continue");
  });
});
