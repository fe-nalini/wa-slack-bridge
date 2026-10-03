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
          'Respostas em threads Slack não estão cobertas por esta leitura de histórico.',
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

def sync_source(channel, stop):
    token = os.getenv('SLACK_BOT_TOKEN','')
    if not token:
        raise ValueError('slack_credential_missing')
    client = slack_delivery.Slack(token)
    oldest = datetime.now(timezone.utc) - timedelta(days=3)
    cursor = ''; complete = False; count = 0
    for _ in range(10):
        data = client.call('conversations.history', {'channel':channel, 'oldest':str(oldest.timestamp()),
                           'limit':100, 'cursor':cursor, 'inclusive':'true'})
        messages = data.get('messages')
        if not isinstance(messages, list):
            raise ValueError('invalid_history_response')
        with store.connect() as db:
            for m in messages:
                if not re.fullmatch(r'\d+\.\d+', str(m.get('ts',''))):
                    continue
                db.execute('''INSERT INTO slack_reports(channel,ts,author,body,thread_ts,raw)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(channel,ts) DO UPDATE SET
                    author=EXCLUDED.author,body=EXCLUDED.body,thread_ts=EXCLUDED.thread_ts,
                    raw=EXCLUDED.raw,imported_at=now()''',
                    (channel,m['ts'],m.get('user'),m.get('text',''),m.get('thread_ts'),Jsonb(scrub(m))))
                count += 1
        cursor = data.get('response_metadata',{}).get('next_cursor','')
        if not cursor and not data.get('has_more',False):
            complete = True; break
        if not cursor:
            break
        if stop.wait(1):
            break
    with store.connect() as db:
        db.execute('''INSERT INTO slack_source_health(channel,last_success,last_error,oldest_requested,history_complete)
            VALUES(%s,now(),NULL,%s,%s) ON CONFLICT(channel) DO UPDATE SET last_success=now(),
            last_error=NULL,oldest_requested=EXCLUDED.oldest_requested,
            history_complete=EXCLUDED.history_complete,updated_at=now()''', (channel,oldest,complete))
    log.info('source_sync channel=%s status=ready messages=%s history_complete=%s threads_complete=False', channel,count,complete)

def sync_sources(stop):
    for channel in configured_sources():
        try:
            sync_source(channel, stop)
        except Exception as exc:
            # Never include remote error bodies or arbitrary exception strings.
            code = str(exc) if isinstance(exc,ValueError) else ''
            allowed = {'slack_missing_scope','slack_not_in_channel','slack_channel_not_found',
                       'slack_invalid_auth','slack_token_revoked','slack_http_429','slack_ratelimited',
                       'slack_credential_missing','invalid_history_response'}
            code = code if code in allowed else 'source_read_unavailable'
            with store.connect() as db:
                db.execute('''INSERT INTO slack_source_health(channel,last_error) VALUES(%s,%s)
                    ON CONFLICT(channel) DO UPDATE SET last_error=EXCLUDED.last_error,
                    history_complete=false,updated_at=now()''', (channel,code))
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
    source_rows = store.query('''SELECT channel,ts,body,raw FROM slack_reports WHERE channel=ANY(%s)
        AND ts::numeric >= %s AND ts::numeric <= %s ORDER BY ts::numeric DESC LIMIT 201''',
        (configured_sources(),start,end))
    slack_cases = []
    for row in source_rows[:200]:
        signals = candidate(row['body'])
        # Source reports remain accessible even when no keyword matches.
        slack_cases.append({'source':'slack','channel':row['channel'],'stream':SOURCES[row['channel']],
            'reference':row['ts'],'signals':signals,'excerpt':safe_text(row['body']),
            'reply_count':(row['raw'] or {}).get('reply_count',0),'threads_complete':False,
            'classification':'source_statement_requires_crosscheck'})
    return json_ready({'version':1,'generated_at':now,'window_start':datetime.fromtimestamp(start,timezone.utc),
        'window_end':now,'kind':'evidence_triage','requires_review':True,'published':False,
        'whatsapp_cases':cases,'whatsapp_cases_truncated':len(rows)>100,
        'slack_source_statements':slack_cases,'slack_statements_truncated':len(source_rows)>200,
        'capture_health':store.query('SELECT * FROM checkpoints'),
        'slack_health':store.query('SELECT * FROM slack_source_health'),
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
