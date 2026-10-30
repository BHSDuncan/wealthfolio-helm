"""Private in-memory synthetic fixtures; never log response bodies or credentials."""
import datetime
import hashlib
import http.client
import http.cookiejar
import json
import secrets
import urllib.error
import urllib.request


class Client:
    def __init__(self, url, password):
        self.url = url
        self.origin = url
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.scope = None

    def request(self, method, path, body=None, expected=200, authenticated=True, headers=None):
        h = {'Origin': self.origin}
        if body is not None:
            h['Content-Type'] = 'application/json'
        if self.scope and authenticated:
            h['x-wf-profile-scope'] = self.scope
        h.update(headers or {})
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
        opener = self.opener if authenticated else urllib.request.build_opener()
        try:
            response = opener.open(req, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        data = response.read()
        assert response.code == expected, f'{method} {path.split("?")[0]} expected {expected}; received {response.code} (body withheld)'
        try:
            value = json.loads(data) if data else None
        except json.JSONDecodeError:
            value = data.decode()
        return value, response.headers

    def login(self):
        self.scope = None
        data, headers = self.request('POST', '/api/v1/auth/login', {'password': self.password})
        assert data['authenticated'] is True
        cookie = headers.get('Set-Cookie', '')
        assert 'wf_session=' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie

    def unlock(self, profile_id):
        self.scope = None
        session, _ = self.request('POST', '/api/v1/profiles/unlock_profile', {'profileId': profile_id})
        assert session['profileId'] == profile_id
        self.scope = session['scopeId']

    def seed(self, encrypted=False):
        self.login()
        status, _ = self.request('GET', '/api/v1/agent-access/status')
        assert status['mcpEnabled'] is False
        state, _ = self.request('POST', '/api/v1/profiles/get_profile_state', {})
        assert state['profiles'], 'Expected fresh default profile'
        default_id = state['session']['profileId']
        second, _ = self.request('POST', '/api/v1/profiles/create_profile', {'name': 'Chart synthetic B', 'avatarId': 'default'})
        expected = []
        for index, profile_id in enumerate((default_id, second['id'])):
            self.unlock(profile_id)
            settings, _ = self.request('GET', '/api/v1/settings')
            assert settings.get('baseCurrency', '') == '', 'Only fresh empty synthetic profiles may be seeded'
            self.request('PUT', '/api/v1/settings', {'baseCurrency': 'USD', 'timezone': 'UTC', 'onboardingCompleted': True, 'syncEnabled': False, 'autoUpdateCheckEnabled': False})
            settings, _ = self.request('GET', '/api/v1/settings')
            assert settings['baseCurrency'] == 'USD'
            name = 'Chart synthetic ' + str(index)
            account, _ = self.request('POST', '/api/v1/accounts', {'name': name, 'accountType': 'CASH', 'currency': 'USD', 'isDefault': False, 'isActive': True, 'trackingMode': 'TRANSACTIONS'})
            marker = secrets.token_urlsafe(32)
            self.request('POST', '/api/v1/secrets', {'secretKey': 'chart-test-marker', 'secret': marker}, expected=204)
            expected.append({'profile': profile_id, 'account': account['id'], 'name': name, 'marker': marker})
        self.verify(expected, encrypted, login=False)
        return expected

    def verify(self, expected, encrypted, login=True):
        health, _ = self.request('GET', '/api/v1/healthz', authenticated=False)
        ready, _ = self.request('GET', '/api/v1/readyz', authenticated=False)
        assert health == ready == 'ok'
        if login:
            self.login()
        state, _ = self.request('POST', '/api/v1/profiles/get_profile_state', {})
        assert {e['profile'] for e in expected} <= {p['id'] for p in state['profiles']}
        for item in expected:
            self.unlock(item['profile'])
            accounts, _ = self.request('GET', '/api/v1/accounts')
            assert any(a['id'] == item['account'] and a['name'] == item['name'] and a['currency'] == 'USD' for a in accounts)
            marker, _ = self.request('GET', '/api/v1/secrets?secretKey=chart-test-marker')
            assert secrets.compare_digest(marker, item['marker']), 'Vault readback mismatch (values withheld)'
            policy, _ = self.request('GET', '/api/v1/utilities/database/encryption')
            assert policy['enabled'] is encrypted and policy['supported'] is False

    def network(self):
        self.request('GET', '/api/v1/accounts', expected=401, authenticated=False)
        self.request('POST', '/api/v1/auth/login', {'password': 'intentionally-incorrect-synthetic-password'}, expected=401, authenticated=False)
        health, headers = self.request('GET', '/api/v1/healthz', authenticated=False)
        assert health == 'ok'
        ready, _ = self.request('GET', '/api/v1/readyz', authenticated=False)
        assert ready == 'ok'
        assert headers.get('Access-Control-Allow-Origin') == self.origin
        assert headers.get('Access-Control-Allow-Credentials') == 'true'
        _, headers = self.request('GET', '/api/v1/healthz', authenticated=False, headers={'Origin': 'https://disallowed.example.test'})
        assert headers.get('Access-Control-Allow-Origin') != 'https://disallowed.example.test'
        html, _ = self.request('GET', '/', authenticated=False)
        assert '<html' in html.lower()
        self.request('POST', '/mcp', {}, expected=405, authenticated=False)

    def mcp(self, fixture, audit=True):
        self.login()
        self.unlock(fixture[0]['profile'])
        state, _ = self.request('GET', '/api/v1/agent-access/status')
        assert state == {'mcpEnabled': True, 'auditEnabled': audit, 'endpoint': '/mcp'}
        previous, _ = self.request('GET', '/api/v1/agent-access/tokens')
        for item in previous:
            if item['name'] == 'chart-synthetic-read' and not item.get('revokedAt'):
                self.request('DELETE', '/api/v1/agent-access/tokens/' + item['id'], expected=204)
        self.request('POST', '/api/v1/agent-access/tokens', {'name': 'invalid-scopes', 'scopes': ['unknown:read']}, expected=400)
        token, _ = self.request('POST', '/api/v1/agent-access/tokens', {'name': 'chart-synthetic-read', 'expiresAt': (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat(), 'scopes': ['accounts:read']}, expected=201)
        raw_token = token.pop('token')
        before, _ = self.request('GET', '/api/v1/agent-access/audit')
        protocol = MCP(self.url, raw_token)
        MCP(self.url, None).call('initialize', {}, expected=401)
        protocol.call('initialize', {}, host='unlisted.example.test', expected=403)
        protocol.call('initialize', {}, omit_host=True, expected=400)
        initialized = protocol.call('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'wealthfolio-chart-test', 'version': '0.1.0'}})
        assert initialized['result']['protocolVersion'] == '2025-11-25'
        protocol.call('notifications/initialized', None, notification=True, expected=202)
        tools = protocol.call('tools/list', {})
        assert any(t['name'] == 'get_accounts' for t in tools['result']['tools'])
        result = protocol.call('tools/call', {'name': 'get_accounts', 'arguments': {}})
        result_text = json.dumps(result)
        assert fixture[0]['account'] in result_text and fixture[1]['account'] not in result_text
        protocol.call('tools/call', {'name': 'get_accounts', 'arguments': {}}, headers={'X-WF-Profile-Id': fixture[1]['profile']}, expected=401)
        before_activities, _ = self.request('POST', '/api/v1/activities/search', {'page': 1, 'pageSize': 100})
        denied = protocol.call('tools/call', {'name': 'commit_activity_draft', 'arguments': {'draft': {'activityType': 'DEPOSIT', 'activityDate': '2026-10-30', 'currency': 'USD', 'accountId': fixture[0]['account'], 'amount': 1}}})
        assert denied['result']['isError'] is True
        assert 'scope' in json.dumps(denied).lower()
        after_activities, _ = self.request('POST', '/api/v1/activities/search', {'page': 1, 'pageSize': 100})
        assert before_activities == after_activities, 'Unauthorized MCP tool changed activities'
        after, _ = self.request('GET', '/api/v1/agent-access/audit')
        if audit:
            assert after['totalCount'] > before['totalCount']
            assert any(e['outcome'].lower() == 'denied' for e in after['items'])
        else:
            assert after['totalCount'] == before['totalCount']
        self.request('DELETE', '/api/v1/agent-access/tokens/' + token['id'], expected=204)
        protocol.call('tools/list', {}, expected=401)
        self.verify(fixture, encrypted=False, login=False)


class MCP:
    """Small streamable HTTP client supporting finite JSON/SSE RPC responses."""
    def __init__(self, url, token):
        from urllib.parse import urlparse
        self.target = urlparse(url)
        self.token = token
        self.session = None
        self.counter = 0

    def call(self, method, params, expected=200, notification=False, host=None, omit_host=False, headers=None):
        self.counter += 1
        body = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            body['id'] = self.counter
        if params is not None:
            body['params'] = params
        connection = http.client.HTTPConnection(self.target.hostname, self.target.port, timeout=30)
        encoded = json.dumps(body).encode()
        connection.putrequest('POST', '/mcp', skip_host=True)
        if not omit_host:
            connection.putheader('Host', host or self.target.netloc)
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Accept', 'application/json, text/event-stream')
        connection.putheader('Content-Length', str(len(encoded)))
        connection.putheader('MCP-Protocol-Version', '2025-11-25')
        if self.token:
            connection.putheader('Authorization', 'Bearer ' + self.token)
        if self.session:
            connection.putheader('Mcp-Session-Id', self.session)
        for key, value in (headers or {}).items():
            connection.putheader(key, value)
        connection.endheaders(encoded)
        try:
            response = connection.getresponse()
            assert response.status == expected, f'MCP {method}: expected {expected}; received {response.status} (body withheld)'
            self.session = response.getheader('mcp-session-id') or self.session
            if expected != 200:
                return None
            if 'text/event-stream' in response.getheader('Content-Type', ''):
                chunks = []
                for _ in range(1000):
                    line = response.readline()
                    if not line:
                        break
                    if line.startswith(b'data:'):
                        chunks.append(line[5:].lstrip(b' ').rstrip(b'\r\n'))
                    elif line in (b'\n', b'\r\n') and chunks:
                        data = b'\n'.join(chunks).strip()
                        chunks = []
                        if data:
                            return json.loads(data)
                raise AssertionError('No MCP SSE result')
            return json.loads(response.read())
        finally:
            connection.close()
