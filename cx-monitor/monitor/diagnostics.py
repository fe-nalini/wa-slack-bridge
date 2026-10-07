"""A separately authorized, fixed-content delivery test; never a reviewed analysis."""
import logging
import os
import re
import uuid
from . import store, slack_delivery

log = logging.getLogger('cx-diagnostics')
TEST_TEXT = ('Teste técnico do CX Monitor pelo app G4 Jornada end-to-end. '
             'Esta mensagem valida a entrega pelo serviço no canal privado. '
             'O envio de análises continua desativado. Desk fica para a última etapa.')

def send_once():
    run_key = os.getenv('SLACK_DELIVERY_TEST_ID', '')
    if not run_key:
        return 'disabled'
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', run_key):
        return 'invalid_configuration'
    # Commit ownership before any network call. An interrupted send is never retried.
    with store.connect() as db:
        row = db.execute('''INSERT INTO delivery_tests(run_key,state) VALUES(%s,'sending')
            ON CONFLICT(run_key) DO NOTHING RETURNING run_key''', (run_key,)).fetchone()
    if row is None:
        return 'already_attempted'
    state, receipt = 'uncertain', None
    try:
        result = slack_delivery.deliver_test(TEST_TEXT, str(uuid.uuid5(uuid.NAMESPACE_URL, 'cx-test:' + run_key)))
        if result.get('channel') == os.getenv('SLACK_DESTINATION_CHANNEL') and result.get('ts'):
            state, receipt = 'sent', result['ts']
    except ValueError:
        state = 'blocked'
    except Exception:
        state = 'uncertain'
    with store.connect() as db:
        db.execute('UPDATE delivery_tests SET state=%s,slack_ts=%s,updated_at=now() WHERE run_key=%s',
                   (state, receipt, run_key))
    log.info('technical_delivery_test state=%s receipt=%s', state, receipt or 'none')
    return state
