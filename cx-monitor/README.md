# Conversation monitor

Private channel identifiers, approved identities and source selection belong in
protected deployment variables. No customer or organizational data is in this repository.
Sources remain read-only. Existing bridges and webhooks are unchanged.

This first implementation polls Evolution 2.3.7, inventories accessible chats,
imports recoverable messages with deduplication, checkpoints and reconciliation,
and exposes six read-only MCP tools at `/mcp`. No send/mark-read/delete endpoints
exist. PostgreSQL enforces read-only transactions for consumer queries. Separate
tokens protect queries and the internal import of source Slack reports.

Run `python -m unittest discover -s cx-monitor/tests` with `PYTHONPATH=cx-monitor`.
Build from repository root: `docker build -f cx-monitor/Dockerfile .`.
One instance/replica is required for the embedded polling worker.

Required variables: `DATABASE_URL`, `EVOLUTION_URL`, `EVOLUTION_API_KEY`,
`QUERY_TOKEN`, `INGEST_TOKEN`, `EVOLUTION_INSTANCE` (exact approved instance name),
`SLACK_SOURCE_CHANNEL` (approved source ID, never committed).
Tokens and keys are only in Railway variables, never git.
Use `POLL_SECONDS=300`, `BACKFILL_PAGES_PER_CYCLE=20` initially.

## Known limitations / activation gates

- No claim of history before joining, deleted messages, or provider omissions.
- Polling does not guarantee all edits/deletes; authenticated event capture is a later activation gate.
- Repeated full scans reconcile shifted offsets. Counts match only current recoverable records.
- Groups without messages stay visible; classification and staff/customer identity require validation.
- Media metadata is preserved, but downloads/transcripts are not implemented.
- Candidate signals are lexical and require contextual review. They are not confirmed failures or automated causal diagnosis.
- Automatic Slack delivery and continuous report import require a dedicated Slack app credential, channel membership and privacy validation before each publish. Existing webhooks must not be reused for a new destination.
- Backups/restore, retention, alert calibration, automated report comparison and MCP account registration are pending. This is a data foundation, not completion of all architectural acceptance criteria.
- `/health` is liveness only; `ingestion_health` supplies capture health and gaps.

Rollback: stop/remove only the new monitor service. Original Evolution, bridge,
Postgres and Redis configurations are untouched. Preserve the pilot database for
evidence; do not delete it as a rollback shortcut.

## Deployment

Select `cx-monitor/Dockerfile` as the Dockerfile. Set the service start command to
`uvicorn monitor.app:app --host 0.0.0.0 --port 8080 --no-access-log`.
Keep the PORT variable and public routing aligned at 8080.

## Private delivery adapter

The delivery adapter verifies a private, unshared destination containing only
the configured owner and the authenticated app before sending. Additional
tests cover unexpected humans/bots, public/shared channels and a missing owner.
It is disabled by default. A separate PostgreSQL outbox now accepts explicitly
owner-reviewed reports via `/internal/reviewed-reports`, protected by the ingest
token (the query token cannot write). Each report needs a stable `report_key`,
`text` (up to 3500 characters), `reviewed: true`, `reviewed_by` matching the
configured owner, and an `evidence` list of `{source, reference}` objects.
Optional timezone-aware `expires_at` must be within 24 hours; the default is 24
hours. This endpoint is not an MCP tool. Lexical candidates never enter this
queue automatically, and submitted fields do not replace actual owner review.

The outbox binds the reviewed content and destination, rejects changed content
under the same key, claims rows transactionally with `SKIP LOCKED`, expires stale
queued reports, and preserves delivery receipts. Explicit rate limits retry at
most three times. A timeout, uncertain receipt or interrupted in-flight send is
held as `uncertain` for manual reconciliation instead of blindly resent. This
avoids claiming exactly-once delivery from Slack client-message IDs alone.

Activation still requires the dedicated Slack app token in `SLACK_BOT_TOKEN`,
validated private-channel membership, and `SLACK_PUBLISH_ENABLED=true`. Required
read scopes support destination/member verification; posting needs `chat:write`.
Only configure credentials securely, never in source or chat. Do not turn it on
until queued reports have been reviewed. Scheduling, semantic report generation,
continuous source import and account MCP registration remain activation gates.
`ingestion_health` now exposes outbox states and source-import freshness.
