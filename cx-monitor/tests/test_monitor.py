import os
import unittest
from unittest.mock import patch, MagicMock
os.environ['WORKER_ENABLED']='false'
os.environ['QUERY_TOKEN']='query-test-token'
os.environ['INGEST_TOKEN']='ingest-test-token'
from starlette.testclient import TestClient
from monitor.core import normalize,parse_page,scrub,candidate
from monitor.app import app,mcp
from monitor.worker import Evolution,instances_from
from monitor import store

class SafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client=TestClient(app)
        cls.client.__enter__()
    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None,None,None)
    def test_media_and_wrapped_content_keep_evidence(self):
        m=normalize('corporate',{'key':{'remoteJid':'group@g.us','id':'1','participant':'person@lid'},
            'messageTimestamp':{'low':1700000000},'message':{'ephemeralMessage':{'message':{
            'imageMessage':{'caption':'pedido','contextInfo':{'stanzaId':'previous'}}}}}})
        self.assertEqual(m['text'],'pedido');self.assertEqual(m['reply'],'previous')
        self.assertEqual(m['media']['status'],'not_downloaded')
        self.assertFalse(m['from_me']);self.assertEqual(m['sender'],'person@lid')
    def test_pagination_mismatch_blocks_false_coverage(self):
        with self.assertRaisesRegex(ValueError,'pagination_mismatch'):
            parse_page({'messages':{'currentPage':1,'records':[]}},2)
        with self.assertRaises(ValueError):parse_page({'messages':[]},1)
    def test_credentials_scrubbed_without_losing_text(self):
        self.assertEqual(scrub({'apikey':'secret','data':{'text':'request','Authorization':'secret'}}),{'data':{'text':'request'}})
    def test_signals_are_review_candidates(self):
        self.assertEqual(candidate('bom dia'),[])
        self.assertIn('reclame_aqui',candidate('Vou ao Reclame Aqui'))
    def test_provider_action_endpoints_unreachable(self):
        with patch.dict(os.environ,EVOLUTION_URL='https://example.com',EVOLUTION_API_KEY='test'):
            e=Evolution()
            with self.assertRaisesRegex(ValueError,'endpoint_not_allowed'):
                e.read('/message/sendText/corporate',{'text':'hello'})
    def test_anonymous_and_wrong_role_requests_blocked(self):
        if True:
            c=self.client
            self.assertEqual(c.get('/health').status_code,200)
            self.assertEqual(c.post('/mcp',json={}).status_code,401)
            self.assertEqual(c.post('/internal/slack-import',headers={'Authorization':'Bearer query-test-token'},json={}).status_code,401)
            self.assertEqual(c.get('/health',headers={'Origin':'https://attacker.example'}).status_code,403)
            self.assertEqual(c.post('/mcp',headers={'Host':'attacker.example'},json={}).status_code,403)
    def test_mcp_real_protocol_and_only_read_tools(self):
        if True:
            c=self.client
            h={'Authorization':'Bearer query-test-token','Accept':'application/json, text/event-stream'}
            init=c.post('/mcp',headers=h,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{
                'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
            self.assertEqual(init.status_code,200,init.text)
            result=c.post('/mcp',headers=h,json={'jsonrpc':'2.0','id':2,'method':'tools/list'}).json()['result']
            self.assertEqual(len(result['tools']),7)
            for tool in result['tools']:
                self.assertTrue(tool['annotations']['readOnlyHint'])
            self.assertNotIn('send', ' '.join(t['name'] for t in result['tools']))
    def test_read_queries_use_postgres_readonly_transaction(self):
        db=MagicMock();db.execute.return_value.fetchall.return_value=[]
        with patch.object(store,'connect') as connect:
            connect.return_value.__enter__.return_value=db
            store.query('SELECT * FROM messages')
        self.assertEqual(db.execute.call_args_list[0].args[0],'SET TRANSACTION READ ONLY')
    def test_instances_two_supported_envelopes(self):
        self.assertEqual(instances_from([{'name':'corporate'}]),['corporate'])
        self.assertEqual(instances_from([{'instance':{'instanceName':'corporate'}}]),['corporate'])

if __name__=='__main__':unittest.main()
