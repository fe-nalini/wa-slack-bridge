import unittest
from monitor.slack_delivery import validate_destination

class FakeSlack:
    def __init__(self,private=True,shared=False,members=None):
        self.private=private;self.shared=shared;self.members=members or ['owner','bot']
    def call(self,name,payload):
        if name=='auth.test':return {'user_id':'bot'}
        if name=='conversations.info':return {'channel':{'is_private':self.private,'is_shared':self.shared}}
        if name=='conversations.members':return {'members':self.members}
        raise AssertionError('No send is permitted during destination verification')

class DeliverySafety(unittest.TestCase):
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

if __name__=='__main__':unittest.main()
