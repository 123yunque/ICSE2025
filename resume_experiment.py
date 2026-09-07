"""Resume unchanged first-answer experiment after independently verified service recovery."""
import hashlib
import os
import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import lcb_experiment as experiment
from lcb_analyze import analyze
from granularity3_local.decomposed_core import read_jsonl

RUN = Path('runs/fresh100_20260906')
parser = argparse.ArgumentParser()
parser.add_argument('--recovery', default='runs/recovery_check_20260907')
args = parser.parse_args()
RECOVERY = Path(args.recovery)


def resume():
    certificate = experiment.load(RECOVERY / 'certificate.json')
    config = experiment.load(RUN / 'config.json')
    if not certificate['passed'] or certificate['model'] != config['model'] or certificate['api_base_url'] != config['api_base_url']:
        raise RuntimeError('A matching successful independent recovery certificate is required')
    root = Path(__file__).resolve().parent
    frozen = experiment.load(RUN / 'execution_source.json')
    for name, digest in frozen['files'].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Frozen execution source changed: ' + name)
    original_responses = read_jsonl(RUN / 'control_flow/model_responses.jsonl')
    audit_path = RUN / 'resumption_audit.json'
    if not audit_path.exists():
        experiment.save(audit_path, {'resumed_at': datetime.now(timezone.utc).isoformat(),
            'recovery_certificate': certificate, 'original_control_pilot_gate': experiment.load(RUN / 'pilot/control_flow_gate.json'),
            'preserved_control_response_ids': [r['request_id'] for r in original_responses],
            'preserved_response_sha256': experiment.sha(original_responses),
            'reason': 'Continue after independent service-format revalidation; original failed pilot is not rescored or replaced.',
            'interpretation': 'Exploratory continuation across service epochs; preserve all-request denominators and report epoch-specific coverage.'})
    stopped = threading.Event()
    original_factory = experiment.create_http_compatible_client
    def guarded_factory(key, base):
        client = original_factory(key, base)
        def create(**kwargs):
            if stopped.is_set():
                raise RuntimeError('Request not dispatched: service circuit open after authentication/quota rejection')
            try:
                return client.chat.completions.create(**kwargs)
            except Exception as exc:
                if 'HTTP 401' in str(exc) or 'HTTP 403' in str(exc):
                    stopped.set()
                raise
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    experiment.create_http_compatible_client = guarded_factory
    def stage(name, function):
        experiment.save(RUN / 'progress.json', {'stage': name, 'status': 'running', 'updated_at': datetime.now(timezone.utc).isoformat()})
        print('STAGE ' + name, flush=True)
        try:
            function()
            if stopped.is_set():
                raise RuntimeError('Service quota/authentication rejection; pending requests were not dispatched')
        except Exception as exc:
            experiment.save(RUN / 'progress.json', {'stage': name, 'status': 'stopped', 'reason': str(exc), 'updated_at': datetime.now(timezone.utc).isoformat()})
            raise
    def full(kind):
        for attempt in range(2):
            experiment.infer(RUN, kind, 3)
            if stopped.is_set():
                return
            summary = experiment.load(RUN / kind / 'summary.json')
            if summary['response_count'] == summary['selected_request_count']:
                return
        raise RuntimeError(kind + ' still has unreceived requests after bounded recovery; all first answers retained')
    stage('control_flow_resumed', lambda: full('control_flow'))
    # Ensure all existing replies survived byte-equivalent JSON record restoration.
    current = {r['request_id']: r for r in read_jsonl(RUN / 'control_flow/model_responses.jsonl')}
    if any(current[r['request_id']] != r for r in original_responses):
        raise RuntimeError('Existing first-answer records changed')
    for kind in ['oracle_state', 'predicted_state']:
        if kind == 'predicted_state':
            stage('prepare_predicted_state', lambda: experiment.predicted(RUN))
        stage(kind + '_pilot', lambda kind=kind: experiment.infer(RUN, kind, 3, pilot=True))
        stage(kind, lambda kind=kind: full(kind))
    stage('repeat_state', lambda: full('repeat_state'))
    summary = analyze(RUN)
    experiment.save(RUN / 'progress.json', {'stage': 'complete' if summary['complete'] else 'incomplete',
        'status': 'complete' if summary['complete'] else 'stopped', 'updated_at': datetime.now(timezone.utc).isoformat()})


if __name__ == '__main__':
    resume()
