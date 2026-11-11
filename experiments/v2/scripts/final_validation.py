"""Consolidate final evidence and verify running source/assets against this checkout."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import httpx

ROOT = Path(__file__).resolve().parents[3]
REPORTS = ROOT/'experiments/v2/reports'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    rows = []
    numeric = ('memory','turns','graph','jobs','deployment','task_http','frontend')
    named = ('legacy','regression',*numeric,'mcp_protocol','model_capability','roles','restart',
             'embedding_ablation','storage_retrieval','review_validation')
    for name in named:
        path = REPORTS/f'stage6_{name}.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        mismatches, matched = [], 0
        for key in ('source_hashes','source_sha256','test_hashes'):
            for relative, expected in data.get(key, {}).items():
                source = ROOT/relative
                if not source.is_file() or sha(source) != expected:
                    mismatches.append(relative)
                else:
                    matched += 1
        if name in ('legacy','regression'):
            passed = data['success'] and data['skipped']==0
        elif name in numeric:
            passed = data['total']>0 and data['passed']==data['total']
        elif name=='embedding_ablation':
            passed = data['completed']
        elif name=='storage_retrieval':
            passed = data['storage_passed']==data['storage_total']>0
        else:
            passed = data.get('passed', data.get('success')) is True
        rows.append({'report': path.name, 'sha256':sha(path), 'passed':passed,
                     'tests':data.get('tests',data.get('total',data.get('storage_total'))),
                     'source_fingerprints_matched':matched, 'source_mismatches':mismatches})
    gate = json.loads((REPORTS/'stage6_review_independent_gate.json').read_text(encoding='utf-8'))
    independent_rejected = gate['passed'] is False and gate['approved_count']==0
    files = sorted(p.relative_to(ROOT).as_posix() for folder in ('api','core','agents','rag','memory','tooling','workflows','monitor','evaluation')
                   for p in (ROOT/folder).glob('*.py'))
    files += sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'memory/migrations').glob('*.sql'))
    files += ['requirements.txt']
    code = 'import json,sys,hashlib,pathlib; print(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in json.load(sys.stdin)}))'
    actual = json.loads(subprocess.check_output(['docker','compose','exec','-T','hellohebut','python','-c',code],
                        cwd=ROOT,input=json.dumps(files),text=True,encoding='utf-8'))
    mismatches = [p for p in files if sha(ROOT/p)!=actual[p]]
    assets = []
    with httpx.Client(base_url='http://localhost:8088',timeout=10) as client:
        client.get('/api/python/ready').raise_for_status()
        for path in sorted((ROOT/'frontend/dist').rglob('*')):
            if path.is_file():
                relative = '/' + path.relative_to(ROOT/'frontend/dist').as_posix()
                response = client.get(relative); response.raise_for_status()
                assets.append({'path':relative,'sha256':sha(path),
                               'served_bytes_match':hashlib.sha256(response.content).hexdigest()==sha(path)})
    pip = subprocess.run(['docker','compose','exec','-T','hellohebut','python','-m','pip','check'],cwd=ROOT,capture_output=True,text=True)
    report = {'mode':'stage6_final_validation', 'checked_at_utc':datetime.now(timezone.utc).isoformat(),
        'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'note':'Report source fingerprints identify the tested uncommitted implementation; this HEAD precedes the delivery commit.',
        'reports':rows, 'independent_review_gate_rejects_unapproved_drafts':independent_rejected,
        'runtime_source_count':len(files),'runtime_source_mismatches':mismatches,'runtime_source_sha256':actual,
        'frontend_assets':assets,'container_dependencies_consistent':pip.returncode==0,
        'build_mode':'reused_local_base',
        'remaining_acceptance':['Independent human references and Judge calibration',
            'Real Langfuse project connection','Browser visual inspection',
            'Clean standard Docker build blocked by configured mirror metadata validation'],
        'limitations':['Functional checks and development retrieval comparisons are not independent accuracy results.',
            'Anonymous identity expires after 24h; graph/job/chat records have no scheduled retention cleanup.',
            'Checkpoint recovery can repeat an interrupted external call; MySQL turn commit is idempotent.']}
    report['passed'] = all(r['passed'] and not r['source_mismatches'] for r in rows) and not mismatches and all(a['served_bytes_match'] for a in assets) and independent_rejected and pip.returncode==0
    (REPORTS/'stage6_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'reports':len(rows),'runtime_files':len(files),
                      'source_mismatches':{r['report']:r['source_mismatches'] for r in rows if r['source_mismatches']},
                      'runtime_mismatches':mismatches,'assets':len(assets)}))
    return 0 if report['passed'] else 1

if __name__=='__main__':
    raise SystemExit(main())
