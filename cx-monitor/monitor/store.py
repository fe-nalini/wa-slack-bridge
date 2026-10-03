import json
import os
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA = '''
CREATE TABLE IF NOT EXISTS instances (name text PRIMARY KEY, state text, updated_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS chats (instance text, jid text, subject text, kind text, classification text DEFAULT 'unclassified', source jsonb, updated_at timestamptz DEFAULT now(), PRIMARY KEY(instance,jid));
CREATE TABLE IF NOT EXISTS messages (instance text, chat text, mid text, sender text, from_me boolean, push_name text, ts bigint, body text, kind text, reply text, media jsonb, uncertain_identity boolean, raw jsonb, received_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), PRIMARY KEY(instance,chat,mid));
CREATE INDEX IF NOT EXISTS messages_chat_time ON messages(instance,chat,ts);
CREATE TABLE IF NOT EXISTS candidates (instance text, chat text, mid text, signal text, state text DEFAULT 'needs_review', created_at timestamptz DEFAULT now(), PRIMARY KEY(instance,chat,mid,signal));
CREATE TABLE IF NOT EXISTS checkpoints (instance text PRIMARY KEY, next_page integer DEFAULT 1, expected_total integer DEFAULT 0, scanned_at timestamptz, last_success timestamptz, last_error text, reconciliation text DEFAULT 'pending', inventory_error text);
CREATE TABLE IF NOT EXISTS provider_records (instance text, source_id text, chat text, mid text, exclusion text, PRIMARY KEY(instance,source_id));
CREATE TABLE IF NOT EXISTS slack_reports (channel text, ts text, author text, body text, thread_ts text, raw jsonb, imported_at timestamptz DEFAULT now(), PRIMARY KEY(channel,ts));
CREATE TABLE IF NOT EXISTS report_outbox (
 report_key text PRIMARY KEY,content_hash text NOT NULL,body text NOT NULL,evidence jsonb NOT NULL,
 reviewed_by text NOT NULL,channel text NOT NULL,state text NOT NULL DEFAULT 'queued',
 attempts integer NOT NULL DEFAULT 0,slack_ts text,last_error text,created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),started_at timestamptz,available_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL,
 CHECK(state IN ('queued','sending','sent','retry','blocked','uncertain','expired')));
CREATE INDEX IF NOT EXISTS report_outbox_pending ON report_outbox(state,available_at);
'''

def connect():
    return psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=10)

def initialize():
    with connect() as db:
        db.execute(SCHEMA)

def query(sql, args=()):
    # The consumer interface only invokes predefined SELECTs. Defense in depth:
    # every consumer transaction is enforced read-only by PostgreSQL.
    with connect() as db:
        db.execute('SET TRANSACTION READ ONLY')
        return db.execute(sql, args).fetchall()

def save_chat(db, instance, jid, subject=None, raw=None):
    db.execute('''INSERT INTO chats(instance,jid,subject,kind,source) VALUES(%s,%s,%s,%s,%s)
       ON CONFLICT(instance,jid) DO UPDATE SET subject=coalesce(EXCLUDED.subject,chats.subject),
       source=coalesce(EXCLUDED.source,chats.source),updated_at=now()
       WHERE (EXCLUDED.subject IS NOT NULL AND EXCLUDED.subject IS DISTINCT FROM chats.subject)
       OR (EXCLUDED.source IS NOT NULL AND EXCLUDED.source IS DISTINCT FROM chats.source)''',
       (instance,jid,subject,'group' if jid.endswith('@g.us') else 'private',Jsonb(raw) if raw else None))

def save_message(db, m, signals):
    save_chat(db,m['instance'],m['chat'])
    db.execute('''INSERT INTO messages(instance,chat,mid,sender,from_me,push_name,ts,body,kind,reply,media,uncertain_identity,raw)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(instance,chat,mid) DO UPDATE SET raw=EXCLUDED.raw,body=EXCLUDED.body,
        media=EXCLUDED.media,updated_at=now()
        WHERE messages.raw IS DISTINCT FROM EXCLUDED.raw OR messages.body IS DISTINCT FROM EXCLUDED.body
        OR messages.media IS DISTINCT FROM EXCLUDED.media''',
        (m['instance'],m['chat'],m['mid'],m['sender'],m['from_me'],m['push_name'],m['ts'],m['text'],
         m['kind'],m['reply'],Jsonb(m['media']) if m['media'] else None,m['uncertain_identity'],Jsonb(m['raw'])))
    for signal in signals:
        db.execute('INSERT INTO candidates(instance,chat,mid,signal) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                   (m['instance'],m['chat'],m['mid'],signal))
