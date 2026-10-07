"""Read-only dashboard snapshots. Never uses a browser session or source DB key."""
import logging
import os
import re
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
import requests
from psycopg.types.json import Jsonb
from . import store, slas

log = logging.getLogger('cx-dashboard')
TZ = ZoneInfo('America/Sao_Paulo')
MEMBER_FIELDS = {'id','full_name','company_name','status','ca_id','sale_date','email','whatsapp','deal_id','deal_id_source'}
STEP_FIELDS = {'id','member_id','step_number','step_name','status','phase','planned_at','completed_at','completed_by','is_optional','sla_days','note','call_link','started_at'}
TEMPLATE_FIELDS = {'step_number','step_name','phase','sla_days','sla_base','is_active'}

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
    members=data.get('members');steps=data.get('steps');coverage=data.get('coverage',{});templates=data.get('templates',[])
    if not isinstance(members,list) or not isinstance(steps,list) or not isinstance(templates,list) or not isinstance(coverage,dict):
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
        'templates':[{k:v for k,v in t.items() if k in TEMPLATE_FIELDS} for t in templates],
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

def local_date(value):
    """Use the calendar date shown in São Paulo; date-only stays unchanged."""
    if not value or not isinstance(value,str): return None
    try:
        if len(value)==10: return date.fromisoformat(value)
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return (parsed.astimezone(TZ) if parsed.tzinfo else parsed).date()
    except ValueError:
        return None

def display_deadlines(member,steps,templates):
    """Mirror dashboard computeDeadline; inferred display date is not audited original."""
    by_number={str(s.get('step_number')):s for s in steps}
    tmpl={str(t.get('step_number')):t for t in templates if t.get('is_active')}
    sale=local_date(member.get('sale_date'))
    result={}
    for step in steps:
        number=str(step.get('step_number'))
        if step.get('planned_at'):
            result[step['id']]=(local_date(step['planned_at']),'recorded_current_deadline')
            continue
        days=step.get('sla_days')
        if not isinstance(days,int) or days<=0:
            result[step['id']]=(None,'not_computable');continue
        base=str(tmpl.get(number,{}).get('sla_base') or ('sale_date' if int(number)<=4 else 'step_completion:4'))
        if base=='sale_date':
            start=sale
        elif base.startswith('step_completion:') and base.split(':',1)[1].isdigit():
            ref=by_number.get(base.split(':',1)[1],{})
            start=local_date(ref.get('completed_at') or ref.get('planned_at'))
        else:
            start=sale
        if start is None:
            result[step['id']]=(None,'not_computable');continue
        current=start;remaining=days
        while remaining:
            current+=timedelta(days=1)
            if current.weekday()<5: remaining-=1
        result[step['id']]=(current,'computed_from_sla_not_original')
    return result

def bundle_context(now):
    rows=store.query('SELECT fetched_at,payload FROM dashboard_snapshots ORDER BY fetched_at DESC LIMIT 1')
    if not rows: return {'loaded':False,'status':'missing_snapshot','sla_findings':[]}
    snapshot=rows[0]['payload'];fetched=rows[0]['fetched_at']
    if (now-fetched).total_seconds()>600:
        return {'loaded':False,'status':'stale_snapshot','fetched_at':fetched.isoformat(),'sla_findings':[]}
    today=now.astimezone(TZ).date().isoformat()
    findings=[]
    member_by_id={m['id']:m for m in snapshot['members']}
    steps_by_member={}
    for step in snapshot['steps']:
        steps_by_member.setdefault(step['member_id'],[]).append(step)
    for member_id,steps in steps_by_member.items():
        deadlines=display_deadlines(member_by_id[member_id],steps,snapshot.get('templates',[]))
        for step in steps:
            due,basis=deadlines[step['id']]
            completed=local_date(step.get('completed_at'))
            verdict=slas.classify(due.isoformat() if due else None,
                                  completed.isoformat() if completed else None,today)
            findings.append({'reference':{'member_id':step['member_id'],'step_id':step['id']},
                'step_number':step.get('step_number'),
                'step_name':slas.STEP_NAMES.get(str(step.get('step_number')),step.get('step_name')),
                'planned_at':step.get('planned_at'),'display_deadline':due.isoformat() if due else None,
                'completed_at':step.get('completed_at'),'source_optional_label':step.get('is_optional'),'required_for_club':True,
                'note':step.get('note'),'call_link':step.get('call_link'),
                'classification':verdict,'deadline_basis':basis,
                'original_deadline_verified':False,'cause_confirmed':False})
    return {'loaded':True,'status':'fresh_snapshot','snapshot':snapshot,'sla_findings':findings}

