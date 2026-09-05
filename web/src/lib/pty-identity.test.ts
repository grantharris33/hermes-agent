// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ptyAttachToken, stableChannelId } from "./pty-identity";

describe("dashboard PTY identity", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    let seed = 0;
    vi.spyOn(crypto, "getRandomValues").mockImplementation((values) => {
      (values as Uint8Array).fill((seed += 1));
      return values;
    });
  });

  it("survives refresh for the exact resume/profile scope", () => {
    const scope = "stored-a\0default";
    expect(stableChannelId(scope)).toBe(stableChannelId(scope));
    expect(ptyAttachToken(scope)).toBe(ptyAttachToken(scope));
  });

  it("rotates attach and channel identity on session switch", () => {
    const channelA = stableChannelId("stored-a\0default");
    const tokenA = ptyAttachToken("stored-a\0default");
    const channelB = stableChannelId("stored-b\0default");
    const tokenB = ptyAttachToken("stored-b\0default");

    expect(channelB).not.toBe(channelA);
    expect(tokenB).not.toBe(tokenA);
  });

  it("explicit fresh start rotates both identities atomically", () => {
    const scope = "\0default";
    const before = stableChannelId(scope);
    ptyAttachToken(scope, true);
    expect(stableChannelId(scope)).not.toBe(before);
  });
});
