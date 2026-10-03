import os
import unittest
from unittest.mock import patch, MagicMock
import requests
from starlette.testclient import TestClient
from monitor import outbox, slack_delivery
from monitor.app import app

ENV={'SLACK_ALLOWED_USER_ID':'owner','SLACK_DESTINATION_CHANNEL':'private-test',
     'SLACK_BOT_TOKEN':'synthetic-token','SLACK_PUBLISH_ENABLED':'true'}
REPORT={'report_key':'synthetic:daily:1','text':'Reviewed evidence and limits.',
        'reviewed':True,'reviewed_by':'owner','evidence':[{'source':'synthetic','reference':'message:1'}]}
ROW={'report_key':REPORT['report_key'],'body':REPORT['text'],'channel':'private-test',
     'reviewed_by':'owner','attempts':1}

class OutboxTests(unittest.TestCase):
    def test_unreviewed_or_other_reviewer_cannot_enqueue(self):
        with patch.dict(os.environ, ENV):
            for changes in [{'reviewed':False},{'reviewed_by':'other'}]:
                with self.assertRaisesRegex(ValueError,'owner_review_required'):
                    outbox.validate_report(dict(REPORT,**changes))
    def test_report_requires_evidence_bounded_text_and_aware_expiry(self):
        with patch.dict(os.environ, ENV):
            for changes in [{'evidence':[]},{'text':'x'*3501},{'expires_at':'2020-01-01T00:00:00Z'},
                            {'expires_at':'2099-01-01T00:00:00'},{'evidence':[{'source':'test','reference':''}]}]:
                with self.assertRaises(ValueError):outbox.validate_report(dict(REPORT,**changes))
    def test_hash_binds_text_evidence_reviewer_and_destination(self):
        with patch.dict(os.environ, ENV):
            original=outbox.validate_report(REPORT)['content_hash']
            self.assertNotEqual(original,outbox.validate_report(dict(REPORT,text='Changed'))['content_hash'])
            with patch.dict(os.environ,SLACK_DESTINATION_CHANNEL='other-private'):
                self.assertNotEqual(original,outbox.validate_report(REPORT)['content_hash'])
    def test_disabled_or_missing_credential_never_claims_or_sends(self):
        for change in [{'SLACK_PUBLISH_ENABLED':'false'},{'SLACK_BOT_TOKEN':''}]:
            with patch.dict(os.environ,dict(ENV,**change)),patch.object(outbox,'claim') as claim:
                self.assertEqual(outbox.process_once(),'disabled');claim.assert_not_called()
    def test_confirmed_delivery_persists_receipt_once(self):
        with patch.dict(os.environ,ENV),patch.object(outbox,'claim',return_value=ROW),\
             patch.object(outbox,'finish') as finish,patch.object(slack_delivery,'deliver',return_value={'channel':'private-test','ts':'1.2'}) as send:
            self.assertEqual(outbox.process_once(),'sent')
            finish.assert_called_once_with(ROW,'sent',slack_ts='1.2');send.assert_called_once()
    def test_timeout_is_uncertain_and_has_no_automatic_retry(self):
        with patch.dict(os.environ,ENV),patch.object(outbox,'claim',return_value=ROW),\
             patch.object(outbox,'finish') as finish,patch.object(slack_delivery,'deliver',side_effect=slack_delivery.DeliveryUncertain()):
            self.assertEqual(outbox.process_once(),'uncertain')
            finish.assert_called_once_with(ROW,'uncertain','send_outcome_unknown')
    def test_only_explicit_rate_limit_retries_up_to_three_attempts(self):
        for attempt,state in [(1,'retry'),(3,'blocked')]:
            row=dict(ROW,attempts=attempt)
            with patch.dict(os.environ,ENV),patch.object(outbox,'claim',return_value=row),\
                 patch.object(outbox,'finish') as finish,patch.object(slack_delivery,'deliver',side_effect=ValueError('slack_http_429')):
                self.assertEqual(outbox.process_once(),state)
                finish.assert_called_once_with(row,state,'slack_http_429')
    def test_destination_change_blocks_without_sending(self):
        with patch.dict(os.environ,dict(ENV,SLACK_DESTINATION_CHANNEL='changed')),\
             patch.object(outbox,'claim',return_value=ROW),patch.object(outbox,'finish') as finish,\
             patch.object(slack_delivery,'deliver') as send:
            self.assertEqual(outbox.process_once(),'blocked');send.assert_not_called()
            finish.assert_called_once_with(ROW,'blocked','destination_changed')
    def test_malformed_receipt_does_not_claim_success(self):
        with patch.dict(os.environ,ENV),patch.object(outbox,'claim',return_value=ROW),\
             patch.object(outbox,'finish') as finish,patch.object(slack_delivery,'deliver',return_value={'ok':True}):
            self.assertEqual(outbox.process_once(),'uncertain')
    def test_stable_message_id_for_same_report(self):
        with patch.dict(os.environ,ENV),patch.object(outbox,'claim',return_value=ROW),\
             patch.object(outbox,'finish'),patch.object(slack_delivery,'deliver',return_value={'channel':'private-test','ts':'1.2'}) as send:
            outbox.process_once();outbox.process_once()
            self.assertEqual(send.call_args_list[0].args[1],send.call_args_list[1].args[1])
    def test_same_key_same_content_preserves_sent_state(self):
        with patch.dict(os.environ,ENV):
            report=outbox.validate_report(REPORT)
            db=MagicMock();db.execute.return_value.fetchone.return_value={'content_hash':report['content_hash'],'state':'sent','slack_ts':'1.2'}
            with patch.object(outbox.store,'connect') as connect:
                connect.return_value.__enter__.return_value=db
                self.assertEqual(outbox.enqueue(REPORT)['state'],'sent')
                self.assertIn('ON CONFLICT(report_key) DO NOTHING',db.execute.call_args_list[0].args[0])
    def test_same_key_different_content_rejected(self):
        db=MagicMock();db.execute.return_value.fetchone.return_value={'content_hash':'different','state':'sent','slack_ts':'1.2'}
        with patch.dict(os.environ,ENV),patch.object(outbox.store,'connect') as connect:
            connect.return_value.__enter__.return_value=db
            with self.assertRaisesRegex(ValueError,'report_key_conflict'):outbox.enqueue(REPORT)
    def test_post_timeout_is_distinct_from_preflight_failure(self):
        fake=MagicMock();fake.call.side_effect=requests.Timeout()
        with patch.dict(os.environ,ENV),patch.object(slack_delivery,'Slack',return_value=fake),\
             patch.object(slack_delivery,'validate_destination'):
            with self.assertRaises(slack_delivery.DeliveryUncertain):slack_delivery.deliver('text','synthetic-id')
    def test_query_token_cannot_write_and_ingest_validation_returns_errors(self):
        with patch.dict(os.environ,dict(ENV,WORKER_ENABLED='false',QUERY_TOKEN='query',INGEST_TOKEN='ingest')):
            # Authentication/routing test does not need a second MCP lifespan.
            # The real MCP lifespan/protocol is covered by SafetyTests.
            client=TestClient(app)
            if True:
                self.assertEqual(client.post('/internal/reviewed-reports',json=REPORT,headers={'Authorization':'Bearer query'}).status_code,401)
                self.assertEqual(client.post('/internal/reviewed-reports',json={},headers={'Authorization':'Bearer ingest'}).status_code,400)
                with patch.object(outbox,'enqueue',return_value={'state':'queued'}) as enqueue:
                    self.assertEqual(client.post('/internal/reviewed-reports',json=REPORT,headers={'Authorization':'Bearer ingest'}).status_code,202)
                    enqueue.assert_called_once_with(REPORT)

if __name__=='__main__':unittest.main()
