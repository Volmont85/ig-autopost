import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from datetime import timedelta
from unittest.mock import patch

from test_leadmagnet_engine import NOW, ENTRY, MemoryStore, FakeTransport, build, make_comment
from composio_instagram import InstagramTransport, TransportError
from webhook.webhook_event import parse_events, encrypt_events, decrypt_events


class EventEngineTests(unittest.TestCase):
    def test_new_lead_uses_one_invitation_then_two_operations_after_reply(self):
        t, s = FakeTransport(True), MemoryStore()
        e = build(t, s)
        e.run([make_comment()], NOW, incoming_events=[])
        self.assertEqual((t.follow_calls, len(t.private_calls), len(t.incoming_calls)), (0, 1, 0))
        reply = {'id': 'mid:opaque', 'user_id': 'user-1', 'timestamp': NOW + timedelta(minutes=1), 'text': 'ПОЛУЧИТЬ'}
        e.run([], NOW + timedelta(minutes=2), incoming_events=[reply])
        self.assertEqual((t.follow_calls, len(t.send_calls), len(t.incoming_calls)), (1, 1, 0))
        before = (t.follow_calls, len(t.send_calls), len(t.private_calls))
        e.run([], NOW + timedelta(minutes=3), incoming_events=[reply])
        self.assertEqual((t.follow_calls, len(t.send_calls), len(t.private_calls)), before)
        self.assertNotIn('mid:opaque', repr(s.state))
        self.assertNotIn('user-1', repr(s.state))

    def test_known_consent_next_material_uses_follow_and_private_reply(self):
        t, s = FakeTransport(True), MemoryStore()
        s.state['consented_users'] = {hmac.new(b'test-only-secret', b'user\0user-1', hashlib.sha256).hexdigest(): NOW.isoformat()}
        build(t, s).run([make_comment()], NOW, incoming_events=[])
        self.assertEqual((t.follow_calls, len(t.private_calls), len(t.incoming_calls)), (1, 1, 0))
        self.assertIn(ENTRY['url'], t.private_calls[0][1])

    def test_unrelated_dm_and_stop_do_not_fetch_inbox_or_send_material(self):
        t, s = FakeTransport(True), MemoryStore()
        e = build(t, s)
        e.run([], NOW, incoming_events=[{'id': 'm', 'user_id': 'stranger', 'timestamp': NOW, 'text': 'hi'}])
        self.assertEqual((t.follow_calls, t.send_calls, t.incoming_calls), (0, [], []))
        e.run([make_comment()], NOW, incoming_events=[])
        e.run([], NOW + timedelta(minutes=2), incoming_events=[{'id': 'stop', 'user_id': 'user-1', 'timestamp': NOW + timedelta(minutes=1), 'text': 'Не присылайте'}])
        self.assertEqual((t.follow_calls, t.send_calls), (0, []))
        self.assertEqual(next(iter(s.state['users'].values()))['status'], 'opted_out')

    def test_uncertain_event_send_is_not_retried(self):
        t, s = FakeTransport(True), MemoryStore()
        t.private_error = TimeoutError()
        e = build(t, s)
        e.run([make_comment()], NOW, incoming_events=[])
        e.run([make_comment()], NOW, incoming_events=[])
        self.assertEqual((t.follow_calls, len(t.private_calls)), (0, 1))


class PinnedTransportTests(unittest.TestCase):
    def transport(self, **changes):
        account = {'id': 'pinned', 'status': 'ACTIVE', 'is_default': True,
                   'user_info': {'username': 'goszakupki.info', 'id': 'profile'}}
        account.update(changes)
        class MCP:
            def start(self): pass
            def call(self, *args):
                return {'session': {'id': 's'}, 'toolkit_connection_statuses': [{'toolkit': 'instagram', 'accounts': [account]}]}
        return InstagramTransport(MCP())

    def test_binding_uses_free_metadata_no_instagram_read(self):
        t = self.transport()
        t.start(pinned_account='pinned', pinned_profile_id='profile')
        self.assertTrue(t.verified)
        self.assertEqual(t.instagram_operations_attempted, 0)

    def test_default_and_profile_changes_fail_closed(self):
        for change in ({'is_default': False}, {'id': 'changed'}, {'user_info': {'username': 'goszakupki.info', 'id': 'different'}}):
            t = self.transport(**change)
            with self.assertRaises(TransportError):
                t.start(pinned_account='pinned', pinned_profile_id='profile')
            self.assertFalse(t.verified)


class RealPayloadShapeTests(unittest.TestCase):
    def test_nested_author_and_opaque_message_id(self):
        data = {'object': 'instagram', 'entry': [{'id': '111', 'time': int(NOW.timestamp()),
            'changes': [{'field': 'comments', 'value': {'id': '333', 'from': {'id': '222'},
                        'media': {'id': '444'}, 'text': 'АВАНС'}}],
            'messaging': [{'sender': {'id': '222'}, 'recipient': {'id': '111'},
                'timestamp': int(NOW.timestamp() * 1000), 'message': {'mid': 'aWdfZAG1faXRlbTox==', 'text': 'ПОЛУЧИТЬ'}}]}]}
        events = parse_events(data, '111', ['444'], ['АВАНС'])
        self.assertEqual([e['kind'] for e in events], ['comment', 'message'])
        key = base64.urlsafe_b64encode(b'x' * 32).decode()
        self.assertEqual(decrypt_events(encrypt_events(events, key, NOW), key, NOW), events)

    def test_root_account_does_not_override_wrong_entry(self):
        data = {'id': '111', 'entry': [{'id': '999', 'time': int(NOW.timestamp()), 'field': 'comments',
                'value': {'id': '333', 'from': {'id': '222'}, 'media': {'id': '444'}, 'text': 'АВАНС'}}]}
        self.assertEqual(parse_events(data, '111', ['444'], ['АВАНС']), [])


class ReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(DISABLE_HEARTBEAT='1', META_VERIFY_TOKEN='synthetic-verify', META_APP_SECRET='synthetic-secret',
            GH_DISPATCH_TOKEN='synthetic-gh', IG_EVENTS_ENABLED='1', IG_EVENT_ENCRYPTION_KEY=base64.urlsafe_b64encode(b'x'*32).decode(),
            IG_EVENT_MEDIA_IDS='["444"]')
        folder = Path(__file__).resolve().parents[1] / 'webhook'
        sys.path.insert(0, str(folder))
        spec = importlib.util.spec_from_file_location('receiver_under_test', folder / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()

    def payload(self):
        return json.dumps({'entry': [{'id': '17841410301724309', 'time': int(NOW.timestamp()),
            'changes': [{'field': 'comments', 'value': {'id': '333', 'from': {'id': '222'}, 'media': {'id': '444'}, 'text': 'АВАНС'}}]}]}).encode()

    def test_signature_challenge_and_dispatch_after_persistence(self):
        self.assertEqual(self.client.get('/instagram-events?hub.mode=subscribe&hub.verify_token=synthetic-verify&hub.challenge=challenge').data, b'challenge')
        self.assertEqual(self.client.get('/instagram-events?hub.mode=subscribe&hub.verify_token=bad').status_code, 403)
        raw = self.payload()
        self.assertEqual(self.client.post('/instagram-events', data=raw).status_code, 403)
        calls = []
        with patch.object(self.module, 'EventQueue') as q, patch.object(self.module, '_dispatch_workflow', side_effect=lambda *a: calls.append('dispatch')):
            q.return_value.enqueue.side_effect = lambda e: calls.append('persist')
            sig = 'sha256=' + hmac.new(b'synthetic-secret', raw, hashlib.sha256).hexdigest()
            response = self.client.post('/instagram-events', data=raw, headers={'X-Hub-Signature-256': sig})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(calls, ['persist', 'dispatch'])
            self.assertNotIn('222', repr(self.module._event_status))

    def test_dispatch_failure_returns_retry_not_false_success(self):
        raw = self.payload()
        sig = 'sha256=' + hmac.new(b'synthetic-secret', raw, hashlib.sha256).hexdigest()
        with patch.object(self.module, 'EventQueue'), patch.object(self.module, '_dispatch_workflow', side_effect=RuntimeError()):
            self.assertEqual(self.client.post('/instagram-events', data=raw, headers={'X-Hub-Signature-256': sig}).status_code, 503)
