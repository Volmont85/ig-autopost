"""Scheduled worker: disabled until config, live URLs and secrets are ready."""
import base64
import json
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from composio_instagram import ConsumerMCP, InstagramTransport, TransportError
from leadmagnet_engine import EngineConfig, LeadMagnetEngine
from webhook.event_queue import EventQueue


class GitHubStore:
    """CAS persistence BEFORE sends. An uncertain save stops all subsequent sends."""
    def __init__(self, repo, token, path='leadmagnet/goszakupki/composio_state.json', hash_key_id=None):
        self.repo, self.token, self.path = repo, token, path
        self.sha = None
        self.failed = False
        self.hash_key_id = hash_key_id

    def _request(self, method, data=None):
        req = Request('https://api.github.com/repos/' + self.repo + '/contents/' + self.path,
                      headers={'Authorization': 'Bearer ' + self.token,
                               'Accept': 'application/vnd.github+json',
                               'Content-Type': 'application/json', 'User-Agent': 'goszakupki-leadmagnet'},
                      data=json.dumps(data).encode() if data is not None else None, method=method)
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    def load(self):
        try:
            row = self._request('GET')
        except HTTPError as exc:
            if exc.code == 404:
                return {}
            raise RuntimeError('Cannot read durable lead-magnet state') from None
        self.sha = row['sha']
        state = json.loads(base64.b64decode(row['content']))
        if not isinstance(state, dict):
            raise RuntimeError('Invalid durable state; refusing to reset')
        if self.hash_key_id and state.get('hash_key_id') != self.hash_key_id:
            raise RuntimeError('State key changed; migration required before delivery')
        return state

    def save(self, state):
        if self.failed:
            raise RuntimeError('State persistence halted for this run')
        state = dict(state)
        if self.hash_key_id:
            state['hash_key_id'] = self.hash_key_id
        payload = {'message': 'leadmagnet: durable processing checkpoint [skip ci]',
                   'content': base64.b64encode((json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()).decode(),
                   'branch': 'main'}
        if self.sha:
            payload['sha'] = self.sha
        try:
            row = self._request('PUT', payload)
            self.sha = row['content']['sha']
        except Exception:
            self.failed = True
            raise RuntimeError('Durable state save failed; no automatic retry') from None


def verify_material_urls(entries):
    for entry in entries:
        url = entry['url']
        if not url.startswith('https://goszakupki.info/'):
            raise RuntimeError('Only the configured goszakupki.info material host is allowed')
        # Read once before allowing an outbound message; no imaginary/broken lead-magnet URLs.
        with urlopen(Request(url, headers={'User-Agent': 'GoszakupkiMaterialCheck/1.0'}), timeout=25) as r:
            final_url = r.geturl()
            if r.status != 200 or not final_url.startswith('https://goszakupki.info/'):
                raise RuntimeError('Lead-magnet page unavailable')
            text = r.read(500000).decode('utf-8')
        if entry.get('page_marker') and entry['page_marker'] not in text:
            raise RuntimeError('Wrong lead-magnet page content')


class LazyEventTransport:
    """No Composio calls for duplicates, echoes or unrelated messages."""
    def __init__(self, key, cfg):
        self.transport = InstagramTransport(ConsumerMCP(key))
        self.cfg, self.started, self.failed = cfg, False, False

    def _start(self):
        if self.failed:
            raise TransportError('Event transport initialization previously failed')
        if not self.started:
            try:
                self.transport.start(pinned_account=self.cfg['event_account_id'],
                                     pinned_profile_id=self.cfg['event_profile_id'])
                self.started = True
            except Exception:
                self.failed = True
                raise

    def get_follow_status(self, uid):
        self._start()
        return self.transport.get_follow_status(uid)

    def private_reply(self, cid, text):
        self._start()
        return self.transport.private_reply(cid, text)

    def send_message(self, uid, text):
        self._start()
        return self.transport.send_message(uid, text)


def process_event_queue(cfg, env, enabled, now, store):
    queue = EventQueue(env['GITHUB_REPOSITORY'], env['GITHUB_TOKEN'],
                       os.environ.get('IG_EVENT_ENCRYPTION_KEY', ''))
    pending = queue.pending()
    events = [event for _, _, batch in pending for event in batch]
    if cfg.get('event_delivery_enabled') is not True:
        print(json.dumps({'mode': 'event_shadow', 'queued_events': len(events),
                          'comments': sum(e['kind'] == 'comment' for e in events),
                          'messages': sum(e['kind'] == 'message' for e in events),
                          'instagram_operations_attempted': 0}))
        return 0
    if not events:
        print(json.dumps({'mode': 'event', 'queued_events': 0, 'instagram_operations_attempted': 0}))
        return 0
    verify_material_urls(cfg['entries'])
    transport = LazyEventTransport(env['COMPOSIO_CONSUMER_API_KEY'], cfg)
    engine = LeadMagnetEngine(EngineConfig(entries=cfg['entries'], enabled_since=enabled,
        allowlisted_media_ids=frozenset(cfg['media_ids']), hash_key=env['LEADMAGNET_HASH_KEY'].encode()),
        transport, store)
    counters = engine.run([e for e in events if e['kind'] == 'comment'], now,
                         incoming_events=[e for e in events if e['kind'] == 'message'])
    t = transport.transport
    counters.update({'mode': 'event', 'queued_events': len(events),
                     'instagram_operations_attempted': t.instagram_operations_attempted,
                     'proxy_calls': t.proxy_calls, 'mcp_http_requests': t.mcp.http_requests,
                     'mcp_tool_invocations': t.mcp.tool_invocations})
    print(json.dumps(counters))
    if counters['uncertain'] or counters['errors'] or transport.failed:
        raise RuntimeError('Event deliveries require review; spool retained, no blind retries')
    for path, sha, _ in pending:
        queue.acknowledge(path, sha)
    return 0


def main():
    config_path = Path(os.environ.get('LEADMAGNET_CONFIG', 'leadmagnets/composio.json'))
    cfg = json.loads(config_path.read_text())
    if os.environ.get('LEADMAGNET_CHECK_ONLY') == 'true':
        transport = InstagramTransport(ConsumerMCP(os.environ.get('COMPOSIO_CONSUMER_API_KEY', '')))
        transport.start()
        comments = transport.comments(cfg['media_ids'])
        print(json.dumps({'mode': 'read_only', 'account_verified': True, 'comments_read': len(comments)}))
        return 0
    if cfg.get('enabled') is not True:
        print('Lead-magnet automation is disabled; no messages sent.')
        return 0
    now = datetime.now(timezone.utc)
    enabled = datetime.fromisoformat(cfg['enabled_since'].replace('Z', '+00:00'))
    if enabled.tzinfo is None or enabled > now:
        raise RuntimeError('Activation date is missing, naive or in the future')
    if not cfg.get('media_ids'):
        raise RuntimeError('No approved published Instagram media configured')
    env = {name: os.environ.get(name, '') for name in (
        'COMPOSIO_CONSUMER_API_KEY', 'LEADMAGNET_HASH_KEY', 'GITHUB_TOKEN', 'GITHUB_REPOSITORY')}
    if any(not value for value in env.values()):
        raise RuntimeError('Required protected setting is missing')
    if len(env['LEADMAGNET_HASH_KEY']) < 32:
        raise RuntimeError('State hash key is too short')
    key_id = hashlib.sha256(env['LEADMAGNET_HASH_KEY'].encode()).hexdigest()
    store = GitHubStore(env['GITHUB_REPOSITORY'], env['GITHUB_TOKEN'], hash_key_id=key_id)
    if os.environ.get('LEADMAGNET_EVENT_DRIVEN') == 'true' or cfg.get('webhook_only') is True:
        return process_event_queue(cfg, env, enabled, now, store)
    # Existing Railway heartbeat calls this workflow every two minutes. Throttle
    # before Composio initialization to keep a free pilot within a bounded cadence.
    snapshot = store.load()
    last = snapshot.get('last_poll_started_at')
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=cfg.get('poll_minutes', 10)):
        print('Next scheduled check is not due; no Composio calls made.')
        return 0
    verify_material_urls(cfg['entries'])
    snapshot['last_poll_started_at'] = now.isoformat()
    store.save(snapshot)
    transport = InstagramTransport(ConsumerMCP(env['COMPOSIO_CONSUMER_API_KEY']))
    transport.start()
    comments = transport.comments(cfg['media_ids'])
    engine = LeadMagnetEngine(EngineConfig(entries=cfg['entries'], enabled_since=enabled,
        allowlisted_media_ids=frozenset(cfg['media_ids']), hash_key=env['LEADMAGNET_HASH_KEY'].encode()),
        transport, store)
    counters = engine.run(comments, now)
    counters.update({'instagram_operations_attempted': transport.instagram_operations_attempted,
                     'proxy_calls': transport.proxy_calls,
                     'mcp_http_requests': transport.mcp.http_requests,
                     'mcp_tool_invocations': transport.mcp.tool_invocations})
    print(json.dumps(counters))
    if counters['uncertain'] or counters['errors']:
        raise RuntimeError('Some deliveries need operator review; automatic resend is disabled')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # Do not print arbitrary exception strings which could contain request URLs/tokens.
        detail = str(exc) if isinstance(exc, (RuntimeError, TransportError)) else type(exc).__name__
        print('Lead-magnet run stopped safely: ' + detail + '. No blind retries.', file=sys.stderr)
        sys.exit(1)
