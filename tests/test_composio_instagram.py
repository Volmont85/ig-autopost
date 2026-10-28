import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from composio_instagram import ConsumerMCP, InstagramTransport, TransportError, decode_rpc, unwrap
from leadmagnet_worker import GitHubStore, verify_material_urls


class Response:
    def __init__(self, value, headers=None):
        self.value, self.headers = value, headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.value).encode()


class TransportTests(unittest.TestCase):
    def test_mcp_sse_and_nested_results(self):
        self.assertEqual(decode_rpc('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"a":1}}\n\n')['result'], {'a':1})
        value={'content':[{'type':'text','text':json.dumps({'successful':True,'data':{'stdout':'done'}})}]}
        self.assertEqual(unwrap(value), {'stdout':'done'})

    def test_no_automatic_http_write_retry_and_key_not_in_error(self):
        calls=[]
        def failure(req, timeout):
            calls.append(req)
            raise TimeoutError('secret ck_fake_must_not_log')
        with self.assertRaises(TransportError) as err:
            ConsumerMCP('ck_fake_must_not_log', failure).rpc('tools/call', {})
        self.assertEqual(len(calls),1)
        self.assertNotIn('ck_fake',str(err.exception))

    def test_private_reply_and_normal_message_use_different_recipient_shapes(self):
        t=InstagramTransport(None)
        calls=[]
        def proxy(method,path,params=None,body=None,check=True):
            calls.append(body)
            return {'message_id':'verified'}
        t.proxy=proxy
        t.private_reply('comment','text')
        t.send_message('user','text')
        self.assertEqual(calls[0]['recipient'],{'comment_id':'comment'})
        self.assertEqual(calls[1]['recipient'],{'id':'user'})

    def test_missing_send_id_is_uncertain(self):
        t=InstagramTransport(None)
        t.proxy=lambda *a,**k:{}
        with self.assertRaises(TransportError): t.private_reply('comment','text')

    def test_follow_missing_or_non_boolean_is_unknown(self):
        t=InstagramTransport(None)
        for value, expected in [(True,True),(False,False),(None,None),('true',None),(1,None)]:
            t.proxy=lambda *a,**k:{'is_user_follow_business':value}
            self.assertIs(t.get_follow_status('123'),expected)

    def test_wrong_default_account_prevents_writes(self):
        class MCP:
            def start(self): pass
            def call(self,*a):
                return {'session':{'id':'s'},'toolkit_connection_statuses':[{'toolkit':'instagram','accounts':[{'status':'ACTIVE','user_info':{'username':'goszakupki.info'}}]}]}
        t=InstagramTransport(MCP())
        t.proxy=lambda *a,**k:{'username':'volmont85','user_id':'wrong'}
        with self.assertRaises(TransportError):t.start()
        self.assertFalse(t.verified)

    def test_comment_pagination_and_own_comments_excluded(self):
        t=InstagramTransport(None)
        pages=[{'data':[{'id':'c1','from':{'id':'201'},'timestamp':'2026-10-28T12:00:00Z','text':'АВАНС'},
                        {'id':'own','from':{'id':t.ig_id},'text':'АВАНС'}],
                'paging':{'next':'not-followed-url','cursors':{'after':'cursor'}}},
               {'data':[{'id':'c2','from':{'id':'202'},'timestamp':'2026-10-28T12:00:00Z'}]}]
        t.proxy=lambda *a,**k:pages.pop(0)
        self.assertEqual([c['id'] for c in t.comments(['media'])],['c1','c2'])

    def test_state_conflict_halts_subsequent_writes(self):
        store=GitHubStore('owner/repo','fake')
        calls=[]
        def failure(*a):
            calls.append(a)
            raise OSError('failed')
        store._request=failure
        with self.assertRaises(RuntimeError):store.save({'claims':{}})
        with self.assertRaises(RuntimeError):store.save({'claims':{}})
        self.assertEqual(len(calls),1)

    def test_state_save_contains_cas_revision(self):
        store=GitHubStore('owner/repo','fake')
        store.sha='old'
        sent=[]
        def request(method,data):
            sent.append(data)
            return {'content':{'sha':'new'}}
        store._request=request
        store.save({'claims':{}})
        self.assertEqual(sent[0]['sha'],'old')
        self.assertEqual(store.sha,'new')

    def test_wrong_material_host_fails_before_network(self):
        with patch('leadmagnet_worker.urlopen') as opener:
            with self.assertRaises(RuntimeError): verify_material_urls([{'url':'https://other.example/file'}])
            opener.assert_not_called()


if __name__=='__main__':unittest.main()
