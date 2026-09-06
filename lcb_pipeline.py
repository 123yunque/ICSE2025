"""Sequential experiment stages with durable progress and first-answer resume."""
import argparse
import hashlib
import importlib.metadata
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from lcb_experiment import build, prepare, infer, predicted, prepare_repeat, load, save
from lcb_analyze import analyze
from granularity3_local.decomposed_core import read_jsonl


def pipeline(run, data, skip_build=False):
    run = Path(run)
    def stage(name, function):
        save(run / 'progress.json', {'stage': name, 'status': 'running', 'updated_at': datetime.now(timezone.utc).isoformat()})
        print('STAGE ' + name, flush=True)
        try:
            result = function()
        except Exception as exc:
            save(run / 'progress.json', {'stage': name, 'status': 'stopped', 'reason': str(exc),
                                       'updated_at': datetime.now(timezone.utc).isoformat()})
            raise
        return result
    if not skip_build:
        stage('build', lambda: build(run, data))
    results = read_jsonl(run / 'build_results.jsonl')
    if len(results) != 100 or any(r['status'] == 'api_incomplete' for r in results):
        raise RuntimeError('Complete or resume the frozen 100-question build first')
    root = Path(__file__).resolve().parent
    files = sorted((root / 'granularity3_local').glob('*.py')) + [root / name for name in ['lcb_experiment.py', 'lcb_worker.py', 'vendor/testing_util.py']]
    hashes = {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    source_path = run / 'execution_source.json'
    if source_path.exists() and load(source_path)['files'] != hashes:
        raise RuntimeError('Execution source changed after freeze; audit before resuming')
    if not source_path.exists():
        save(source_path, {'files': hashes, 'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
              'python': sys.version, 'packages': {name: importlib.metadata.version(name) for name in ['numpy', 'requests', 'datasets']},
              'frozen_at': datetime.now(timezone.utc).isoformat()})
    stage('prepare', lambda: prepare(run, 3))
    # Freeze repeated-call selection before any execution-inference response is observed.
    stage('freeze_repeat', lambda: prepare_repeat(run))
    for kind in ['control_flow', 'oracle_state', 'predicted_state']:
        if kind == 'predicted_state':
            stage('prepare_predicted_state', lambda: predicted(run))
        stage(kind + '_pilot', lambda kind=kind: infer(run, kind, 3, pilot=True))
        stage(kind, lambda kind=kind: infer(run, kind, 3))
    stage('repeat_state', lambda: infer(run, 'repeat_state', 3))
    summary = stage('analyze', lambda: analyze(run, data))
    save(run / 'progress.json', {'stage': 'complete' if summary['complete'] else 'missing_responses',
        'status': 'complete' if summary['complete'] else 'stopped', 'updated_at': datetime.now(timezone.utc).isoformat()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', default='runs/fresh100_20260906')
    parser.add_argument('--data', default='data')
    parser.add_argument('--skip-build', action='store_true')
    args = parser.parse_args()
    pipeline(args.run, args.data, args.skip_build)
