"""Pin upstream judge, license and errata without silently changing the checker."""
import hashlib
import json
from pathlib import Path
import requests

root = Path(__file__).resolve().parent / 'vendor'
response = requests.get('https://api.github.com/repos/LiveCodeBench/LiveCodeBench/commits/main', timeout=30)
revision = response.json()['sha'] if response.ok else 'main'
resolution_status = 'commit_pinned' if response.ok else 'content_hash_pinned; GitHub commit API HTTP ' + str(response.status_code)
records = {}
for name in ['lcb_runner/evaluation/testing_util.py', 'LICENSE', 'ERRATA.md']:
    url = 'https://raw.githubusercontent.com/LiveCodeBench/LiveCodeBench/' + revision + '/' + name
    response = requests.get(url, timeout=40)
    response.raise_for_status()
    dest = root / name.rsplit('/', 1)[-1]
    if dest.exists() and dest.name == 'testing_util.py':
        original = dest.read_text(encoding='utf-8-sig').strip()
        upstream = response.content.decode('utf-8-sig').replace('\r\n', '\n').strip()
        if original != upstream:
            (root / 'testing_util_upstream.py').write_bytes(response.content)
            raise ValueError('Upstream checker changed semantically; inspect before replacing')
    else:
        dest.write_bytes(response.content)
    records[name] = {'url': url, 'sha256': hashlib.sha256(dest.read_bytes()).hexdigest(),
                     'upstream_sha256': hashlib.sha256(response.content).hexdigest()}
(root / 'SOURCE.json').write_text(json.dumps({'revision': revision, 'resolution_status': resolution_status, 'files': records}, indent=2), encoding='utf-8')
print(json.dumps({'revision': revision, 'files': list(records)}), flush=True)
