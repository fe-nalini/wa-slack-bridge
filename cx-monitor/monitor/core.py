"""Normalization and conservative candidate detection; source text is untrusted data."""
import hashlib
import json
import re

SECRET_KEYS = {'apikey', 'api_key', 'authorization', 'password', 'access_token', 'token'}

def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k.lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value

def unpack(message):
    for _ in range(6):
        nested = next((message.get(k, {}).get('message') for k in
                       ('ephemeralMessage', 'viewOnceMessage', 'viewOnceMessageV2', 'documentWithCaptionMessage')
                       if isinstance(message.get(k), dict) and message[k].get('message')), None)
        if not nested:
            break
        message = nested
    return message

def normalize(instance, record):
    key = record.get('key') or {}
    chat = key.get('remoteJid')
    if not chat or chat == 'status@broadcast':
        return None
    content = unpack(record.get('message') or {})
    text = content.get('conversation') or ''
    kind = record.get('messageType') or next(iter(content), 'unknown')
    reply = (record.get('contextInfo') or {}).get('stanzaId')
    media = None
    for k in ('extendedTextMessage', 'imageMessage', 'videoMessage', 'documentMessage', 'audioMessage', 'stickerMessage'):
        v = content.get(k)
        if isinstance(v, dict):
            text = text or v.get('text') or v.get('caption') or v.get('title') or ''
            reply = reply or (v.get('contextInfo') or {}).get('stanzaId')
            if k != 'extendedTextMessage':
                media = {'type': k, 'status': 'not_downloaded', 'mime': v.get('mimetype')}
    mid = key.get('id') or record.get('id')
    uncertain = not bool(mid)
    if uncertain:
        mid = 'fallback:' + hashlib.sha256(json.dumps(scrub(record), sort_keys=True).encode()).hexdigest()
    ts = record.get('messageTimestamp')
    if isinstance(ts, dict):
        ts = ts.get('low')
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        ts = None
    return {'instance': instance, 'chat': chat, 'mid': str(mid),
            'sender': key.get('participant') or key.get('participantAlt') or chat,
            'from_me': bool(key.get('fromMe')), 'push_name': record.get('pushName'),
            'ts': ts, 'text': str(text), 'kind': kind, 'reply': reply,
            'media': media, 'uncertain_identity': uncertain, 'raw': scrub(record)}

def candidate(text):
    """Signals requiring review. Never claims a broken SLA or staff fault."""
    value = text.casefold()
    signals = []
    for code, pattern in [
        ('reclame_aqui', r'reclame\s*aqui|\bprocon\b'),
        ('cancelamento_reembolso', r'cancelamento|cancelar|reembolso|estorno'),
        ('repeticao_sem_resposta', r'ningu[eé]m (?:me )?responde|sem (?:nenhuma )?resposta|j[aá] (?:pedi|solicitei|falei)'),
    ]:
        if re.search(pattern, value):
            signals.append(code)
    return signals

def parse_page(data, requested):
    envelope = data.get('messages') if isinstance(data, dict) else None
    if not isinstance(envelope, dict) or not isinstance(envelope.get('records'), list):
        raise ValueError('unsupported_messages_envelope')
    if int(envelope.get('currentPage', requested)) != requested:
        raise ValueError('pagination_mismatch')
    return envelope['records'], int(envelope.get('pages', 0)), int(envelope.get('total', 0))
