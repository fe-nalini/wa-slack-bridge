"""Read-only dashboard snapshots. Never uses a browser session or source DB key."""
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from psycopg.types.json import Jsonb
from . import store, slas

log = logging.getLogger('cx-dashboard')
TZ = ZoneInfo('America/Sao_Paulo')
MEMBER_FIELDS = {'id','full_name','company_name','status','ca_id'}
STEP_FIELDS = {'id','member_id','step_number','step_name','status','phase','planned_at','completed_at','completed_by','is_optional'}

def configuration():
    url=os.getenv('DASHBOARD_READ_URL','')
    token=os.getenv('DASHBOARD_READ_TOKEN','')
    if not url or not token: raise ValueError('dashboard_read_configuration_missing')
    if not re.fullmatch(r'https://ohuzcsfqwzrrcbqaumrr\.supabase\.co/functions/v1/(?:cx-monitor-onboarding-read|bright-processor)',url):
        raise ValueError('dashboard_read_url_invalid')
    if len(token)<32: raise ValueError('dashboard_read_token_invalid')
    return url,token

def validate_snapshot(data,now):
    if not isinstance(data,dict) or data.get('schema_version')!=1 or data.get('complete') is not True:
        raise ValueError('dashboard_snapshot_incomplete')
    try:
        fetched=datetime.fromisoformat(data['fetched_at'].replace('Z','+00:00'))
        age=(now-fetched).total_seconds()
        if not 0<=age<=600: raise ValueError()
    except (ValueError,TypeError,KeyError,AttributeError):
        raise ValueError('dashboard_snapshot_time_invalid') from None
    members=data.get('members');steps=data.get('steps');coverage=data.get('coverage',{})
    if not isinstance(members,list) or not isinstance(steps,list) or not isinstance(coverage,dict):
        raise ValueError('dashboard_snapshot_invalid')
    if coverage.get('member_count')!=len(members) or coverage.get('step_count')!=len(steps):
        raise ValueError('dashboard_snapshot_count_mismatch')
    member_ids={str(m.get('id','')) for m in members if isinstance(m,dict)}
    step_ids={str(s.get('id','')) for s in steps if isinstance(s,dict)}
    if '' in member_ids or '' in step_ids or len(member_ids)!=len(members) or len(step_ids)!=len(steps):
        raise ValueError('dashboard_snapshot_identity_invalid')
    if any(str(s.get('member_id','')) not in member_ids for s in steps):
        raise ValueError('dashboard_snapshot_orphan_step')
    # Do not accept name/email-enriched Deal links as confirmed identity.
    return {'schema_version':1,'complete':True,'fetched_at':fetched.isoformat(),
        'coverage':coverage,'source':'Ponta de Lança / Atendimento Premium',
        'members':[{k:v for k,v in m.items() if k in MEMBER_FIELDS} for m in members],
        'steps':[{k:v for k,v in s.items() if k in STEP_FIELDS} for s in steps],
        'identity_note':'member_id é a chave da fonte. Vínculo WhatsApp/Deal não confirmado.',
        'consistency_note':'Leitura paginada, sem garantia de transação única entre tabelas.',
        'original_deadline_note':'A fonte não fornece o histórico da previsão original nesta integração.'}

def sync_once(now=None):
    try:
        url,token=configuration()
        response=requests.get(url,headers={'Authorization':'Bearer '+token},timeout=(10,60),allow_redirects=False)
        if response.status_code!=200: raise ValueError('dashboard_read_http_'+str(response.status_code))
        snapshot=validate_snapshot(response.json(),now or datetime.now(timezone.utc))
        with store.connect() as db:
            db.execute('''INSERT INTO dashboard_snapshots(fetched_at,payload) VALUES(%s,%s)
                ON CONFLICT(fetched_at) DO NOTHING''',(snapshot['fetched_at'],Jsonb(snapshot)))
            db.execute('''INSERT INTO dashboard_sync_health(source,last_success,last_error) VALUES('onboarding',now(),NULL)
                ON CONFLICT(source) DO UPDATE SET last_success=now(),last_error=NULL,updated_at=now()''')
        log.info('dashboard_sync status=ready members=%s steps=%s',len(snapshot['members']),len(snapshot['steps']))
        return 'ready'
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) else ''
        allowed={'dashboard_read_configuration_missing','dashboard_read_url_invalid','dashboard_read_token_invalid',
            'dashboard_snapshot_incomplete','dashboard_snapshot_invalid','dashboard_snapshot_time_invalid',
            'dashboard_snapshot_count_mismatch','dashboard_snapshot_identity_invalid','dashboard_snapshot_orphan_step',
            'dashboard_read_http_401','dashboard_read_http_403','dashboard_read_http_429','dashboard_read_http_502'}
        code=code if code in allowed else 'dashboard_read_unavailable'
        with store.connect() as db:
            db.execute('''INSERT INTO dashboard_sync_health(source,last_error) VALUES('onboarding',%s)
                ON CONFLICT(source) DO UPDATE SET last_error=EXCLUDED.last_error,updated_at=now()''',(code,))
        log.info('dashboard_sync status=blocked code=%s',code)
        return 'blocked'

def bundle_context(now):
    rows=store.query('SELECT fetched_at,payload FROM dashboard_snapshots ORDER BY fetched_at DESC LIMIT 1')
    if not rows: return {'loaded':False,'status':'missing_snapshot','sla_findings':[]}
    snapshot=rows[0]['payload'];fetched=rows[0]['fetched_at']
    if (now-fetched).total_seconds()>600:
        return {'loaded':False,'status':'stale_snapshot','fetched_at':fetched.isoformat(),'sla_findings':[]}
    today=now.astimezone(TZ).date().isoformat()
    findings=[]
    for step in snapshot['steps']:
        verdict=slas.classify(step.get('planned_at'),step.get('completed_at'),today)
        findings.append({'reference':{'member_id':step['member_id'],'step_id':step['id']},
            'step_number':step.get('step_number'),
            'step_name':slas.STEP_NAMES.get(str(step.get('step_number')),step.get('step_name')),
            'planned_at':step.get('planned_at'),'completed_at':step.get('completed_at'),
            'classification':verdict,'deadline_basis':'recorded_current_deadline',
            'original_deadline_verified':False,'cause_confirmed':False})
    return {'loaded':True,'status':'fresh_snapshot','snapshot':snapshot,'sla_findings':findings}
