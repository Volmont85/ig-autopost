import base64
import json
import unittest
from urllib.error import HTTPError
from webhook.event_queue import EventQueue, PREFIX


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = EventQueue('Volmont85/ig-autopost', 'synthetic-token', base64.urlsafe_b64encode(b'q'*32).decode())
        self.files = {}
        self.calls = []
        def request(method, path, data=None):
            self.calls.append((method, path))
            if method == 'GET':
                if path == PREFIX.rstrip('/'):
                    return [{'path': p} for p in self.files]
                if path not in self.files:
                    raise HTTPError('https://example.test', 404, 'missing', {}, None)
                return self.files[path]
            if method == 'PUT':
                self.files[path] = {'content': data['content'], 'sha': 'synthetic-sha'}
                return {}
            if method == 'DELETE':
                self.assertEqual(data['sha'], self.files[path]['sha'])
                del self.files[path]
                return {}
        self.queue.request = request
        from datetime import datetime, timezone
        self.event = {'kind':'comment', 'id':'123', 'user_id':'456', 'media_id':'789',
                      'text':'АВАНС', 'timestamp':datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')}

    def test_durable_ciphertext_and_idempotent_enqueue(self):
        path = self.queue.enqueue(self.event)
        self.queue.enqueue(self.event)
        self.assertEqual(len(self.files), 1)
        self.assertEqual(sum(m == 'PUT' for m,p in self.calls), 1)
        decoded = base64.b64decode(self.files[path]['content']).decode()
        self.assertNotIn('АВАНС', decoded)
        self.assertNotIn('user_id', decoded)
        pending = self.queue.pending()
        self.assertEqual(pending[0][2], [self.event])
        self.queue.acknowledge(*pending[0][:2])
        self.assertEqual(self.queue.pending(), [])

    def test_multiple_events_survive_one_coalesced_worker(self):
        self.queue.enqueue(self.event)
        self.queue.enqueue({**self.event, 'id':'124'})
        self.assertEqual(len(self.queue.pending()), 2)

    def test_authentication_failure_retains_event(self):
        self.queue.enqueue(self.event)
        for item in self.files.values():
            envelope = json.loads(base64.b64decode(item['content']))
            envelope['ciphertext'] = 'AAAA'
            item['content'] = base64.b64encode(json.dumps(envelope).encode()).decode()
        with self.assertRaises(ValueError): self.queue.pending()
        self.assertEqual(len(self.files), 1)

    def test_wrong_repository_is_rejected(self):
        with self.assertRaises(ValueError): EventQueue('someone/else', 'x', self.queue.key)
