"""Exercise live preflight and saved-draft endpoints without executing automations."""
import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--url', required=True)
parser.add_argument('--key-file', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
key = args.key_file.read_text().strip()

def request(path, payload=None, method=None):
    req = urllib.request.Request(args.url + '/api/automation/v1' + path,
        data=None if payload is None else json.dumps(payload).encode(), method=method,
        headers={'Content-Type': 'application/json', 'X-Session-API-Key': key})
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        return response.status, json.loads(body) if body else None

draft = {'name': 'tags-live-probe', 'trigger': {'type': 'cron', 'schedule': '*/10 * * * *', 'timezone': 'UTC'}, 'entrypoint': 'python run.py', 'tarball_path': 'oh-internal://uploads/example'}
cases = [('omitted', {}), ('populated', {'tags': ['example-tag']}), ('empty', {'tags': []}), ('normalized', {'tags': [' Example Tag ', 'example-tag']}), ('invalid_slug', {'tags': ['Bad Tag!']}), ('too_many', {'tags': [f'tag-{i}' for i in range(11)]}), ('too_long', {'tags': ['x' * 51]}), ('unknown_field', {'unexpected': 'value'})]
evidence = []
for name, fields in cases:
    payload = {'endpoint': '/v1', 'draft': {**draft, **fields}}
    status, body = request('/validate', payload)
    evidence.append({'case': name, 'method': 'POST', 'path': '/v1/validate', 'request': payload, 'http_status': status, 'response': body})
    print(f'{name}: HTTP {status}; valid={body.get("valid")}; errors={json.dumps(body.get("errors"))}')
for endpoint in ['/v1/preset/prompt', '/v1/preset/plugin']:
    payload = {'endpoint': endpoint, 'draft': {'name': 'preset-probe', 'tags': []}}
    status, body = request('/validate', payload)
    evidence.append({'case': endpoint + '_tags', 'request': payload, 'http_status': status, 'response': body})
    print(f'{endpoint} tags: HTTP {status}; valid={body.get("valid")}; errors={json.dumps(body.get("errors"))}')
payload = {'endpoint': '/v1', 'draft': {'name': 'saved-tag-probe', 'tags': [' Example Tag ', 'example-tag']}}
status, body = request('/drafts', payload)
evidence.append({'case': 'saved_draft', 'request': payload, 'http_status': status, 'response': body})
if status == 201:
    draft_id = body['id']
    status, saved = request('/drafts/' + draft_id)
    evidence.append({'case': 'saved_draft_reload', 'http_status': status, 'response': saved})
    print(f'saved_draft: persisted tags={json.dumps(saved["draft"]["tags"])}')
    cleanup_status, _ = request('/drafts/' + draft_id, method='DELETE')
    assert cleanup_status in (200, 204), cleanup_status
else:
    print(f'saved_draft: HTTP {status}; {json.dumps(body)}')
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(evidence, indent=2) + '\n')
print('Live HTTP requests complete; no automation created or dispatched; any saved draft deleted.')
