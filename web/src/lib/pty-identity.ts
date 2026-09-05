/** Stable, route-scoped identity for the dashboard's keep-alive PTY. */

const PTY_IDENTITY_KEY = "hermes.pty.identity.chat";

interface StoredPtyIdentity {
  scope: string;
  token: string;
}

function mintToken(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

function readIdentity(): StoredPtyIdentity | null {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(PTY_IDENTITY_KEY) ?? "null");
    return typeof parsed?.scope === "string" &&
      typeof parsed?.token === "string" &&
      /^[a-f0-9]{32}$/.test(parsed.token)
      ? parsed
      : null;
  } catch {
    return null;
  }
}

export function ptyAttachToken(scope: string, rotate = false): string {
  const stored = rotate ? null : readIdentity();
  if (stored?.scope === scope) return stored.token;

  const token = mintToken();
  try {
    window.sessionStorage.setItem(
      PTY_IDENTITY_KEY,
      JSON.stringify({ scope, token } satisfies StoredPtyIdentity),
    );
  } catch {
    /* Private mode / storage blocked: the current render still has a safe token. */
  }
  return token;
}

export function stableChannelId(scope: string): string {
  let hash = 2166136261;
  for (let index = 0; index < scope.length; index += 1) {
    hash ^= scope.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `chat-${ptyAttachToken(scope)}-${(hash >>> 0).toString(36)}`;
}
