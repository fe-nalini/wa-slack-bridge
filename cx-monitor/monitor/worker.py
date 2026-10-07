import logging
import hashlib
import json
import os
import threading
import time
from urllib.parse import quote
import requests
from .core import normalize, candidate, parse_page, scrub
from . import store

log = logging.getLogger('cx')

class Evolution:
    def __init__(self):
        self.base = os.environ['EVOLUTION_URL'].rstrip('/')
        self.session = requests.Session()
        self.session.headers['apikey'] = os.environ['EVOLUTION_API_KEY']
    def read(self, path, body=None):
        # Strict allowlist; POST here performs database queries, never WhatsApp actions.
        allowed = ('/instance/fetchInstances','/instance/connectionState/', '/group/fetchAllGroups/', '/chat/findChats/', '/chat/findMessages/')
        if not any(path == p or (p.endswith('/') and path.startswith(p)) for p in allowed):
            raise ValueError('endpoint_not_allowed')
        r = self.session.get(self.base+path,timeout=(10,90)) if body is None else self.session.post(self.base+path,json=body,timeout=(10,90))
        if not r.ok:
            raise ValueError('evolution_http_'+str(r.status_code))
        return r.json()

def instances_from(data):
    if isinstance(data, dict):
        data = data.get('instances', [])
    if not isinstance(data, list):
        raise ValueError('unsupported_instances_envelope')
    out=[]
    for item in data:
        i=item.get('instance',item)
        name=i.get('name') or i.get('instanceName')
        if name:
            out.append(name)
    return out

def save_page(client, instance, page):
    records,pages,total=parse_page(client.read('/chat/findMessages/'+quote(instance,safe=''), {'page':page,'offset':200,'where':{}}),page)
    with store.connect() as db:
        for record in records:
            m=normalize(instance,record)
            source_id=str(record.get('id') or hashlib.sha256(json.dumps(scrub(record),sort_keys=True).encode()).hexdigest())
            db.execute('INSERT INTO provider_records(instance,source_id,chat,mid,exclusion) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (instance,source_id,m['chat'] if m else None,m['mid'] if m else None,None if m else 'status_or_missing_chat'))
            if m:
                store.save_message(db,m,candidate(m['text']))
    return len(records),pages,total

def sync_instance(client, instance):
    encoded=quote(instance,safe='')
    connection=client.read('/instance/connectionState/'+encoded)
    i=connection.get('instance',connection)
    state=i.get('state') or i.get('connectionStatus') or 'unknown'
    with store.connect() as db:
        db.execute('INSERT INTO instances(name,state) VALUES(%s,%s) ON CONFLICT(name) DO UPDATE SET state=EXCLUDED.state,updated_at=now()', (instance,state))
        db.execute('INSERT INTO checkpoints(instance) VALUES(%s) ON CONFLICT DO NOTHING',(instance,))
    inventory_error=None
    try:
        groups=client.read('/group/fetchAllGroups/'+encoded+'?getParticipants=false')
        if not isinstance(groups,list):
            raise ValueError('unsupported_groups_envelope')
        with store.connect() as db:
            for g in groups:
                if g.get('id'):
                    store.save_chat(db,instance,g['id'],g.get('subject'),scrub(g))
        chats=client.read('/chat/findChats/'+encoded,{})
        if not isinstance(chats,list):
            raise ValueError('unsupported_chats_envelope')
        with store.connect() as db:
            for c in chats:
                jid=c.get('remoteJid') or c.get('id')
                if jid and '@' in jid:
                    store.save_chat(db,instance,jid,c.get('name') or c.get('pushName'),scrub(c))
    except Exception as exc:
        inventory_error=type(exc).__name__+':'+str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    # A live sweep and a checkpointed complete sweep run side by side. Absence
    # is never inferred from the live window. Offset shifts are repaired by repeats.
    n,pages,total=save_page(client,instance,1)
    for page in range(2,min(pages,3)+1):
        save_page(client,instance,page)
        time.sleep(.25)
    cp=store.query('SELECT * FROM checkpoints WHERE instance=%s',(instance,))[0]
    page=cp['next_page']
    if page>max(pages,1):
        page=1
    finished=False
    for _ in range(int(os.getenv('BACKFILL_PAGES_PER_CYCLE','20'))):
        n,pages,total=save_page(client,instance,page)
        page+=1
        if not n or page>pages:
            finished=True
            page=1
            break
        time.sleep(.25)
    with store.connect() as db:
        # counts are recoverable records, not proof of pre-entry or deleted history
        local=db.execute('SELECT count(*) AS n FROM messages WHERE instance=%s',(instance,)).fetchone()['n']
        source_count=db.execute('SELECT count(*) AS n FROM provider_records WHERE instance=%s',(instance,)).fetchone()['n']
        status='scanning'
        if finished:
            status='scanned_counts_match' if source_count==total else 'scanned_counts_differ'
        db.execute('''UPDATE checkpoints SET next_page=%s,expected_total=%s,last_success=now(),last_error=NULL,
           reconciliation=%s,inventory_error=%s,scanned_at=CASE WHEN %s THEN now() ELSE scanned_at END WHERE instance=%s''',
           (page,total,status,inventory_error,finished,instance))
    log.info('sync_complete messages=%s expected_records=%s inventory_ok=%s state=%s',local,total,inventory_error is None,state)

def run(stop):
    failures=0
    while not stop.is_set():
        try:
            store.initialize()
            client=Evolution()
            discovered=instances_from(client.read('/instance/fetchInstances'))
            allowed=os.getenv('EVOLUTION_INSTANCE','')
            names=[n for n in discovered if n==allowed] if allowed else []
            with store.connect() as db:
                for n in discovered:
                    db.execute('INSERT INTO instances(name,state) VALUES(%s,%s) ON CONFLICT(name) DO NOTHING',(n,'not_selected'))
            log.info('instances_discovered count=%s selected=%s',len(discovered),len(names))
            if not names:
                log.warning('capture_blocked code=approved_instance_not_selected')
            for instance in names:
                try:
                    sync_instance(client,instance)
                except Exception as exc:
                    code=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                    with store.connect() as db:
                        db.execute('UPDATE checkpoints SET last_error=%s WHERE instance=%s',(code,instance))
                    log.warning('instance_sync_failed code=%s',code)
            failures=0
        except Exception as exc:
            failures+=1
            log.warning('worker_failed code=%s',type(exc).__name__)
        stop.wait(min(900,int(os.getenv('POLL_SECONDS','300'))*max(1,failures)))

def start():
    stop=threading.Event()
    threading.Thread(target=run,args=(stop,),daemon=True,name='cx-sync').start()
    return stop
