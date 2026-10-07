import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from monitor import dashboard

NOW=datetime(2026,10,4,20,tzinfo=timezone.utc)
def sample():
    return {'schema_version':1,'complete':True,'fetched_at':NOW.isoformat(),
        'members':[{'id':'member1','full_name':'Synthetic','email':'private','deal_id':'unverified'}],
        'steps':[{'id':'step1','member_id':'member1','step_number':0,'planned_at':'2026-09-28','completed_at':'2026-09-29'}],
        'coverage':{'member_count':1,'step_count':1}}

class DashboardTests(unittest.TestCase):
    def test_no_configuration_makes_no_network_request(self):
        with patch.dict(os.environ,{},clear=True),patch.object(dashboard.requests,'get') as get,\
             patch.object(dashboard.store,'connect') as connect:
            self.assertEqual(dashboard.sync_once(NOW),'blocked');get.assert_not_called()
            db=connect.return_value.__enter__.return_value
            self.assertEqual(db.execute.call_args.args[1],('dashboard_read_configuration_missing',))
    def test_only_fixed_https_endpoint_can_receive_secret(self):
        for url in ['http://abc.supabase.co/functions/v1/cx-monitor-onboarding-read',
                    'https://evil.test/functions/v1/cx-monitor-onboarding-read',
                    'https://abc.supabase.co/functions/v1/mutation']:
            with patch.dict(os.environ,DASHBOARD_READ_URL=url,DASHBOARD_READ_TOKEN='a'*32):
                with self.assertRaisesRegex(ValueError,'dashboard_read_url_invalid'):dashboard.configuration()
    def test_unverified_identity_and_unnecessary_personal_data_removed(self):
        result=dashboard.validate_snapshot(sample(),NOW)
        self.assertNotIn('email',result['members'][0]);self.assertNotIn('deal_id',result['members'][0])
    def test_partial_duplicate_or_orphan_snapshot_rejected(self):
        cases=[]
        data=sample();data['complete']=False;cases.append(data)
        data=sample();data['coverage']['step_count']=2;cases.append(data)
        data=sample();data['steps'][0]['member_id']='unknown';cases.append(data)
        data=sample();data['members']*=2;data['coverage']['member_count']=2;cases.append(data)
        for data in cases:
            with self.assertRaises(ValueError):dashboard.validate_snapshot(data,NOW)
    def test_future_or_stale_snapshot_rejected(self):
        for age in [-1,601]:
            data=sample();data['fetched_at']=(NOW-timedelta(seconds=age)).isoformat()
            with self.assertRaisesRegex(ValueError,'dashboard_snapshot_time_invalid'):dashboard.validate_snapshot(data,NOW)
    def test_recorded_late_completion_without_claim_of_original_deadline_or_blame(self):
        with patch.object(dashboard.store,'query',return_value=[{'fetched_at':NOW,'payload':sample()}]):
            context=dashboard.bundle_context(NOW)
        finding=context['sla_findings'][0]
        self.assertTrue(finding['classification']['out_of_sla'])
        self.assertEqual(finding['step_name'],'forms de handoff')
        self.assertFalse(finding['original_deadline_verified']);self.assertFalse(finding['cause_confirmed'])
    def test_stale_persisted_snapshot_does_not_confirm_current_sla(self):
        with patch.object(dashboard.store,'query',return_value=[{'fetched_at':NOW-timedelta(seconds=601),'payload':sample()}]):
            self.assertFalse(dashboard.bundle_context(NOW)['loaded'])
    def test_read_credential_cannot_follow_redirect(self):
        response=MagicMock(status_code=302)
        with patch.dict(os.environ,DASHBOARD_READ_URL='https://abc.supabase.co/functions/v1/cx-monitor-onboarding-read',DASHBOARD_READ_TOKEN='a'*32),\
             patch.object(dashboard.requests,'get',return_value=response) as get,patch.object(dashboard.store,'connect'):
            self.assertEqual(dashboard.sync_once(NOW),'blocked')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

if __name__=='__main__':unittest.main()
