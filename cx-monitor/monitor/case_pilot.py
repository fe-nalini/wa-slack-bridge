"""One-time, evidence-only Tatiane pilot in the exclusive private channel."""
import logging
import os
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from . import store, slack_delivery, dashboard, slas

log = logging.getLogger('cx-case-pilot')
TZ = ZoneInfo('America/Sao_Paulo')
RUN_KEY = 'tatiane-65511973694-audit-v3-context'
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
    lines=[f'*🔎 FRED | PILOTO CX / CS — {NAME}*',
        f'*Data venda:* {member.get("sale_date") or "30/09/2026 (referência fornecida)"} • *Negócio / Deal:* <https://app.hubspot.com/contacts/7186301/record/0-3/{DEAL_ID}|{DEAL_ID}> • *CA:* Carolina Moreno (relato Slack)',
        f'*Leitura das fontes:* onboarding {rows[0]["fetched_at"].astimezone(TZ):%d/%m %H:%M}; WhatsApp {len(messages)} mensagens; Slack {len(reports)} relatos.',
        '*Escopo:* teste contextual com jornada, mensagens e relatos abaixo. Deals Premium / timeline comercial não consultada neste teste. Histórico da previsão original e anexos não disponíveis no feed; notas limitadas a 500 caracteres. Nenhuma conclusão sobre churn, pagamento ou exceção comercial sem essa fonte.',
        '*Identidade:* membro único e grupo único pelo nome. Deal indicado por Fernanda; vínculo grupo–Deal provável, sem identificador compartilhado confirmado.',
        '\n*🟠 LEITURA E ACIONÁVEL*',
        'No histórico já auditado, Tatiane responde à apresentação, escolhe agenda e confirma a call. Isso sustenta engajamento e não sustenta PL por não responsividade. As mensagens atuais estão transcritas abaixo para conferência; eventuais novidades prevalecem sobre essa leitura.',
        'A comunicação inicial apresenta próximos passos e convida ao Board; é um sinal positivo de condução. Para avaliar personalização premium e primeiro valor, é necessário relacionar objetivos registrados nas notas à construção da jornada. Horário exato da venda ausente: não é possível calcular venda → primeira mensagem com precisão.',
        'Apresentação no grupo de onboarding e inserção nos grupos do Club são etapas distintas. O relato de inserção em 07/10 deve ser confrontado com a execução da etapa final, sem tratar a apresentação de 30/09 como conclusão dessa etapa.',
        '*Próximo passo:* Carolina, como CA citada no report, revisar as pendências e evidências da jornada; responsabilidade de cada causa a validar. Malu (operações Club) apoiar consistência dos registros; atribuição desta ação proposta, não confirmada.',
        '\n*⏱ ETAPAS EM ATRASO — HÁ QUANTOS DIAS*',
        'Todas obrigatórias. Comparação com previsão atual calculada como na tela; violação da previsão original a validar. Contagem abaixo em dias corridos; data sem hora vence ao fim do dia local.']
    for s in sorted(steps,key=lambda x:int(x.get('step_number') or 0)):
        due,basis=dates[s['id']]
        done=dashboard.local_date(s.get('completed_at'))
        verdict=slas.classify(due.isoformat() if due else None,done.isoformat() if done else None,today)
        if verdict['status'] in ('completed_late','overdue'):
            end=done or datetime.now(TZ).date()
            days=(end-due).days
            lines.append(f'🔴 {s.get("step_number")}. {s.get("step_name")}: previsão {due:%d/%m}; '+(f'concluída {done:%d/%m}' if done else 'sem conclusão registrada')+f'; {days} dia(s). Causa ainda não atribuída.')
    lines += ['*Overdelivery:* SLA da SOLICITAÇÃO = até 24h úteis após forms de handoff; entrega e check são eventos diferentes. Não aplicar automaticamente a previsão genérica da etapa para julgar a solicitação. Horário/evidência da solicitação e calendário de horas úteis precisam sustentar o veredito.',
        '\n*📝 OBSERVAÇÕES / EVIDÊNCIAS DA JORNADA*']
    for s in sorted(steps,key=lambda x:int(x.get('step_number') or 0)):
        if s.get('note'):
            lines.append(f'{s.get("step_number")}. {s.get("step_name")}: {s["note"]}')
        if s.get('call_link'):
            lines.append(f'{s.get("step_number")}. gravação registrada; conteúdo reservado à v2.')
    if not any(s.get('note') for s in steps):
        lines.append('Nenhuma nota recebida nesta leitura. Isso não prova ausência de atendimento.')
    lines += ['\n*💬 WHATSAPP — CRONOLOGIA E REFERÊNCIAS*',f'Grupo: {chat["subject"]} • JID: {chat["jid"]}']
    for m in messages:
        lines.append(f'{when(m["ts"])} • ID {m["mid"]} • {m.get("body") or "[mídia/sem texto]"}')
    lines += ['\n*📣 SLACK — DECLARAÇÕES DO REPORT*']
    for r in reports:
        link='https://gestao40.slack.com/archives/C0BNDFL2PC7/p'+str(r['ts']).replace('.','')
        lines.append(f'<{link}|{when(r["ts"])}> — {r["body"]}')
    lines += ['\n*⚖ CONVERGÊNCIAS / DIVERGÊNCIAS / DECISÃO*',
        'Convergência a verificar pelas transcrições: disponibilidade da cliente e agenda. Relato de call ainda “agendada” depois da data merece conciliação com o check e a evidência da conclusão; isso é diferença de atualização, não prova de call não realizada.',
        'Calls com o mesmo horário de conclusão exigem conferir se é horário da execução ou do registro. Links de gravação não demonstram conteúdo nem resultado sem leitura.',
        'Classificação: atenção operacional a validar nos pontos de prazo e qualidade de registro; PL / churn não confirmados. Atraso e causa separados. A regra de cancelamento depende do marco de onboarding e da cronologia comercial; venda do mês passado, isoladamente, não determina responsabilidade.',
        '<https://csg4.g4business.com/onboarding|Fonte da jornada> • <https://app.notion.com/p/g40/Regras-de-Cancelamento-Receita-Prazos-e-Responsabilidades-d76b431349e1831f97f78187675d3d79|Regras de PL / cancelamento>',
        '*Resultado do teste:* leitura e publicação destas fontes; auditoria ponta a ponta permanece parcial pelas lacunas declaradas.']
    text='\n'.join(lines)
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
