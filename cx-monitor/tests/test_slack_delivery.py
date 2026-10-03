import unittest
from unittest.mock import patch
from monitor.slack_delivery import Slack, validate_destination, probe_destination

class FakeSlack:
    def __init__(self,private=True,shared=False,members=None):
        self.private=private;self.shared=shared;self.members=members or ['owner','bot']
    def call(self,name,payload):
        if name=='auth.test':return {'user_id':'bot'}
        if name=='conversations.info':return {'channel':{'is_private':self.private,'is_shared':self.shared}}
        if name=='conversations.members':return {'members':self.members}
        raise AssertionError('No send is permitted during destination verification')

class DeliverySafety(unittest.TestCase):
    def test_read_methods_pass_channel_and_cursor_as_get_parameters(self):
        with patch('monitor.slack_delivery.requests.Session') as factory:
            factory.return_value.get.return_value.json.return_value={'ok':True}
            client=Slack('synthetic')
            for method in ['conversations.info','conversations.members']:
                payload={'channel':'synthetic-channel','cursor':'next-page'}
                client.call(method,payload)
                factory.return_value.get.assert_called_with('https://slack.com/api/'+method,
                    params=payload,timeout=(10,30))
            factory.return_value.post.assert_not_called()
    def test_unsupported_method_never_reaches_slack(self):
        with patch('monitor.slack_delivery.requests.Session') as factory:
            with self.assertRaisesRegex(ValueError,'unsupported_slack_method'):
                Slack('synthetic').call('conversations.invite',{'channel':'synthetic-channel'})
            factory.return_value.get.assert_not_called()
            factory.return_value.post.assert_not_called()
    def test_only_owner_and_current_app_can_receive(self):
        validate_destination(FakeSlack(),'synthetic-channel','owner')
    def test_added_human_or_other_bot_blocks_delivery(self):
        for extra in ['other-human','other-bot']:
            with self.assertRaisesRegex(ValueError,'unexpected_recipient'):
                validate_destination(FakeSlack(members=['owner','bot',extra]),'synthetic-channel','owner')
    def test_public_or_shared_blocks_delivery(self):
        for client in [FakeSlack(private=False),FakeSlack(shared=True)]:
            with self.assertRaises(ValueError):validate_destination(client,'synthetic-channel','owner')
    def test_absent_owner_blocks_delivery(self):
        with self.assertRaises(ValueError):validate_destination(FakeSlack(members=['bot']),'synthetic-channel','owner')
    def test_authenticated_bot_must_be_a_channel_member(self):
        with self.assertRaisesRegex(ValueError,'unexpected_recipient'):
            validate_destination(FakeSlack(members=['owner']),'synthetic-channel','owner')
    def test_preflight_is_read_only_with_publishing_disabled(self):
        with patch.dict('os.environ', {'SLACK_BOT_TOKEN':'synthetic',
                'SLACK_DESTINATION_CHANNEL':'synthetic-channel','SLACK_ALLOWED_USER_ID':'owner',
                'SLACK_PUBLISH_ENABLED':'false'}, clear=True), patch('monitor.slack_delivery.Slack',return_value=FakeSlack()):
            self.assertEqual(probe_destination()['status'],'ready')
    def test_preflight_preserves_fixed_error_without_echoing_arbitrary_content(self):
        with patch.dict('os.environ', {'SLACK_BOT_TOKEN':'synthetic'}, clear=True):
            for error,expected in [('slack_missing_scope','slack_missing_scope'),
                                   ('response contains a confidential value','slack_check_failed')]:
                with patch('monitor.slack_delivery.validate_destination',side_effect=ValueError(error)):
                    self.assertEqual(probe_destination(),{'status':'blocked','code':expected,'stage':'configuration'})

if __name__=='__main__':unittest.main()
