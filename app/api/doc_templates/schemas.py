"""inst:doctemplates — Pydantic schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

DocCategory = Literal[
    "loan_agreement", "facility_letter", "legal_agreement", "terms_conditions", "other"
]
TemplateStatus = Literal["draft", "pending_approval", "active", "retired"]
VersionStatus = Literal["draft", "pending_approval", "published", "retired", "rejected"]
GenerationStatus = Literal["pending", "generating", "generated", "failed"]


class TemplateCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=2, max_length=200)
    description: str | None = None
    doc_category: DocCategory = "other"
    product_id: UUID | None = None
    product_code: str | None = None


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    doc_category: DocCategory | None = None
    product_id: UUID | None = None
    product_code: str | None = None


class TemplateOut(BaseModel):
    id: UUID
    institution_id: UUID
    code: str
    name: str
    description: str | None
    doc_category: DocCategory
    product_id: UUID | None
    product_code: str | None
    status: TemplateStatus
    current_version: int
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class VersionCreate(BaseModel):
    """Metadata accompanying a .docx upload (multipart)."""
    merge_field_map: dict[str, Any] = Field(default_factory=dict)
    change_note: str | None = None


class VersionOut(BaseModel):
    id: UUID
    template_id: UUID
    version_no: int
    file_name: str
    file_size_bytes: int | None
    checksum_sha256: str | None
    merge_field_map: dict[str, Any]
    change_note: str | None
    status: VersionStatus
    created_by: UUID | None
    approved_by: UUID | None
    approved_at: datetime | None
    rejection_note: str | None
    created_at: datetime


class VersionDecision(BaseModel):
    """Checker action on a pending version."""
    action: Literal["approve", "reject"]
    note: str | None = None


class GenerateRequest(BaseModel):
    entity_type: str = "loan_pipeline"
    entity_id: UUID
    stage_instance_id: UUID | None = None
    # Explicit overrides merged over the auto-resolved deal data snapshot.
    data_overrides: dict[str, Any] = Field(default_factory=dict)
    output_pdf: bool = True


class GenerationOut(BaseModel):
    id: UUID
    template_id: UUID
    template_version_id: UUID
    entity_type: str
    entity_id: UUID
    stage_instance_id: UUID | None
    status: GenerationStatus
    error: str | None
    output_docx_path: str | None
    output_pdf_path: str | None
    esign_envelope_id: UUID | None
    generated_by: UUID | None
    generated_at: datetime | None
    created_at: datetime
