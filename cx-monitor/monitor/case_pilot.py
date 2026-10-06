"""One-time, evidence-only Tatiane pilot in the exclusive private channel."""
import logging
import os
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from . import store, slack_delivery, dashboard, slas

log = logging.getLogger('cx-case-pilot')
TZ = ZoneInfo('America/Sao_Paulo')
RUN_KEY = 'tatiane-65511973694-audit-v2'
NAME = 'Tatiane Arruda'
DEAL_ID = '65511973694'

def build():
    rows = store.query('SELECT fetched_at,payload FROM dashboard_snapshots ORDER BY fetched_at DESC LIMIT 1')
    if not rows:
        return None, 'snapshot_missing'
    snap = rows[0]['payload']
    members = [m for m in snap['members'] if NAME.casefold() in str(m.get('full_name','')).casefold()]
    if len(members) != 1:
        return None, 'member_ambiguous_or_missing'
    member = members[0]
    chats = store.query("SELECT instance,jid,subject FROM chats WHERE kind='group' AND subject ILIKE %s", ('%Tatiane Arruda%',))
    if len(chats) != 1:
        return None, 'group_ambiguous_or_missing'
    chat = chats[0]
    messages = store.query('''SELECT mid,ts,body,sender,from_me,kind,media FROM messages
        WHERE instance=%s AND chat=%s AND ts >= %s AND ts <= %s ORDER BY ts,mid''',
        (chat['instance'],chat['jid'],int(datetime(2026,9,30,tzinfo=TZ).timestamp()),int(datetime.now(timezone.utc).timestamp())))
    if not messages:
        return None, 'group_messages_missing'
    steps = [s for s in snap['steps'] if s['member_id'] == member['id']]
    reports = store.query('''SELECT ts,body FROM slack_reports WHERE channel=%s AND body ILIKE %s
        AND ts::numeric >= %s ORDER BY ts::numeric''',
        ('C0BNDFL2PC7','%Tatiane Arruda%',int(datetime(2026,9,30,tzinfo=TZ).timestamp())))
    def when(ts):
        return datetime.fromtimestamp(float(ts),timezone.utc).astimezone(TZ).strftime('%d/%m %H:%M')
    dates=dashboard.display_deadlines(member,steps,snap.get('templates',[]))
    today=datetime.now(TZ).date().isoformat()
    lines=[f'*CLUB | AUDITORIA PILOTO — {NAME}*',
        f'*Venda:* 30/09/2026  •  *Deal:* <https://app.hubspot.com/contacts/7186301/record/0-3/{DEAL_ID}|{DEAL_ID}>  •  *CA:* Carolina Moreno',
        f'*Fontes:* onboarding {rows[0]["fetched_at"].astimezone(TZ):%d/%m %H:%M}; WhatsApp {len(messages)} msgs ({when(messages[0]["ts"])}–{when(messages[-1]["ts"])}); Slack {len(reports)} relatos.',
        '*Vínculo:* nome, venda e CA convergem; ID do Deal ainda não consta no feed. Grupo único por título; associação provável.',
        '\n*JORNADA E SLA* (P = previsão calculada como na tela; O = data original não verificada)']
    for s in sorted(steps,key=lambda x:int(x.get('step_number') or 0)):
        due,basis=dates[s['id']]
        done=dashboard.local_date(s.get('completed_at'))
        verdict=slas.classify(due.isoformat() if due else None,done.isoformat() if done else None,today)
        mark={'completed_late':'🔴','overdue':'🔴','completed_on_time':'✅','pending_on_time':'⏳'}.get(verdict['status'],'?')
        label=s.get('step_name')
        lines.append(f'{mark} {s.get("step_number")}. {label}: P {due.strftime("%d/%m") if due else "?"} → {done.strftime("%d/%m") if done else "pendente"}{" (opcional)" if s.get("is_optional") else ""}')
    lines += ['\n*WHATSAPP | FATOS*',
        '30/09 18h31 Carol se apresenta; 18h35–18h36 Tatiane responde e escolhe 1ª call 02/10 15h e Board 23/11.',
        '30/09 19h20 link do formulário enviado. 01/10 11h51 Tatiane responde durante Scale Ladies. 02/10 10h19 confirma a call.',
        'Não responsividade: não sustentada por este histórico. Apresentação inicial ≠ inserção nos grupos do Club.',
        '\n*LEITURA / AÇÃO*',
        'Handoff e aprovação no comitê aparecem concluídos depois da previsão mostrada. Conferir observações, histórico da previsão e responsabilidade antes de atribuir causa.',
        'Overdelivery permanece sem conclusão registrada: conferir envio e registrar evidência. Prazo de 24h após handoff exige conferir o horário, separado da previsão da tela.',
        'Inserção nos grupos do Club é etapa final distinta; comparar previsão e relato de 07/10 com execução real.',
        '1ª e 2ª calls têm link na jornada; conteúdo das gravações fica para v2.',
        f'<https://gestao40.slack.com/archives/C0BNDFL2PC7/p1791307856123619|Relato Club 06/10> • <https://csg4.g4business.com/onboarding|Jornada>']
    text='\n'.join(lines)
    if len(text)>3900:
        text=text[:3800]+'\n[Resumo limitado por tamanho; histórico integral permanece no CX Monitor.]'
    return text, 'ready'

def send_once():
    if os.getenv('CASE_PILOT_TATIANE_ENABLED','false') != 'true':
        return 'disabled'
    body,state=build()
    if state!='ready':
        log.info('case_pilot status=%s',state)
        return state
    with store.connect() as db:
        owned=db.execute("INSERT INTO delivery_tests(run_key,state) VALUES(%s,'sending') ON CONFLICT DO NOTHING RETURNING run_key",(RUN_KEY,)).fetchone()
    if not owned:
        return 'already_attempted'
    outcome='uncertain'; receipt=None
    try:
        response=slack_delivery._send(body,str(uuid.uuid5(uuid.NAMESPACE_URL,RUN_KEY)))
        if response.get('channel')==os.getenv('SLACK_DESTINATION_CHANNEL') and response.get('ts'):
            outcome='sent'; receipt=response['ts']
    except ValueError:
        outcome='blocked'
    except Exception:
        outcome='uncertain'
    with store.connect() as db:
        db.execute('UPDATE delivery_tests SET state=%s,slack_ts=%s,updated_at=now() WHERE run_key=%s',(outcome,receipt,RUN_KEY))
    log.info('case_pilot status=%s receipt=%s',outcome,receipt or 'none')
    return outcome
