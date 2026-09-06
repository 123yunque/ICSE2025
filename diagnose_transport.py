"""Independent synthetic transport probes; never replace experiment first answers."""
import json
import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from lcb_experiment import create_http_compatible_client, DEFAULT_API_BASE_URL, save

root = Path('runs/transport_diagnostic_20260906')
parser = argparse.ArgumentParser()
parser.add_argument('--retry-errors', action='store_true', help='Retry only previously unreceived synthetic probes; archive errors')
args = parser.parse_args()
instruction = 'Return exactly this JSON object: {"transport_probe":"violet_731"}. Do not compute or return a mathematical answer.'
question = 'What is 2 + 2?'
cases = [
    ('system_json', [{'role': 'system', 'content': instruction}, {'role': 'user', 'content': question}], True),
    ('system_plain', [{'role': 'system', 'content': instruction}, {'role': 'user', 'content': question}], False),
    ('developer_json', [{'role': 'developer', 'content': instruction}, {'role': 'user', 'content': question}], True),
    ('user_only_json', [{'role': 'user', 'content': instruction + '\n\nBackground question: ' + question}], True),
]
client = create_http_compatible_client(os.environ['YUNWU_API_KEY'], os.getenv('YUNWU_API_BASE_URL', DEFAULT_API_BASE_URL))

def probe(case):
    name, messages, json_mode = case
    path = root / (name + '.json')
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        if not (args.retry_errors and previous.get('status') == 'error'):
            print(json.dumps(previous, ensure_ascii=False), flush=True)
            return
        save(root / 'history' / (name + '_' + str(time.time_ns()) + '.json'), previous)
    config = {'model': 'gpt-5.4', 'reasoning_effort': 'low', 'verbosity': 'low', 'max_completion_tokens': 256}
    if json_mode:
        config['response_format'] = {'type': 'json_object'}
    record = {'name': name, 'messages': messages, 'config': config}
    started = time.monotonic()
    try:
        result = client.chat.completions.create(**config, messages=messages, timeout=60)
        record.update(response=result.choices[0].message.content, finish_reason=result.choices[0].finish_reason,
                      model=getattr(result, 'model', None), status='received')
    except Exception as exc:
        record.update(status='error', reason=str(exc)[:800])
    record['elapsed_seconds'] = time.monotonic() - started
    save(path, record)
    print(json.dumps(record, ensure_ascii=False), flush=True)

with ThreadPoolExecutor(2) as pool:
    list(pool.map(probe, cases))
