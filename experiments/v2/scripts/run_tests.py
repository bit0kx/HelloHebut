"""Run offline regression suites and record a reproducible, credential-free report."""
import argparse
import hashlib
import json
import logging
import importlib.metadata
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', default='experiments/v2/tests')
    parser.add_argument('--report', default='experiments/v2/reports/regression.json')
    args = parser.parse_args()
    report = ROOT / args.report
    report.parent.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.discover(args.suite)
    started = time.monotonic()
    with report.with_suffix('.log').open('w', encoding='utf-8') as stream:
        logging.basicConfig(level=logging.WARNING, handlers=[logging.StreamHandler(stream)], force=True)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    sources = sorted(p for folder in ('api', 'core', 'mcp', 'memory', 'rag', 'agents')
                     for p in (ROOT / folder).glob('*.py'))
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sources}
    payload = {
        'mode': 'offline_regression', 'suite': args.suite,
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'python': platform.python_version(), 'source_hashes': hashes,
        'dependencies': {name: importlib.metadata.version(name) for name in ('anthropic', 'redis', 'pydantic', 'fastapi', 'jsonschema')},
        'test_hashes': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted((ROOT / args.suite).glob('*.py'))},
        'data_sha256': hashlib.sha256((ROOT / 'data/hebut_admission.csv').read_bytes()).hexdigest(),
        'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'duration_s': round(time.monotonic() - started, 3),
        'failed_tests': [str(t) for t, _ in result.failures + result.errors],
        'skip_reasons': [{'test': str(t), 'reason': why} for t, why in result.skipped],
        'success': result.wasSuccessful(),
        'limitations': 'Offline assertions are not live-model answer-quality measurements.',
    }
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    logging.getLogger().handlers.clear()
    print(json.dumps({k: payload[k] for k in ('tests', 'failures', 'errors', 'skipped', 'success')}))
    if not result.wasSuccessful():
        print(report.with_suffix('.log').read_text(encoding='utf-8')[-12000:])
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
