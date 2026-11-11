"""Validate human review records and freeze explicitly labelled benchmark snapshots.

This validates declarations and local snapshot integrity, not human identity or
the truth of external sources. Draft/development cases never become independent
merely because their expected outputs were filled in.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
DATASETS = ROOT/'experiments/v2/datasets'
SCORING_VERSION = 'human-review-rubric-v1'
KINDS = {'answer', 'clarify', 'partial', 'refuse', 'handoff'}
REFERENCE_KINDS = {'official_policy', 'official_data', 'product_requirement', 'synthetic_fixture'}
SOURCE_ROOTS = {'data', 'docs', 'core', 'agents', 'memory', 'rag', 'tooling', 'workflows', 'skills'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_cases(path):
    cases = []
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                cases.append(json.loads(line))
    return cases


def timestamp(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def snapshot_path(value, root=ROOT):
    """Resolve only curated project documents, never .env, arbitrary files or tmp."""
    if not isinstance(value, str) or '\\' in value:
        raise ValueError('reference_path_must_be_relative_posix')
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts or ':' in value:
        raise ValueError('reference_path_outside_allowed_sources')
    allowed = (relative.parts[0] in SOURCE_ROOTS or value == 'AGENT_EVOLUTION_PLAN.md'
               or value.startswith(('experiments/v2/datasets/', 'experiments/v2/review_sources/', 'experiments/v2/tests/')))
    path = (root/value).resolve()
    if not allowed or any(part.startswith('.') for part in relative.parts) or not path.is_relative_to(root.resolve()):
        raise ValueError('reference_path_outside_allowed_sources')
    if not path.is_file() or path.stat().st_size > 8*1024*1024:
        raise ValueError('reference_snapshot_missing_or_too_large')
    return path


def validate_cases(cases, tier='development', root=ROOT):
    if tier not in {'development', 'independent'}:
        raise ValueError('invalid_benchmark_tier')
    errors, ids = [], set()
    def error(case_id, code):
        errors.append({'case_id': case_id, 'code': code})
    if not cases:
        error('dataset', 'empty_dataset')
    for ordinal, case in enumerate(cases):
        if not isinstance(case, dict):
            error(str(ordinal), 'case_must_be_object')
            continue
        case_id = case.get('id')
        if not isinstance(case_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,79}', case_id) or case_id in ids:
            error(str(ordinal), 'invalid_or_duplicate_case_id')
        ids.add(str(case_id))
        if case.get('schema_version') != 1 or not isinstance(case.get('family'), str) or not case['family'].strip():
            error(case_id, 'invalid_case_schema_or_family')
        if not isinstance(case.get('question'), str) or not case['question'].strip():
            error(case_id, 'question_required')
        review = case.get('review')
        expected = case.get('expected')
        provenance = case.get('provenance')
        if not all(isinstance(value, dict) for value in (review, expected, provenance)):
            error(case_id, 'review_expected_provenance_required')
            continue
        status = case.get('review_status')
        if status not in {'draft', 'approved', 'rejected'}:
            error(case_id, 'invalid_review_status')
        if tier == 'independent' and status != 'approved':
            error(case_id, 'independent_case_requires_approval')
        if tier == 'independent':
            if provenance.get('case_origin') != 'independent_human':
                error(case_id, 'independent_case_requires_new_human_origin')
            if review.get('used_for_tuning') is not False or review.get('independent_from_implementation') is not True:
                error(case_id, 'independent_case_contamination_or_attestation_missing')
        if status != 'approved':
            continue
        if (not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()
                or review.get('human_verified') is not True or not timestamp(review.get('reviewed_at'))):
            error(case_id, 'human_reviewer_and_timestamp_required')
        if expected.get('response_kind') not in KINDS:
            error(case_id, 'expected_response_kind_required')
        for key in ('must_include', 'must_not_include', 'required_tools', 'assertions'):
            value = expected.get(key)
            if (not isinstance(value, list) or len(value) > 30
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in value)):
                error(case_id, 'invalid_expected_'+key)
        if not any(expected.get(key) for key in ('must_include', 'must_not_include', 'assertions')):
            error(case_id, 'reviewed_scoring_labels_required')
        refs = review.get('verified_references')
        if not isinstance(refs, list) or not refs:
            error(case_id, 'verified_references_required')
            continue
        reference_ids = set()
        for reference in refs:
            if not isinstance(reference, dict):
                error(case_id, 'reference_must_be_object')
                continue
            reference_id = reference.get('reference_id')
            if not isinstance(reference_id, str) or not reference_id.strip() or reference_id in reference_ids:
                error(case_id, 'reference_id_required_and_unique')
            reference_ids.add(str(reference_id))
            if (reference.get('verified') is not True or not isinstance(reference.get('verified_by'), str)
                    or not reference['verified_by'].strip() or not timestamp(reference.get('verified_at'))
                    or not isinstance(reference.get('locator'), str) or not reference['locator'].strip()
                    or reference.get('kind') not in REFERENCE_KINDS):
                error(case_id, 'reference_verification_required')
            if reference.get('kind') in {'official_policy', 'official_data'}:
                if not re.fullmatch(r'https://[^\s]+', str(reference.get('source_url', ''))):
                    error(case_id, 'official_reference_url_required')
            try:
                path = snapshot_path(reference.get('path'), root)
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if reference.get('sha256') != actual:
                    error(case_id, 'reference_snapshot_hash_mismatch')
            except (ValueError, OSError):
                error(case_id, 'reference_snapshot_invalid')
    return {'benchmark_tier': tier, 'case_count': len(cases), 'approved_count': sum(
                isinstance(case, dict) and case.get('review_status') == 'approved' for case in cases),
            'scoring_version': SCORING_VERSION, 'passed': not errors, 'errors': errors,
            'eligible_for_independent_acceptance': tier == 'independent' and not errors,
            'limitations': 'Declarations and snapshot hashes are checked; human identity, source authority and label truth still require actual review.'}


def freeze_cases(cases, tier, output_dir=DATASETS/'frozen', root=ROOT):
    validation = validate_cases(cases, tier, root)
    if not validation['passed']:
        raise ValueError('dataset_validation_failed')
    identity = {'benchmark_tier': tier, 'scoring_version': SCORING_VERSION, 'cases': cases}
    identity_hash = digest(identity)
    benchmark_id = tier+'-'+identity_hash[:20]
    payload = {'benchmark_id': benchmark_id, 'benchmark_tier': tier, 'benchmark_sha256': identity_hash,
               'eligible_for_independent_acceptance': tier == 'independent', 'scoring_version': SCORING_VERSION,
               'label_provenance': 'human reviewed declarations' if tier == 'independent' else 'development; may include unreviewed draft tasks',
               'limitations': validation['limitations'], 'cases': cases}
    target_dir = Path(output_dir).resolve()
    if not target_dir.is_relative_to((root/'experiments/v2/datasets').resolve()):
        raise ValueError('frozen_output_must_stay_in_datasets')
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir/(benchmark_id+'.json')
    raw = json.dumps(payload, ensure_ascii=False, indent=2)+'\n'
    if target.exists():
        if target.read_text(encoding='utf-8') != raw:
            raise ValueError('frozen_benchmark_collision_or_mutation')
    else:
        with target.open('x', encoding='utf-8') as stream:
            stream.write(raw)
    return target, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'freeze'))
    parser.add_argument('--input', type=Path, default=DATASETS/'human_review_template.jsonl')
    parser.add_argument('--tier', choices=('development', 'independent'), default='development')
    parser.add_argument('--report', type=Path, default=ROOT/'experiments/v2/reports/stage6_review_validation.json')
    parser.add_argument('--output-dir', type=Path, default=DATASETS/'frozen')
    args = parser.parse_args()
    cases = load_cases(args.input)
    result = validate_cases(cases, args.tier)
    result['input_sha256'] = hashlib.sha256(args.input.read_bytes()).hexdigest()
    if args.command == 'freeze' and result['passed']:
        target, payload = freeze_cases(cases, args.tier, args.output_dir)
        result.update({'benchmark_id': payload['benchmark_id'], 'frozen_path': target.relative_to(ROOT).as_posix()})
    report = args.report.resolve()
    if not report.is_relative_to((ROOT/'experiments/v2/reports').resolve()):
        raise ValueError('review_report_must_stay_in_reports')
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('benchmark_tier', 'case_count', 'approved_count', 'passed', 'eligible_for_independent_acceptance')}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
