import os
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from monitor import analysis, diagnostics, slack_delivery
from monitor.slas import classify

class SlaTests(unittest.TestCase):
    def test_completed_after_deadline_remains_late(self):
        self.assertEqual(classify('2026-09-28','2026-09-29','2026-10-03')['status'],'completed_late')
    def test_completion_on_deadline_is_on_time(self):
        self.assertFalse(classify('2026-09-28','2026-09-28','2026-10-03')['out_of_sla'])
    def test_pending_before_on_and_after_calendar_deadline(self):
        for today,late in [('2026-10-02',False),('2026-10-03',False),('2026-10-04',True)]:
            self.assertEqual(classify('2026-10-03',None,today)['out_of_sla'],late)
    def test_replanning_does_not_erase_original_breach(self):
        r=classify('2026-10-06','2026-10-02','2026-10-03',original_planned='2026-09-26')
        self.assertTrue(r['out_of_sla']);self.assertEqual(r['effective_deadline'],'2026-09-26')
    def test_missing_invalid_or_future_records_do_not_establish_compliance(self):
        for planned,done in [(None,None),('not-a-date',None),('2026-10-03','2026-10-04')]:
            self.assertIsNone(classify(planned,done,'2026-10-03')['out_of_sla'])

class PipelineTests(unittest.TestCase):
    def test_only_approved_source_channels_are_queried(self):
        with patch.dict(os.environ,SLACK_MONITOR_SOURCE_CHANNELS='other-channel'):
            with self.assertRaisesRegex(ValueError,'unapproved_source_channel'):analysis.configured_sources()
    def test_source_missing_scope_is_persisted_without_logging_remote_body(self):
        db=MagicMock()
        with patch.dict(os.environ,SLACK_MONITOR_SOURCE_CHANNELS='C0BNDFL2PC7'),\
             patch.object(analysis,'sync_source',side_effect=ValueError('slack_missing_scope')),\
             patch.object(analysis.store,'connect') as connect,patch.object(analysis,'sync_threads',return_value=False):
            connect.return_value.__enter__.return_value=db
            analysis.sync_sources(threading.Event())
        self.assertEqual(db.execute.call_args.args[1],('C0BNDFL2PC7','slack_missing_scope'))
    def test_truncated_source_window_is_never_marked_complete(self):
        db=MagicMock();fake=MagicMock()
        fake.call.return_value={'messages':[], 'has_more':True,'response_metadata':{}}
        with patch.dict(os.environ,SLACK_BOT_TOKEN='synthetic'),patch.object(analysis.slack_delivery,'Slack',return_value=fake),\
             patch.object(analysis.store,'connect') as connect,patch.object(analysis,'sync_threads',return_value=False):
            connect.return_value.__enter__.return_value=db
            analysis.sync_source('C0BNDFL2PC7',threading.Event())
        self.assertFalse(db.execute.call_args.args[1][-2])
    def test_thread_pages_include_parent_and_every_reply(self):
        db=MagicMock();client=MagicMock()
        client.call.side_effect=[{'messages':[{'ts':'1.0','reply_count':2,'latest_reply':'3.0'},
            {'ts':'2.0','thread_ts':'1.0','text':'first reply'}],
            'has_more':True,'response_metadata':{'next_cursor':'next'}},
            {'messages':[{'ts':'3.0','thread_ts':'1.0','text':'second reply'}],'has_more':False}]
        stop=MagicMock();stop.is_set.return_value=False;stop.wait.return_value=False
        with patch.object(analysis.store,'connect') as connect,patch.object(analysis,'save_slack_messages') as save:
            connect.return_value.__enter__.return_value=db
            self.assertTrue(analysis.sync_thread(client,'channel','1.0',stop))
            self.assertEqual(save.call_count,2)
        self.assertEqual(client.call.call_args.args[1]['cursor'],'next')
        self.assertEqual(db.execute.call_args.args[1][2:4],(True,2))
    def test_thread_truncation_or_count_mismatch_never_complete(self):
        for data in [{'messages':[{'ts':'1.0','reply_count':1}],'has_more':True},
                     {'messages':[{'ts':'1.0','reply_count':2},{'ts':'2.0','thread_ts':'1.0'}]}]:
            client=MagicMock();client.call.return_value=data;db=MagicMock()
            with patch.object(analysis.store,'connect') as connect,patch.object(analysis,'save_slack_messages'):
                connect.return_value.__enter__.return_value=db
                self.assertFalse(analysis.sync_thread(client,'channel','1.0',threading.Event()))
    def test_thread_without_requested_parent_is_rejected(self):
        client=MagicMock();client.call.return_value={'messages':[{'ts':'2.0','thread_ts':'1.0'}]}
        with self.assertRaisesRegex(ValueError,'invalid_thread_response'):
            analysis.sync_thread(client,'channel','1.0',threading.Event())
    def test_thread_scope_failure_persists_partial_coverage(self):
        db=MagicMock()
        with patch.object(analysis.store,'query',side_effect=[[{'ts':'1.0'}],[{'pending':1}]]),\
             patch.object(analysis,'sync_thread',side_effect=ValueError('slack_missing_scope')),\
             patch.object(analysis.store,'connect') as connect:
            connect.return_value.__enter__.return_value=db
            self.assertFalse(analysis.sync_threads(MagicMock(),'channel',threading.Event()))
        self.assertEqual(db.execute.call_args.args[1],('channel','1.0','slack_missing_scope'))
    def test_evidence_bundle_preserves_unknown_identity_and_requires_review(self):
        case={'instance':'synthetic','chat':'synthetic@g.us','mid':'1','ts':1791040000,
              'body':'Solicitei reembolso','subject':'Synthetic','signals':['cancelamento_reembolso']}
        with patch.object(analysis.store,'query',side_effect=[[case],[],[],[],[],[],[]]):
            bundle=analysis.build_bundle(datetime(2026,10,3,20,tzinfo=timezone.utc))
        self.assertIsNone(bundle['whatsapp_cases'][0]['confirmed_deal_id'])
        self.assertTrue(bundle['requires_review']);self.assertFalse(bundle['published'])
    def test_generation_survives_restart_without_duplicate_hour(self):
        db=MagicMock();db.execute.return_value.fetchone.side_effect=[{'locked':True},{'run_key':'existing'}]
        with patch.object(analysis.store,'connect') as connect,patch.object(analysis,'build_bundle') as build:
            connect.return_value.__enter__.return_value=db
            self.assertEqual(analysis.generate_once(),'already_generated');build.assert_not_called()
    def test_confidential_token_not_in_excerpt_and_truncation_visible(self):
        text=analysis.safe_text('xoxb-synthetic-fake-credential '+('a'*2000))
        self.assertNotIn('xoxb-',text['text']);self.assertTrue(text['truncated'])

class TechnicalTestTests(unittest.TestCase):
    def test_disabled_without_explicit_test_run_id(self):
        with patch.dict(os.environ,{},clear=True),patch.object(diagnostics.store,'connect') as db:
            self.assertEqual(diagnostics.send_once(),'disabled');db.assert_not_called()
    def test_previously_claimed_test_never_posts_again(self):
        db=MagicMock();db.execute.return_value.fetchone.return_value=None
        with patch.dict(os.environ,SLACK_DELIVERY_TEST_ID='synthetic-test'),\
             patch.object(diagnostics.store,'connect') as connect,patch.object(slack_delivery,'deliver_test') as send:
            connect.return_value.__enter__.return_value=db
            self.assertEqual(diagnostics.send_once(),'already_attempted');send.assert_not_called()
    def test_timeout_persists_uncertain_without_retry(self):
        db=MagicMock();db.execute.return_value.fetchone.return_value={'run_key':'test'}
        with patch.dict(os.environ,SLACK_DELIVERY_TEST_ID='test'),\
             patch.object(diagnostics.store,'connect') as connect,\
             patch.object(slack_delivery,'deliver_test',side_effect=slack_delivery.DeliveryUncertain()) as send:
            connect.return_value.__enter__.return_value=db
            self.assertEqual(diagnostics.send_once(),'uncertain');send.assert_called_once()
        self.assertEqual(db.execute.call_args.args[1],('uncertain',None,'test'))
    def test_technical_path_cannot_publish_arbitrary_analysis(self):
        with patch.dict(os.environ,SLACK_DELIVERY_TEST_ID='test'),patch.object(slack_delivery,'_send') as send:
            with self.assertRaisesRegex(ValueError,'technical_test_not_authorized'):
                slack_delivery.deliver_test('arbitrary analysis','id')
            send.assert_not_called()
    def test_fixed_test_can_send_while_analysis_publishing_is_off(self):
        with patch.dict(os.environ,SLACK_DELIVERY_TEST_ID='test',SLACK_PUBLISH_ENABLED='false'),\
             patch.object(slack_delivery,'_send',return_value={'ts':'1.2'}) as send:
            self.assertEqual(slack_delivery.deliver_test(diagnostics.TEST_TEXT,'id')['ts'],'1.2')
            send.assert_called_once_with(diagnostics.TEST_TEXT,'id')

if __name__=='__main__':unittest.main()
