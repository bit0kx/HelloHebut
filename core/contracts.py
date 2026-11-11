"""Small, versioned contracts shared by chat, retrieval and offline evaluation."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TaskState(Contract):
    schema_version: int = 2
    revision: int = 0
    entities: dict[str, list[str]] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)
    invalidated: list[str] = Field(default_factory=list)
    subject_is_self: bool = True


class RetrievalQuery(Contract):
    query: str = Field(min_length=1, max_length=500)
    year: int | None = Field(default=None, ge=2000, le=2100)


class RetrievalPlan(Contract):
    queries: list[RetrievalQuery] = Field(min_length=1, max_length=6)


class Evidence(Contract):
    evidence_id: str
    content: str
    title: str = ''
    source_url: str = ''
    source_file: str = ''
    year: int | None = None
    version: str = ''
    content_hash: str
    document_id: str = ''
    revision_id: str = ''
    chunk_id: str = ''


class SupportedClaim(Contract):
    claim: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)


class EvidenceVerdict(Contract):
    sufficient: bool = Field(strict=True)
    supported_claims: list[SupportedClaim] = Field(default_factory=list, max_length=20)
    missing_facts: list[str] = Field(default_factory=list, max_length=12)
    conflicting_sources: list[str] = Field(default_factory=list, max_length=12)
    next_queries: list[RetrievalQuery] = Field(default_factory=list, max_length=2)
    decision: Literal['answer', 'retrieve_again', 'abstain']


class AnswerVerdict(Contract):
    passed: bool = Field(strict=True)
    unsupported_claims: list[str] = Field(default_factory=list, max_length=12)


class AdmissionQuery(Contract):
    query: str = Field(min_length=1, max_length=8000)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)
