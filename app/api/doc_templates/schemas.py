"""inst:doctemplates — Pydantic schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
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
    description: Optional[str] = None
    doc_category: DocCategory = "other"
    product_id: Optional[UUID] = None
    product_code: Optional[str] = None


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    doc_category: Optional[DocCategory] = None
    product_id: Optional[UUID] = None
    product_code: Optional[str] = None


class TemplateOut(BaseModel):
    id: UUID
    institution_id: UUID
    code: str
    name: str
    description: Optional[str]
    doc_category: DocCategory
    product_id: Optional[UUID]
    product_code: Optional[str]
    status: TemplateStatus
    current_version: int
    created_by: Optional[UUID]
    created_at: datetime
    updated_at: datetime


class VersionCreate(BaseModel):
    """Metadata accompanying a .docx upload (multipart)."""
    merge_field_map: dict[str, Any] = Field(default_factory=dict)
    change_note: Optional[str] = None


class VersionOut(BaseModel):
    id: UUID
    template_id: UUID
    version_no: int
    file_name: str
    file_size_bytes: Optional[int]
    checksum_sha256: Optional[str]
    merge_field_map: dict[str, Any]
    change_note: Optional[str]
    status: VersionStatus
    created_by: Optional[UUID]
    approved_by: Optional[UUID]
    approved_at: Optional[datetime]
    rejection_note: Optional[str]
    created_at: datetime


class VersionDecision(BaseModel):
    """Checker action on a pending version."""
    action: Literal["approve", "reject"]
    note: Optional[str] = None


class GenerateRequest(BaseModel):
    entity_type: str = "loan_pipeline"
    entity_id: UUID
    stage_instance_id: Optional[UUID] = None
    # Explicit overrides merged over the auto-resolved deal data snapshot.
    data_overrides: dict[str, Any] = Field(default_factory=dict)
    output_pdf: bool = True


class GenerationOut(BaseModel):
    id: UUID
    template_id: UUID
    template_version_id: UUID
    entity_type: str
    entity_id: UUID
    stage_instance_id: Optional[UUID]
    status: GenerationStatus
    error: Optional[str]
    output_docx_path: Optional[str]
    output_pdf_path: Optional[str]
    esign_envelope_id: Optional[UUID]
    generated_by: Optional[UUID]
    generated_at: Optional[datetime]
    created_at: datetime
