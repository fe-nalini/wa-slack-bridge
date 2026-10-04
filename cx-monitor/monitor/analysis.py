"""Recurring evidence preparation. No LLM, inferred SLA, identity match or publication."""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from psycopg.types.json import Jsonb
from . import store, slack_delivery
from .core import scrub, candidate

log = logging.getLogger('cx-analysis')
TZ = ZoneInfo('America/Sao_Paulo')
SOURCES = {'C0BNDFL2PC7': 'Club', 'C05DTS89QTU': 'Reclame Aqui / Procon', 'C0AF1TU73PU': 'G4 OS'}
LIMITS = ['Triagem por palavras; exige leitura e revisão do contexto, não confirma causa ou falha.',
          'Grupos e pessoas do WhatsApp ainda não estão vinculados ao Deal por ID confirmado.',
          'Histórico recuperável não comprova ausência de atendimento ou mensagens excluídas.',
          'Cobertura das threads é indicada por conversa; o histórico de descoberta é limitado às últimas 72 horas e registros já importados.',
          'Dashboard não tem ingestão contínua autenticada; Desk será integrado por último.',
          'Áudios/anexos não foram transcritos. Nenhum candidato é publicado automaticamente.']

def configured_sources():
    supplied = {x.strip() for x in os.getenv('SLACK_MONITOR_SOURCE_CHANNELS','').split(',') if x.strip()}
    if supplied - set(SOURCES):
        raise ValueError('unapproved_source_channel')
    return sorted(supplied)

def safe_text(value, limit=1600):
    value = re.sub(r'xox[baprs]-[A-Za-z0-9-]+', '[credencial ocultada]', str(value or ''))
    return {'text': value[:limit], 'truncated': len(value) > limit}

def json_ready(value):
    if isinstance(value, (datetime, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {k: json_ready(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    return value

def save_slack_messages(channel, messages, thread_ts=None):
    with store.connect() as db:
        for m in messages:
            if not re.fullmatch(r'\d+\.\d+', str(m.get('ts',''))):
                raise ValueError('invalid_slack_timestamp')
            if thread_ts and m.get('thread_ts', thread_ts) != thread_ts:
                raise ValueError('invalid_thread_response')
            db.execute('''INSERT INTO slack_reports(channel,ts,author,body,thread_ts,raw)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(channel,ts) DO UPDATE SET
                author=EXCLUDED.author,body=EXCLUDED.body,thread_ts=EXCLUDED.thread_ts,
                raw=EXCLUDED.raw,imported_at=now()''',
                (channel,m['ts'],m.get('user'),m.get('text',''),
                 m.get('thread_ts') or thread_ts,Jsonb(scrub(m))))

READ_ERRORS = {'slack_missing_scope','slack_not_in_channel','slack_channel_not_found',
               'slack_invalid_auth','slack_token_revoked','slack_http_429','slack_ratelimited',
               'slack_thread_not_found','slack_not_allowed_token_type',
               'slack_credential_missing','invalid_history_response','invalid_thread_response',
               'invalid_slack_timestamp'}

def read_error(exc):
    code = str(exc) if isinstance(exc,ValueError) else ''
    return code if code in READ_ERRORS else 'source_read_unavailable'

def sync_thread(client, channel, parent, stop):
    cursor = ''; complete = False; count = 0; root = None
    for _ in range(10):
        if stop.is_set(): break
        data = client.call('conversations.replies', {'channel':channel,'ts':parent,'limit':100,'cursor':cursor})
        messages = data.get('messages')
        if not isinstance(messages,list) or (not cursor and not any(m.get('ts')==parent for m in messages)):
            raise ValueError('invalid_thread_response')
        for m in messages:
            if m.get('ts')==parent: root=m
        save_slack_messages(channel,messages,parent)
        count += sum(m['ts']!=parent for m in messages)
        cursor = data.get('response_metadata',{}).get('next_cursor','')
        if not cursor and not data.get('has_more',False):
            complete=True; break
        if not cursor or stop.wait(1): break
    # A changed or deleted reply is not evidence of a completely reconciled thread.
    expected = (root or {}).get('reply_count')
    complete = complete and isinstance(expected,int) and count==expected
    with store.connect() as db:
        db.execute('''INSERT INTO slack_thread_health(channel,thread_ts,complete,observed_reply_count,
            observed_latest_reply,last_success,last_error) VALUES(%s,%s,%s,%s,%s,now(),NULL)
            ON CONFLICT(channel,thread_ts) DO UPDATE SET complete=EXCLUDED.complete,
            observed_reply_count=EXCLUDED.observed_reply_count,observed_latest_reply=EXCLUDED.observed_latest_reply,
            last_success=now(),last_error=NULL,updated_at=now()''',
            (channel,parent,complete,expected,(root or {}).get('latest_reply')))
    log.info('thread_sync channel=%s parent=%s replies=%s complete=%s',channel,parent,count,complete)
    return complete

def sync_threads(client, channel, stop):
    # Revisit all known parents, including older imported reports. Bounded work
    # progresses across cycles using persisted completion and refresh times.
    parents = store.query('''SELECT r.ts FROM slack_reports r LEFT JOIN slack_thread_health h
        ON h.channel=r.channel AND h.thread_ts=r.ts WHERE r.channel=%s
        AND coalesce((r.raw->>'reply_count')::integer,0)>0
        AND (r.thread_ts IS NULL OR r.thread_ts=r.ts)
        AND (h.complete IS DISTINCT FROM true OR h.observed_reply_count IS DISTINCT FROM (r.raw->>'reply_count')::integer
          OR h.observed_latest_reply IS DISTINCT FROM r.raw->>'latest_reply'
          OR h.last_success < now()-interval '24 hours')
        ORDER BY h.last_success ASC NULLS FIRST,r.ts::numeric DESC LIMIT 20''',(channel,))
    blocked=False
    for row in parents:
        if stop.is_set(): break
        try:
            sync_thread(client,channel,row['ts'],stop)
        except Exception as exc:
            code=read_error(exc); blocked=True
            with store.connect() as db:
                db.execute('''INSERT INTO slack_thread_health(channel,thread_ts,complete,last_error)
                    VALUES(%s,%s,false,%s) ON CONFLICT(channel,thread_ts) DO UPDATE SET
                    complete=false,last_error=EXCLUDED.last_error,updated_at=now()''',(channel,row['ts'],code))
            log.info('thread_sync channel=%s parent=%s status=blocked code=%s',channel,row['ts'],code)
            # Scope/auth/rate errors affect the whole batch; do not hammer Slack.
            if code!='slack_thread_not_found': break
        if stop.wait(1): break
    missing=store.query('''SELECT count(*) AS pending FROM slack_reports r LEFT JOIN slack_thread_health h
        ON h.channel=r.channel AND h.thread_ts=r.ts WHERE r.channel=%s
        AND coalesce((r.raw->>'reply_count')::integer,0)>0 AND (r.thread_ts IS NULL OR r.thread_ts=r.ts)
        AND (h.complete IS DISTINCT FROM true OR h.observed_reply_count IS DISTINCT FROM (r.raw->>'reply_count')::integer
          OR h.observed_latest_reply IS DISTINCT FROM r.raw->>'latest_reply'
          OR h.last_success < now()-interval '24 hours')''',(channel,))[0]['pending']
    return not blocked and missing==0

def sync_source(channel, stop):
    token = os.getenv('SLACK_BOT_TOKEN','')
    if not token: raise ValueError('slack_credential_missing')
    client = slack_delivery.Slack(token)
    oldest = datetime.now(timezone.utc) - timedelta(days=3)
    cursor = ''; complete = False; count = 0
    for _ in range(10):
        data = client.call('conversations.history', {'channel':channel, 'oldest':str(oldest.timestamp()),
                           'limit':100, 'cursor':cursor, 'inclusive':'true'})
        messages = data.get('messages')
        if not isinstance(messages, list): raise ValueError('invalid_history_response')
        save_slack_messages(channel,messages); count+=len(messages)
        cursor = data.get('response_metadata',{}).get('next_cursor','')
        if not cursor and not data.get('has_more',False):
            complete = True; break
        if not cursor or stop.wait(1): break
    threads_complete = sync_threads(client,channel,stop)
    with store.connect() as db:
        db.execute('''INSERT INTO slack_source_health(channel,last_success,last_error,oldest_requested,history_complete,threads_complete)
            VALUES(%s,now(),NULL,%s,%s,%s) ON CONFLICT(channel) DO UPDATE SET last_success=now(),
            last_error=NULL,oldest_requested=EXCLUDED.oldest_requested,
            history_complete=EXCLUDED.history_complete,threads_complete=EXCLUDED.threads_complete,updated_at=now()''',
            (channel,oldest,complete,threads_complete))
    log.info('source_sync channel=%s status=ready messages=%s history_complete=%s threads_complete=%s',channel,count,complete,threads_complete)

def sync_sources(stop):
    for channel in configured_sources():
        try:
            sync_source(channel, stop)
        except Exception as exc:
            # Never include remote error bodies or arbitrary exception strings.
            code = read_error(exc)
            with store.connect() as db:
                db.execute('''INSERT INTO slack_source_health(channel,last_error) VALUES(%s,%s)
                    ON CONFLICT(channel) DO UPDATE SET last_error=EXCLUDED.last_error,
                    history_complete=false,threads_complete=false,updated_at=now()''', (channel,code))
            log.info('source_sync channel=%s status=blocked code=%s',channel,code)

def build_bundle(now):
    end = int(now.timestamp()); start = end - 72*3600
    rows = store.query('''SELECT m.instance,m.chat,m.mid,m.ts,m.body,m.sender,m.from_me,c.subject,
        array_agg(DISTINCT r.signal ORDER BY r.signal) AS signals FROM candidates r
        JOIN messages m USING(instance,chat,mid) LEFT JOIN chats c ON c.instance=m.instance AND c.jid=m.chat
        WHERE m.ts >= %s AND m.ts <= %s GROUP BY m.instance,m.chat,m.mid,c.subject
        ORDER BY m.ts DESC,m.mid LIMIT 101''',(start,end))
    cases = []
    for row in rows[:100]:
        context = store.query('''(SELECT mid,ts,body,reply,from_me,media FROM messages
            WHERE instance=%s AND chat=%s AND ts <= %s ORDER BY ts DESC,mid DESC LIMIT 8)
            UNION (SELECT mid,ts,body,reply,from_me,media FROM messages WHERE instance=%s AND chat=%s
            AND ts > %s AND ts <= %s ORDER BY ts,mid LIMIT 8) ORDER BY ts,mid''',
            (row['instance'],row['chat'],row['ts'],row['instance'],row['chat'],row['ts'],end))
        cases.append({'source':'whatsapp','reference':{'instance':row['instance'],'chat':row['chat'],'mid':row['mid']},
                      'subject':row['subject'],'signals':row['signals'],'timestamp':row['ts'],
                      'excerpt':safe_text(row['body']),'confirmed_deal_id':None,'classification':'needs_context_review',
                      'context_is_excerpt':True,'context':[dict(m,body=safe_text(m['body'],800)) for m in context]})
    source_rows = store.query('''SELECT channel,ts,body,thread_ts,raw FROM slack_reports WHERE channel=ANY(%s)
        AND ts::numeric >= %s AND ts::numeric <= %s ORDER BY ts::numeric DESC LIMIT 201''',
        (configured_sources(),start,end))
    slack_cases = []
    thread_health = store.query('SELECT * FROM slack_thread_health WHERE channel=ANY(%s)',(configured_sources(),))
    health_by_parent = {(h['channel'],h['thread_ts']):h for h in thread_health}
    for row in source_rows[:200]:
        signals = candidate(row['body'])
        # Source reports remain accessible even when no keyword matches.
        parent=row.get('thread_ts') or row['ts']
        health=health_by_parent.get((row['channel'],parent),{})
        context=store.query('''SELECT ts,author,body,raw FROM slack_reports WHERE channel=%s
            AND (ts=%s OR thread_ts=%s) ORDER BY ts::numeric LIMIT 101''',(row['channel'],parent,parent))
        root=next((m for m in context if m['ts']==parent),{})
        root_raw=root.get('raw') or {}
        last_success=health.get('last_success')
        thread_complete=(health.get('complete',False) and last_success is not None
            and (now-last_success).total_seconds()<86400
            and health.get('observed_reply_count')==root_raw.get('reply_count')
            and health.get('observed_latest_reply')==root_raw.get('latest_reply'))
        slack_cases.append({'source':'slack','channel':row['channel'],'stream':SOURCES[row['channel']],
            'reference':row['ts'],'signals':signals,'excerpt':safe_text(row['body']),
            'reply_count':(row['raw'] or {}).get('reply_count',0),
            'thread_ts':parent,'threads_complete':thread_complete,
            'thread_context_truncated':len(context)>100,
            'thread_context':[{'ts':m['ts'],'author':m['author'],'body':safe_text(m['body'])} for m in context[:100]],
            'classification':'source_statement_requires_crosscheck'})
    return json_ready({'version':2,'generated_at':now,'window_start':datetime.fromtimestamp(start,timezone.utc),
        'window_end':now,'kind':'evidence_triage','requires_review':True,'published':False,
        'whatsapp_cases':cases,'whatsapp_cases_truncated':len(rows)>100,
        'slack_source_statements':slack_cases,'slack_statements_truncated':len(source_rows)>200,
        'capture_health':store.query('SELECT * FROM checkpoints'),
        'slack_health':store.query('SELECT * FROM slack_source_health'),'thread_health':thread_health,
        'source_limits':LIMITS,'sla_rules':{'0':'forms de handoff, preenchido pelo vendedor',
            '7':'forms do Club, preenchido pelo membro','overdue':'Prevista vencida e sem conclusão.',
            'completed_late':'Conclusão posterior à previsão original; concluir não apaga atraso.',
            'blame':'Atribuição de causa requer evidência separada.','dashboard_snapshot_loaded':False}})

def generate_once(now=None):
    now = now or datetime.now(timezone.utc)
    key = 'triage:' + now.strftime('%Y%m%dT%H')
    # Hold one short-lived advisory lock across generation to serialize replica/restart races.
    with store.connect() as db:
        locked = db.execute("SELECT pg_try_advisory_xact_lock(783610321) AS locked").fetchone()['locked']
        if not locked or db.execute('SELECT run_key FROM analysis_runs WHERE run_key=%s',(key,)).fetchone():
            return 'already_generated'
        bundle = build_bundle(now)
        db.execute("INSERT INTO analysis_runs(run_key,kind,payload) VALUES(%s,'evidence_triage',%s)", (key,Jsonb(bundle)))
        local = now.astimezone(TZ)
        if local.weekday()<5 and local.hour in {9,15}:
            digest_key = 'digest:' + local.strftime('%Y%m%dT%H')
            db.execute("INSERT INTO analysis_runs(run_key,kind,payload) VALUES(%s,'scheduled_review_dossier',%s) ON CONFLICT DO NOTHING",
                       (digest_key,Jsonb(bundle)))
    log.info('analysis_generated kind=evidence_triage whatsapp_cases=%s slack_statements=%s requires_review=True',
             len(bundle['whatsapp_cases']),len(bundle['slack_source_statements']))
    return 'generated'

def run(stop):
    last_source_sync = None
    while not stop.is_set():
        try:
            now = datetime.now(timezone.utc)
            if last_source_sync is None or (now-last_source_sync).total_seconds()>=300:
                sync_sources(stop); last_source_sync=now
            generate_once(now)
        except Exception as exc:
            log.warning('analysis_worker_failed code=%s',type(exc).__name__)
        stop.wait(60)

def start():
    stop = threading.Event()
    if os.getenv('ANALYSIS_ENABLED','false')=='true':
        threading.Thread(target=run,args=(stop,),daemon=True,name='cx-analysis').start()
        log.info('analysis_started cadence=hourly review_dossiers=weekdays_09_15_BRT publishing=False')
    return stop
