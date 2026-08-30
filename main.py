from flask import Flask, request, jsonify
import requests
import os
from datetime import datetime

app = Flask(__name__)

SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
TARGET_GROUP_JID = '120363409223805899@g.us'
GROUP_NAME = 'Combate ao cancelamento'

def format_slack_message(push_name, text, timestamp):
    try:
        dt = datetime.fromtimestamp(int(timestamp)).strftime('%d/%m %H:%M')
    except:
        dt = ''

    time_str = f" `{dt}`" if dt else ''
    return f"*{push_name}*{time_str}: {text}"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json or {}
        event = data.get('event', '')

        if event not in ['messages.upsert', 'MESSAGES_UPSERT']:
            return jsonify({'status': 'ignored', 'event': event}), 200

        msg_data = data.get('data', {})
        key = msg_data.get('key', {})
        remote_jid = key.get('remoteJid', '')
        from_me = key.get('fromMe', False)

        if remote_jid != TARGET_GROUP_JID:
            return jsonify({'status': 'ignored', 'jid': remote_jid}), 200

        push_name = msg_data.get('pushName', 'Desconhecido')
        message = msg_data.get('message', {})
        timestamp = msg_data.get('messageTimestamp', '')

        text = (
            message.get('conversation') or
            message.get('extendedTextMessage', {}).get('text') or
            message.get('imageMessage', {}).get('caption') or
            message.get('videoMessage', {}).get('caption') or
            message.get('documentMessage', {}).get('title') or
            '[📎 mídia ou arquivo]'
        )

        if not text or text.strip() == '':
            return jsonify({'status': 'empty_message'}), 200

        if SLACK_WEBHOOK_URL:
            slack_text = format_slack_message(push_name, text, timestamp)
            prefix = "↩️ " if from_me else ""
            payload = {
                "text": f"{prefix}{slack_text}",
                "username": f"WhatsApp · {GROUP_NAME}",
                "icon_emoji": ":whatsapp:"
            }
            r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
            return jsonify({'status': 'sent', 'slack_status': r.status_code}), 200
        else:
            print(f"[{push_name}]: {text}")
            return jsonify({'status': 'no_slack_webhook'}), 200

    except Exception as e:
        print(f"Erro: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/', methods=['GET'])
def health():
    return jsonify({
        'status': 'running',
        'service': 'WhatsApp → Slack Bridge',
        'group': GROUP_NAME
    }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    print(f"Bridge rodando na porta {port}")
    app.run(host='0.0.0.0', port=port)
