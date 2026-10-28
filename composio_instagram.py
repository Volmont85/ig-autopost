"""Composio consumer MCP transport. Credentials remain server-side.

Only the existing Connect MCP endpoint is used, not developer-project APIs.
No network writes are retried. Responses/credentials are never logged.
"""
import json
import re
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class TransportError(RuntimeError):
    pass


def decode_rpc(body):
    text = body.decode() if isinstance(body, bytes) else body
    if text.lstrip().startswith('{'):
        return json.loads(text)
    events = []
    for event in text.replace('\r\n', '\n').split('\n\n'):
        data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
        if data:
            events.append(json.loads(data))
    responses = [e for e in events if 'result' in e or 'error' in e]
    if not responses:
        raise TransportError('MCP returned no result')
    return responses[-1]


def unwrap(value):
    """Unwrap MCP text blocks; do not log the original response."""
    for _ in range(8):
        if not isinstance(value, dict):
            break
        if value.get('isError') or value.get('successful') is False:
            raise TransportError('Composio rejected the operation')
        if isinstance(value.get('content'), list):
            texts = [x.get('text', '') for x in value['content'] if x.get('type') == 'text']
            if len(texts) != 1:
                raise TransportError('Unexpected MCP content')
            value = json.loads(texts[0])
        elif 'data' in value and ('successful' in value or 'success' in value):
            value = value['data']
        else:
            break
    return value


class ConsumerMCP:
    URL = 'https://connect.composio.dev/mcp'

    def __init__(self, key, opener=urlopen):
        if not key or not key.startswith('ck_'):
            raise TransportError('Missing Composio consumer key')
        self.key, self.opener, self.counter = key, opener, 0
        self.session_header = None
        self.tool_names = {}

    def rpc(self, method, params=None, notify=False):
        self.counter += 1
        payload = {'jsonrpc': '2.0', 'method': method}
        if not notify:
            payload['id'] = self.counter
        if params is not None:
            payload['params'] = params
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'x-consumer-api-key': self.key}
        if self.session_header:
            headers['Mcp-Session-Id'] = self.session_header
            headers['MCP-Protocol-Version'] = '2025-03-26'
        req = Request(self.URL, data=json.dumps(payload).encode(), headers=headers, method='POST')
        try:
            with self.opener(req, timeout=120) as response:
                sid = response.headers.get('Mcp-Session-Id')
                if sid:
                    self.session_header = sid
                raw = response.read()
        except HTTPError as exc:
            # Never include response headers/body or request secrets.
            raise TransportError(f'Composio HTTP {exc.code}') from None
        except Exception:
            raise TransportError('Composio request outcome unavailable') from None
        if notify:
            return None
        decoded = decode_rpc(raw)
        if 'error' in decoded:
            raise TransportError('MCP operation rejected')
        return decoded.get('result')

    def start(self):
        self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                'clientInfo': {'name': 'goszakupki-leadmagnet', 'version': '1.0'}})
        self.rpc('notifications/initialized', notify=True)
        cursor = None
        while True:
            page = self.rpc('tools/list', {'cursor': cursor} if cursor else {})
            for tool in page.get('tools', []):
                self.tool_names[tool['name'].lower()] = tool['name']
            cursor = page.get('nextCursor')
            if not cursor:
                break

    def call(self, suffix, arguments):
        names = [name for lower, name in self.tool_names.items() if lower.endswith(suffix.lower())]
        if len(names) != 1:
            raise TransportError('Required Composio tool not uniquely available')
        return unwrap(self.rpc('tools/call', {'name': names[0], 'arguments': arguments}))


class InstagramTransport:
    def __init__(self, mcp, expected_username='goszakupki.info', expected_id='17841410301724309'):
        self.mcp, self.expected_username, self.ig_id = mcp, expected_username, expected_id
        self.workflow_session = None
        self.verified = False

    def start(self):
        self.mcp.start()
        discovery = self.mcp.call('COMPOSIO_SEARCH_TOOLS', {
            'session': {'generate_id': True}, 'search_strategy': 'tool_search',
            'queries': [{'use_case': 'Instagram read comments and check user follow status; send a private comment reply using proxy_execute'}]})
        self.workflow_session = discovery['session']['id']
        connections = discovery.get('toolkit_connection_statuses', [])
        accounts = [a for t in connections if t.get('toolkit') == 'instagram'
                    for a in t.get('accounts', []) if a.get('status') == 'ACTIVE']
        if not any(a.get('user_info', {}).get('username') == self.expected_username for a in accounts):
            raise TransportError('Expected Instagram connection is not active')
        me = self.proxy('GET', 'me', {'fields': 'id,user_id,username'}, check=False)
        if me.get('username') != self.expected_username or str(me.get('user_id')) != self.ig_id:
            raise TransportError('Composio default account does not match goszakupki.info')
        self.verified = True

    def proxy(self, method, path, params=None, body=None, check=True):
        if check and not self.verified:
            raise TransportError('Instagram account has not been verified')
        if method not in ('GET', 'POST') or not re.fullmatch(r'[A-Za-z0-9_:/=+.-]+', path):
            raise TransportError('Unsupported Instagram request')
        # JSON encoded as a Python string literal prevents script interpolation.
        args = json.dumps({'method': method, 'endpoint': 'https://graph.instagram.com/v21.0/' + path,
                           'toolkit': 'instagram', 'query_params': params, 'body': body})
        code = ('import json\n'
                f'_lm_args = json.loads({args!r})\n'
                '_lm_result, _lm_error = proxy_execute(**_lm_args)\n'
                'print("_LM_JSON_" + json.dumps({"ok": not bool(_lm_error), "data": _lm_result if not _lm_error else None}, ensure_ascii=False))')
        out = self.mcp.call('COMPOSIO_REMOTE_WORKBENCH', {
            'session_id': self.workflow_session, 'current_step': 'LEADMAGNET_INSTAGRAM',
            'code_to_execute': code, 'thought': 'Run the authorized Instagram lead-magnet operation.'})
        marker_lines = [line[len('_LM_JSON_'):] for line in out.get('stdout', '').splitlines()
                        if line.startswith('_LM_JSON_')]
        if not marker_lines:
            raise TransportError('Instagram operation outcome unavailable')
        payload = json.loads(marker_lines[-1])
        if not payload.get('ok') or not isinstance(payload.get('data'), dict) or 'error' in payload['data']:
            raise TransportError('Instagram operation rejected or outcome unavailable')
        return payload['data']

    def get_follow_status(self, uid):
        if not str(uid).isdigit():
            raise TransportError('Invalid scoped user identifier')
        result = self.proxy('GET', str(uid), {'fields': 'is_user_follow_business'})
        value = result.get('is_user_follow_business')
        return value if type(value) is bool else None

    def private_reply(self, comment_id, text):
        result = self.proxy('POST', self.ig_id + '/messages', body={
            'recipient': {'comment_id': str(comment_id)}, 'message': {'text': text}})
        if not result.get('message_id'):
            raise TransportError('Private reply outcome unavailable')
        return result

    def send_message(self, uid, text):
        result = self.proxy('POST', self.ig_id + '/messages', body={
            'recipient': {'id': str(uid)}, 'message': {'text': text}})
        if not result.get('message_id'):
            raise TransportError('Message outcome unavailable')
        return result

    def comments(self, media_ids):
        all_comments = []
        for media_id in media_ids:
            after = None
            seen = set()
            while True:
                params = {'fields': 'id,text,timestamp,from', 'limit': 100}
                if after:
                    params['after'] = after
                page = self.proxy('GET', str(media_id) + '/comments', params)
                for c in page.get('data', []):
                    uid = str(c.get('from', {}).get('id', ''))
                    if uid and uid != self.ig_id:
                        all_comments.append({'id': c['id'], 'media_id': str(media_id),
                                             'user_id': uid, 'text': c.get('text', ''),
                                             'timestamp': c.get('timestamp')})
                paging = page.get('paging', {})
                after = paging.get('cursors', {}).get('after') if paging.get('next') else None
                if not after:
                    break
                if after in seen:
                    raise TransportError('Comment pagination did not advance')
                seen.add(after)
        return all_comments

    def recent_incoming(self, uid, since):
        page = self.proxy('GET', self.ig_id + '/conversations', {
            'platform': 'instagram', 'user_id': str(uid), 'fields': 'id,participants', 'limit': 25})
        rows = []
        for conv in page.get('data', []):
            participants = conv.get('participants', {}).get('data', [])
            if not any(str(p.get('id')) == str(uid) for p in participants):
                continue
            # Get only this pending user's conversation. Paginate until the time boundary.
            after, seen = None, set()
            while True:
                params = {'fields': 'id,created_time,from,message', 'limit': 100}
                if after:
                    params['after'] = after
                messages = self.proxy('GET', conv['id'] + '/messages', params)
                reached_old = False
                for m in messages.get('data', []):
                    stamp = datetime.fromisoformat(m['created_time'].replace('Z', '+00:00'))
                    if stamp < since:
                        reached_old = True
                    elif str(m.get('from', {}).get('id')) == str(uid):
                        rows.append({'id': m['id'], 'created_time': m['created_time'], 'text': m.get('message', '')})
                paging = messages.get('paging', {})
                after = paging.get('cursors', {}).get('after') if paging.get('next') else None
                if reached_old or not after:
                    break
                if after in seen:
                    raise TransportError('Message pagination did not advance')
                seen.add(after)
        return sorted(rows, key=lambda r: r['created_time'])
