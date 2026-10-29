import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import leadmagnet_worker as worker


class EventWorkerTests(unittest.TestCase):
    def test_webhook_only_regular_heartbeat_never_initializes_poll_adapter(self):
        cfg={'enabled':True,'enabled_since':'2026-10-28T18:35:54.713784+00:00',
             'media_ids':['123'],'webhook_only':True}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json';path.write_text(json.dumps(cfg))
            env={'LEADMAGNET_CONFIG':str(path),'LEADMAGNET_CHECK_ONLY':'false',
                 'LEADMAGNET_EVENT_DRIVEN':'false','COMPOSIO_CONSUMER_API_KEY':'ck_synthetic',
                 'LEADMAGNET_HASH_KEY':'x'*32,'GITHUB_TOKEN':'synthetic','GITHUB_REPOSITORY':'Volmont85/ig-autopost'}
            with patch.dict(os.environ,env), patch.object(worker,'GitHubStore'), \
                 patch.object(worker,'process_event_queue',return_value=0) as process, \
                 patch.object(worker,'InstagramTransport') as transport:
                self.assertEqual(worker.main(),0)
                process.assert_called_once()
                transport.assert_not_called()

    def test_empty_event_queue_uses_zero_composio_calls(self):
        with patch.object(worker,'EventQueue') as queue, patch.object(worker,'LazyEventTransport') as transport:
            queue.return_value.pending.return_value=[]
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(worker.process_event_queue({'event_delivery_enabled':True},
                    {'GITHUB_REPOSITORY':'Volmont85/ig-autopost','GITHUB_TOKEN':'synthetic'},None,None,None),0)
            transport.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['instagram_operations_attempted'],0)
