"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { db, type Status } from "@/lib/db";
import { COOKIE, expectedToken, isValidToken } from "@/lib/auth";

const ALLOWED: Record<string, Status> = {
  accept: "accepted",
  discard: "discarded",
  applied: "applied",
  reopen: "pending",
  retry: "accepted",
};

async function assertAuth() {
  const token = (await cookies()).get(COOKIE)?.value;
  if (!isValidToken(token)) throw new Error("not authenticated");
}

export async function decide(id: string, action: string) {
  await assertAuth();
  const status = ALLOWED[action];
  if (!id || !status) throw new Error("bad request");
  const patch: Record<string, unknown> = { status, decided_at: new Date().toISOString() };
  if (action === "retry" || action === "reopen") patch.error = null;
  await (await db()).updateJob(id, patch);
  revalidatePath("/");
  revalidatePath("/dashboard");
}

export async function login(formData: FormData) {
  const pw = String(formData.get("password") ?? "");
  const expected = expectedToken();
  const ok = expected === null || pw === process.env.APP_PASSWORD;
  if (!ok) redirect("/login?error=1");
  (await cookies()).set(COOKIE, expected ?? "open", {
    httpOnly: true, sameSite: "lax", secure: process.env.NODE_ENV === "production",
    path: "/", maxAge: 60 * 60 * 24 * 90,
  });
  redirect("/");
}
