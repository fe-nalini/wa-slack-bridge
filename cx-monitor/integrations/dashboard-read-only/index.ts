// PROPOSAL ONLY: deploy as cx-monitor-onboarding-read in Atendimento Premium
// after approval. It has no INSERT, UPDATE, DELETE, RPC, storage or sync actions.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
});
async function tokenMatches(supplied: string, expected: string): Promise<boolean> {
  if (expected.length < 32 || !supplied) return false;
  const enc = new TextEncoder();
  const a = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(supplied)));
  const b = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(expected)));
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}
Deno.serve(async (req) => {
  if (req.method !== "GET") return json({ error: "method_not_allowed" }, 405);
  const token = req.headers.get("Authorization")?.replace(/^Bearer /, "") ?? "";
  if (!await tokenMatches(token, Deno.env.get("CX_MONITOR_READ_TOKEN") ?? "")) {
    return json({ error: "unauthorized" }, 401);
  }
  try {
    const url = Deno.env.get("PONTADELANCA_SUPABASE_URL");
    const key = Deno.env.get("PONTADELANCA_SERVICE_ROLE_KEY");
    if (!url || !key) return json({ error: "source_not_configured", complete: false }, 503);
    // Existing source credential stays in the source backend. The monitor only
    // receives a separate credential usable on this fixed read endpoint.
    const db = createClient(url, key, { auth: { persistSession: false, autoRefreshToken: false } });
    const members: Record<string, unknown>[] = [];
    let expectedMembers: number | null = null;
    for (let offset = 0; ; offset += 1000) {
      if (offset >= 100000) throw new Error("limit");
      const { data, error, count } = await db.from("onboarding_members")
        .select("id,full_name,company_name,status,ca_id", { count: "exact" })
        .is("deleted_at", null).not("full_name", "is", null)
        .in("status", ["pending", "submitted", "active", "approved", "completed", "cancelled", "paused", "in_progress"])
        .order("id").range(offset, offset + 999);
      if (error || !data || count === null) throw new Error("members");
      expectedMembers ??= count;
      if (count !== expectedMembers) throw new Error("changed_count");
      members.push(...data);
      if (data.length < 1000) break;
    }
    if (members.length !== expectedMembers || new Set(members.map(m => m.id)).size !== members.length) {
      throw new Error("member_count");
    }
    const ids = new Set(members.map(m => m.id));
    const steps: Record<string, unknown>[] = [];
    const scannedIds = new Set<unknown>();
    let scannedSteps = 0, expectedSteps: number | null = null;
    for (let offset = 0; ; offset += 1000) {
      if (offset >= 1000000) throw new Error("limit");
      const { data, error, count } = await db.from("onboarding_steps")
        .select("id,member_id,step_number,step_name,status,phase,planned_at,completed_at,completed_by,is_optional", { count: "exact" })
        .order("id").range(offset, offset + 999);
      if (error || !data || count === null) throw new Error("steps");
      expectedSteps ??= count;
      if (count !== expectedSteps) throw new Error("changed_count");
      for (const step of data) {
        if (scannedIds.has(step.id)) throw new Error("duplicate");
        scannedIds.add(step.id); scannedSteps++;
        if (ids.has(step.member_id)) steps.push(step);
      }
      if (data.length < 1000) break;
    }
    if (scannedSteps !== expectedSteps) throw new Error("step_count");
    return json({ schema_version: 1, complete: true, fetched_at: new Date().toISOString(),
      members, steps, coverage: { member_count: members.length, step_count: steps.length,
        scanned_step_count: scannedSteps, source_step_count: expectedSteps } });
  } catch {
    // No source errors, records, credentials or partial successful payload in logs.
    return json({ error: "source_read_incomplete", complete: false }, 502);
  }
});
