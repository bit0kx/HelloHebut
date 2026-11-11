"""Separate frozen benchmark identity from changing implementation/run fingerprints."""
from dataclasses import asdict, is_dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def benchmark_manifest(intent_cases, dialog_cases, *, knowledge_before=None, knowledge_after=None):
    cases = {'intent': [asdict(c) if is_dataclass(c) else c for c in intent_cases or []], 'dialog': dialog_cases or []}
    csv_path = Path(os.getenv('ADMISSION_DATA_PATH', str(ROOT/'data/hebut_admission.csv')))
    csv_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    benchmark = {'cases_sha256': digest(cases), 'scoring_version': 'evaluator-hard-assertions-v2', 'scoring_sha256': hashlib.sha256((ROOT/'evaluation/evaluator.py').read_bytes()).hexdigest(),
                 'reference_data_sha256': csv_hash,
                 'judge_model': os.getenv('EVAL_JUDGE_MODEL', os.getenv('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')),
                 'label_provenance': 'developer references; human independent review not claimed'}
    files = [p for folder in ('api','core','agents','memory','rag','tooling','workflows') for p in (ROOT/folder).glob('*.py')]
    files += list((ROOT/'skills').rglob('*.md'))
    versions = {}
    for package in ('langgraph','langgraph-checkpoint','mcp','pydantic','asyncmy','anthropic'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'not-installed'
    return {'benchmark_id': digest(benchmark), 'benchmark': benchmark, 'run_manifest': {
        'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)},
        'admission_data_sha256': csv_hash,
        'knowledge_before': knowledge_before, 'knowledge_after': knowledge_after,
        'knowledge_stable': knowledge_before is not None and knowledge_before == knowledge_after,
        'embedding': {'implementation':'chromadb.utils.embedding_functions.ONNXMiniLM_L6_V2', 'dimensions':384},
        'model': os.getenv('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022'), 'python': platform.python_version(),
        'dependencies': versions, 'lexical': os.getenv('RAG_LEXICAL_MODE','filtered_legacy'),
        'fusion': os.getenv('RAG_FUSION_MODE','balanced'), 'role_mode': os.getenv('HELLOHEBUT_ROLE_MODE','single'),
        'graph': os.getenv('HELLOHEBUT_CHAT_GRAPH','true'), 'tool_loop': os.getenv('HELLOHEBUT_TOOL_LOOP','false')}}
