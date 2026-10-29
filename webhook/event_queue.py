"""Encrypted durable spool in the existing GitHub repository.

GitHub can coalesce pending workflow runs. Persisting each event first keeps
coalescing safe: every worker drains the spool, not just its trigger payload.
Only ciphertext and keyed opaque filenames are stored in the public repo.
"""
import base64
import hashlib
import hmac
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    from .webhook_event import encrypt_events, decrypt_events, _key
except ImportError:
    from webhook_event import encrypt_events, decrypt_events, _key

PREFIX = 'leadmagnet/goszakupki/event_queue/'


class EventQueue:
    def __init__(self, repo, token, encryption_key, opener=urlopen):
        if repo != 'Volmont85/ig-autopost':
            raise ValueError('Unexpected queue repository')
        self.repo, self.token, self.key, self.opener = repo, token, encryption_key, opener
        _key(encryption_key)

    def request(self, method, path, data=None):
        req = Request('https://api.github.com/repos/' + self.repo + '/contents/' + path,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'Authorization': 'Bearer ' + self.token,
                               'Accept': 'application/vnd.github+json',
                               'Content-Type': 'application/json', 'User-Agent': 'ig-event-spool'}, method=method)
        with self.opener(req, timeout=20) as response:
            return json.load(response)

    def enqueue(self, event):
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        digest = hmac.new(_key(self.key), canonical, hashlib.sha256).hexdigest()
        path = PREFIX + digest + '.json'
        try:
            self.request('GET', path)
            return path
        except HTTPError as exc:
            if exc.code != 404:
                raise RuntimeError('Cannot inspect encrypted event spool') from None
        envelope = encrypt_events([event], self.key)
        payload = {'message': 'leadmagnet: encrypted event [skip ci]', 'branch': 'main',
                   'content': base64.b64encode(json.dumps(envelope, separators=(',', ':')).encode()).decode()}
        try:
            self.request('PUT', path, payload)
        except HTTPError as exc:
            if exc.code not in (409, 422):
                raise RuntimeError('Cannot persist encrypted event') from None
            # A concurrent duplicate is safe only if an authenticated envelope
            # exists and decrypts to exactly the same event.
            row = self.request('GET', path)
            existing = json.loads(base64.b64decode(row['content']))
            if decrypt_events(existing, self.key) != [event]:
                raise RuntimeError('Encrypted queue conflict') from None
        return path

    def pending(self):
        try:
            rows = self.request('GET', PREFIX.rstrip('/'))
        except HTTPError as exc:
            if exc.code == 404:
                return []
            raise RuntimeError('Cannot list encrypted event spool') from None
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise RuntimeError('Encrypted spool requires operator review')
        result = []
        for row in rows:
            path = row.get('path', '')
            name = path.removeprefix(PREFIX)
            if not (path.startswith(PREFIX) and len(name) == 69 and name.endswith('.json')
                    and all(c in '0123456789abcdef' for c in name[:-5])):
                continue
            item = self.request('GET', path)
            envelope = json.loads(base64.b64decode(item['content']))
            result.append((path, item['sha'], decrypt_events(envelope, self.key)))
        return result

    def acknowledge(self, path, sha):
        if not path.startswith(PREFIX) or '/' in path[len(PREFIX):]:
            raise ValueError('Invalid spool path')
        self.request('DELETE', path, {'message': 'leadmagnet: event acknowledged [skip ci]',
                                      'sha': sha, 'branch': 'main'})
