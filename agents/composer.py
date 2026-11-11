"""Typed role output and deterministic, evidence-bound composition.

No majority vote or extra factual generation. Semantic support is still checked
by the outer answer verifier, including all claims whose IDs are well formed.
"""
import re
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid')
    statement: str = Field(min_length=1, max_length=1500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    fact_key: str = Field(default='', max_length=160)
    kind: Literal['fact', 'advice'] = 'fact'


class RoleResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['completed', 'partial', 'failed']
    claims: list[Claim] = Field(default_factory=list, max_length=20)
    missing_facts: list[Annotated[str, StringConstraints(max_length=500)]] = Field(default_factory=list, max_length=10)
    errors: list[Annotated[str, StringConstraints(max_length=200)]] = Field(default_factory=list, max_length=10)


def normalise(text):
    return re.sub(r'\s+|\[来源\d+\]', '', text).strip('。.!！?？')


def validate_role_result(result: RoleResult, evidence: list[dict]) -> RoleResult:
    known = {f'source:{n}' for n in range(1, len(evidence)+1)}
    for claim in result.claims:
        if set(claim.evidence_ids) - known:
            raise ValueError('unknown_role_evidence')
        if claim.kind == 'fact' and not claim.evidence_ids:
            raise ValueError('fact_claim_without_evidence')
        if claim.kind == 'fact' and not claim.fact_key:
            raise ValueError('fact_claim_without_key')
        if claim.kind == 'advice' and (re.search(r'\d|学校规定|学费|住宿费|校区|最低分|最低位次', claim.statement)):
            raise ValueError('factual_claim_disguised_as_advice')
    return result


class ResponseComposer:
    def compose(self, results: list[RoleResult]) -> tuple[str, dict]:
        by_text, keys, signature_keys, conflicts, missing, errors = {}, {}, {}, set(), [], []
        for result in results:
            missing.extend(result.missing_facts)
            errors.extend(result.errors)
            if result.status == 'failed':
                continue
            for original in result.claims:
                claim = original.model_copy(deep=True)
                signature = normalise(claim.statement)
                if claim.fact_key:
                    signature_keys.setdefault(signature, set()).add(claim.fact_key)
                    prior = keys.get(claim.fact_key)
                    if prior and prior != signature:
                        conflicts.add(claim.fact_key)
                    keys[claim.fact_key] = signature
                if signature in by_text:
                    saved = by_text[signature]
                    saved.evidence_ids = list(dict.fromkeys(saved.evidence_ids + claim.evidence_ids))
                else:
                    by_text[signature] = claim
        lines = []
        for signature, claim in by_text.items():
            if signature_keys.get(signature, set()) & conflicts:
                continue
            text = re.sub(r'\[来源\d+\]', '', claim.statement).strip()
            refs = ''.join(f'[来源{ref.split(":")[1]}]' for ref in claim.evidence_ids)
            lines.append(('建议：' if claim.kind == 'advice' else '') + text + refs)
        claim_count = len(lines)
        if conflicts:
            lines.append('相关结论存在冲突，暂不采用冲突内容，需要重新核对原始资料。')
        if missing:
            lines.append('尚待核实：' + '；'.join(dict.fromkeys(missing)))
        if any(result.status == 'failed' for result in results) and lines:
            lines.append('部分分析未能完成，请补充条件或稍后重试。')
        if not lines:
            lines.append('当前无法形成有依据的完整回答，请补充条件或稍后重试。')
        status = ('conflict' if conflicts else 'failed' if not results or all(r.status == 'failed' for r in results)
                  else 'partial' if missing or errors or not claim_count or any(r.status == 'partial' or r.status == 'failed' for r in results)
                  else 'completed')
        metadata = {'status': status, 'claim_count': claim_count, 'conflict_keys': sorted(conflicts),
                    'missing_facts': list(dict.fromkeys(missing)), 'errors': list(dict.fromkeys(errors))}
        return '\n\n'.join(lines), metadata
