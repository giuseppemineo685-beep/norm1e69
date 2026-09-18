import { createHash } from "crypto";

export const COOKIE = "jh_session";

export function expectedToken(): string | null {
  const pw = process.env.APP_PASSWORD;
  if (!pw) return null; // no password configured: open access (local dev)
  return createHash("sha256").update(`jobhunt:${pw}`).digest("hex");
}

export function isValidToken(token: string | undefined): boolean {
  const expected = expectedToken();
  return expected === null || token === expected;
}
