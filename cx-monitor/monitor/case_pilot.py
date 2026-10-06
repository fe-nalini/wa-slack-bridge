"""One-time, evidence-only Tatiane pilot in the exclusive private channel."""
import logging
import os
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from . import store, slack_delivery

log = logging.getLogger('cx-case-pilot')
TZ = ZoneInfo('America/Sao_Paulo')
RUN_KEY = 'tatiane-65511973694-evidence-v1'
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
        return datetime.fromtimestamp(int(ts),timezone.utc).astimezone(TZ).strftime('%d/%m %H:%M')
    lines = [f'*TESTE DE EVIDÊNCIAS — Club | {NAME} | Deal {DEAL_ID}*',
        'Leitura do histórico recuperado pelo CX Monitor. Vínculo entre fontes: nome e sequência temporal; Deal ID conhecido no dashboard, mas ainda não transmitido no snapshot. Associação do grupo é provável, não uma chave técnica confirmada.',
        f'Dashboard: {member.get("full_name")} | membro {member["id"]} | CA ID {member.get("ca_id")} | snapshot {rows[0]["fetched_at"].astimezone(TZ):%d/%m %H:%M}.',
        f'Grupo: {chat["subject"]} | {len(messages)} mensagens recuperadas entre {when(messages[0]["ts"])} e {when(messages[-1]["ts"])}.',
        'Etapas (prevista → concluída; vazio = sem data no snapshot):']
    for s in sorted(steps,key=lambda x:(str(x.get('step_number')),str(x.get('id')))):
        lines.append(f'{s.get("step_number")} {s.get("step_name")}: {s.get("planned_at") or "—"} → {s.get("completed_at") or "—"}')
    lines += ['Mensagens do grupo (trechos com ID para conferência):']
    for m in messages[:22]:
        body=' '.join(str(m.get('body') or '').split())[:130]
        lines.append(f'{when(m["ts"])} [{str(m["mid"])[-12:]}] {body or "[sem texto; mídia/tipo a conferir]"}')
    if len(messages)>22:
        lines.append(f'Mais {len(messages)-22} mensagens fora deste resumo; não concluir ausência de contato por este recorte.')
    lines.append(f'Slack: {len(reports)} relatos que citam Tatiane; fonte de relato, não substitui a jornada. ' + ', '.join(f'<https://gestao40.slack.com/archives/C0BNDFL2PC7/p{r["ts"].replace(".", "")}|{when(r["ts"])}>' for r in reports[-5:]))
    lines.append('Este teste apresenta evidências coletadas; não classifica PL, causa, qualidade da call ou conclusão integral do SLA sem observações/anexos e previsão original. Apresentação no grupo inicial e inserção nos grupos do Club são etapas distintas.')
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
