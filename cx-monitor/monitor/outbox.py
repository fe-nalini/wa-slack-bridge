"""Durable delivery of reviewed reports, independent of the lexical signal queue."""
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from psycopg.types.json import Jsonb
from . import store, slack_delivery, diagnostics

log = logging.getLogger('cx-outbox')

def validate_report(data):
    if not isinstance(data, dict):
        raise ValueError('invalid_report')
    key = data.get('report_key', '')
    text = data.get('text', '')
    evidence = data.get('evidence', [])
    reviewer = os.getenv('SLACK_ALLOWED_USER_ID', '')
    channel = os.getenv('SLACK_DESTINATION_CHANNEL', '')
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', key):
        raise ValueError('invalid_report_key')
    if not isinstance(text, str) or not text.strip() or len(text) > 3500:
        raise ValueError('invalid_report_text')
    if data.get('reviewed') is not True or not reviewer or data.get('reviewed_by') != reviewer:
        raise ValueError('owner_review_required')
    if not channel:
        raise ValueError('destination_not_configured')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 50:
        raise ValueError('evidence_required')
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {'source', 'reference'}:
            raise ValueError('invalid_evidence')
        if any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 500 for k in item):
            raise ValueError('invalid_evidence')
    now = datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(data['expires_at']) if 'expires_at' in data else now + timedelta(hours=24)
    except (ValueError, TypeError):
        raise ValueError('invalid_expiry') from None
    if expires.tzinfo is None or not now < expires <= now + timedelta(hours=24):
        raise ValueError('invalid_expiry')
    content = {'text': text, 'evidence': evidence, 'reviewed_by': reviewer, 'channel': channel}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return dict(content, report_key=key, content_hash=digest, expires_at=expires)

def enqueue(data):
    report = validate_report(data)
    with store.connect() as db:
        db.execute('''INSERT INTO report_outbox(report_key,content_hash,body,evidence,reviewed_by,channel,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(report_key) DO NOTHING''',
            (report['report_key'], report['content_hash'], report['text'], Jsonb(report['evidence']),
             report['reviewed_by'], report['channel'], report['expires_at']))
        row = db.execute('SELECT content_hash,state,slack_ts FROM report_outbox WHERE report_key=%s',
                         (report['report_key'],)).fetchone()
        if row['content_hash'] != report['content_hash']:
            raise ValueError('report_key_conflict')
    return {'report_key': report['report_key'], 'state': row['state'], 'slack_ts': row['slack_ts']}

def claim():
    with store.connect() as db:
        # A crash after POST may mean Slack accepted it. Never resend blindly.
        db.execute("""UPDATE report_outbox SET state='uncertain',last_error='interrupted_delivery',updated_at=now()
            WHERE state='sending' AND started_at < now()-interval '5 minutes'""")
        db.execute("""UPDATE report_outbox SET state='expired',updated_at=now()
            WHERE state IN ('queued','retry') AND expires_at <= now()""")
        return db.execute("""UPDATE report_outbox SET state='sending',attempts=attempts+1,
            started_at=now(),updated_at=now() WHERE report_key=(SELECT report_key FROM report_outbox
            WHERE state IN ('queued','retry') AND available_at <= now() AND expires_at > now() AND attempts < 3
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *""").fetchone()

def finish(row, state, code=None, slack_ts=None):
    with store.connect() as db:
        db.execute("""UPDATE report_outbox SET state=%s,last_error=%s,slack_ts=%s,
            available_at=now()+interval '5 minutes',updated_at=now()
            WHERE report_key=%s AND state='sending'""", (state, code, slack_ts, row['report_key']))

def process_once():
    if os.getenv('SLACK_PUBLISH_ENABLED', 'false') != 'true' or not os.getenv('SLACK_BOT_TOKEN'):
        return 'disabled'
    row = claim()
    if row is None:
        return 'empty'
    if row['channel'] != os.getenv('SLACK_DESTINATION_CHANNEL') or row['reviewed_by'] != os.getenv('SLACK_ALLOWED_USER_ID'):
        finish(row, 'blocked', 'destination_changed')
        return 'blocked'
    message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'reviewed-report:' + row['channel'] + ':' + row['report_key']))
    try:
        result = slack_delivery.deliver(row['body'], message_id)
    except slack_delivery.DeliveryUncertain:
        finish(row, 'uncertain', 'send_outcome_unknown')
        return 'uncertain'
    except ValueError as exc:
        code = str(exc)
        retry = code in {'slack_http_429', 'slack_ratelimited'} and row['attempts'] < 3
        state = 'retry' if retry else 'blocked'
        finish(row, state, code)
        return state
    except Exception:
        # Before/after-send distinction was not established; preserve for review.
        finish(row, 'uncertain', 'unexpected_delivery_error')
        return 'uncertain'
    if result.get('channel') != row['channel'] or not result.get('ts'):
        finish(row, 'uncertain', 'invalid_send_receipt')
        return 'uncertain'
    finish(row, 'sent', slack_ts=result['ts'])
    return 'sent'

def run(stop):
    result = slack_delivery.probe_destination()
    log.info('slack_preflight status=%s code=%s stage=%s', result['status'], result['code'], result['stage'])
    try:
        diagnostics.send_once()
    except Exception as exc:
        log.warning('technical_delivery_test_failed code=%s',type(exc).__name__)
    while not stop.is_set():
        try:
            for _ in range(10):
                state = process_once()
                if state in {'disabled', 'empty'}:
                    break
                log.info('report_delivery state=%s', state)
        except Exception as exc:
            log.warning('outbox_worker_failed code=%s', type(exc).__name__)
        stop.wait(30)

def start():
    stop = threading.Event()
    log.info('outbox_started publishing_enabled=%s credential_configured=%s',
             os.getenv('SLACK_PUBLISH_ENABLED','false')=='true', bool(os.getenv('SLACK_BOT_TOKEN')))
    threading.Thread(target=run, args=(stop,), daemon=True, name='cx-outbox').start()
    return stop
