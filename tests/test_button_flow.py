from dataclasses import replace
from datetime import timedelta
import unittest

from test_leadmagnet_engine import build, FakeTransport, MemoryStore, make_comment, NOW
from webhook.webhook_event import parse_events


class ButtonTransport(FakeTransport):
    def __init__(self, follows=True):
        super().__init__(follows)
        self.buttons=[]
    def send_buttons(self, uid, text, buttons):
        self.buttons.append((uid,text,buttons))
        if self.send_error: raise self.send_error


class ButtonFlowTests(unittest.TestCase):
    def engine(self,t,s):
        e=build(t,s);e.config=replace(e.config,interactive_buttons=True)
        return e

    def test_subscriber_gets_material_button_once_after_reply(self):
        t,s=ButtonTransport(),MemoryStore();e=self.engine(t,s)
        e.run([make_comment()],NOW,incoming_events=[])
        reply={'id':'reply','user_id':'user-1','timestamp':NOW+timedelta(minutes=1),'text':'ПОЛУЧИТЬ'}
        e.run([],NOW+timedelta(minutes=2),incoming_events=[reply])
        self.assertEqual(len(t.buttons),1)
        self.assertEqual(t.buttons[0][2][0]['type'],'web_url')
        self.assertEqual(t.buttons[0][2][0]['title'],'Открыть рабочий лист')
        self.assertEqual(t.send_calls,[])
        e.run([],NOW+timedelta(minutes=3),incoming_events=[reply])
        self.assertEqual(len(t.buttons),1)

    def test_non_subscriber_gets_real_recheck_button_then_material(self):
        t,s=ButtonTransport(False),MemoryStore();e=self.engine(t,s)
        e.run([make_comment()],NOW,incoming_events=[])
        e.run([],NOW+timedelta(minutes=2),incoming_events=[{'id':'r1','user_id':'user-1','timestamp':NOW+timedelta(minutes=1),'text':'ПОЛУЧИТЬ'}])
        self.assertEqual(t.buttons[0][2][-1]['payload'],'LM_CHECK:AVANS')
        self.assertEqual(t.buttons[0][2][0]['title'],'Открыть профиль')
        self.assertNotIn('Открыть рабочий лист',str(t.buttons))
        t.follows=True
        e.run([],NOW+timedelta(minutes=4),incoming_events=[{'id':'r2','user_id':'user-1','timestamp':NOW+timedelta(minutes=3),'text':'ГОТОВО'}])
        self.assertEqual(t.buttons[1][2][0]['title'],'Открыть рабочий лист')

    def test_uncertain_button_delivery_has_no_text_fallback_or_resend(self):
        t,s=ButtonTransport(),MemoryStore();e=self.engine(t,s)
        e.run([make_comment()],NOW,incoming_events=[]);t.send_error=TimeoutError()
        reply={'id':'r','user_id':'user-1','timestamp':NOW+timedelta(minutes=1),'text':'ПОЛУЧИТЬ'}
        e.run([],NOW+timedelta(minutes=2),incoming_events=[reply])
        e.run([],NOW+timedelta(minutes=3),incoming_events=[{**reply,'id':'different'}])
        self.assertEqual(len(t.buttons),1)
        self.assertEqual(t.send_calls,[])
        self.assertEqual(next(iter(s.state['users'].values()))['status'],'uncertain')


class PostbackTests(unittest.TestCase):
    def test_postback_variants_and_deterministic_missing_mid(self):
        value={'sender':{'id':'222'},'recipient':{'id':'111'},'timestamp':int(NOW.timestamp()*1000),
               'postback':{'title':'untrusted label','payload':'LM_CHECK:AVANS'}}
        forms=[{'id':'111','messaging':[value]},
               {'id':'111','field':'messaging_postbacks','value':value},
               {'id':'111','changes':[{'field':'messaging_postbacks','value':value}]}]
        out=[parse_events({'entry':[x]},'111',['444'],['АВАНС']) for x in forms]
        self.assertTrue(all(batch[0]['text']=='ГОТОВО' for batch in out))
        self.assertEqual(len({batch[0]['id'] for batch in out}),1)

    def test_unknown_payload_and_wrong_recipient_are_ignored(self):
        for payload, recipient in [('DELETE_ALL','111'),('LM_CHECK:OTHER','111'),('LM_CHECK:AVANS','999')]:
            value={'sender':{'id':'222'},'recipient':{'id':recipient},'timestamp':int(NOW.timestamp()*1000),
                   'postback':{'payload':payload}}
            self.assertEqual(parse_events({'entry':[{'id':'111','messaging':[value]}]},'111',['444'],['АВАНС']),[])
