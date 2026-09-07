"""Persist an evidence-based stop after a resumed service epoch."""
import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
from lcb_experiment import load, save
from granularity3_local.decomposed_core import read_jsonl

parser = argparse.ArgumentParser()
parser.add_argument('--run', default='runs/fresh100_20260906')
parser.add_argument('--recovery', default='runs/recovery_check_20260907')
parser.add_argument('--stage', default=None)
args = parser.parse_args()
run = Path(args.run)
out = Path('reports') / run.name
recovery = Path(args.recovery)
counts = {}
def error_bucket(row):
    reason = str(row.get('reason', ''))
    if 'not dispatched' in reason.lower():
        return 'not_dispatched_circuit_open'
    if 'HTTP 429' in reason:
        return 'http_429'
    if 'HTTP 403' in reason:
        return 'http_403_quota'
    if 'HTTP 401' in reason:
        return 'http_401_quota'
    if 'hard timeout' in reason:
        return 'client_timeout'
    return str(row.get('error_type') or 'other')

for kind in ['control_flow', 'oracle_state', 'predicted_state', 'repeat_state']:
    requests = read_jsonl(run / 'prepared' / kind / 'requests.jsonl')
    responses = read_jsonl(run / kind / 'model_responses.jsonl')
    attempts = read_jsonl(run / kind / 'api_attempts.jsonl')
    counts[kind] = {'expected': len(requests), 'received': len(responses),
        'attempt_status': dict(Counter(r.get('status') for r in attempts)),
        'attempt_errors': dict(Counter(error_bucket(r) for r in attempts if r.get('status') != 'received')),
        'received_format': dict(Counter(r.get('validation') for r in attempts if r.get('status') == 'received'))}
progress = load(run / 'progress.json') if (run / 'progress.json').exists() else {}
stage = args.stage or progress.get('stage', 'unknown')
record = {'stage': stage, 'status': 'stopped',
    'reason': ('服务返回 HTTP 403 local:pre_consume_token_quota_failed：剩余额度不足以预扣下一次请求。'
               '保护电路停止后续网络派发；排队项未发送。'),
    'quota_observation': {'remaining_usd': 0.009354, 'next_request_preconsume_usd': 0.009626,
                          'source': 'HTTP 403 response from the configured API service'},
    'updated_at': datetime.now(timezone.utc).isoformat(), 'response_counts': counts,
    'completed_conditions': [k for k, v in counts.items() if v['received'] == v['expected']],
    'incomplete_conditions': [k for k, v in counts.items() if v['received'] != v['expected']],
    'recovery_check': load(recovery / 'certificate.json'),
    'resume_rules': ['Retain all received first answers and frozen denominators.',
                     'After restoring external service quota, run a new independent recovery epoch.',
                     'Do not count circuit-blocked attempts as received model answers.',
                     'Continue only missing request IDs under the unchanged configuration.']}
save(run / 'progress.json', record)
save(out / 'STOP_20260907.json', record)
save(out / 'recovery_certificate_20260907.json', record['recovery_check'])

epochs = []
for recovery_dir in sorted(Path('runs').glob('recovery_check_20260907*')):
    certificate = recovery_dir / 'certificate.json'
    if certificate.exists():
        epochs.append({'recovery_run': str(recovery_dir), 'certificate': load(certificate)})
for kind in ['control_flow', 'oracle_state', 'predicted_state', 'repeat_state']:
    path = run / kind / 'model_responses.jsonl'
    rows = read_jsonl(path)
    epochs.append({'main_condition': kind, 'received': len(rows),
                   'responses_sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None})
service_epochs = {'schema_version': 'service-epochs-v1', 'updated_at': record['updated_at'],
                  'epochs': epochs, 'latest_stop': {'stage': stage, 'reason': record['reason']}}
save(run / 'service_epochs.json', service_epochs)
save(out / 'service_epochs.json', service_epochs)
print(counts)
