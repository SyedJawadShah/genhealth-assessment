"""Order CRUD and PDF upload. Every query is scoped to the current user."""

import hashlib
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.auth import AppSettings, CurrentUser, DbSession, Limiter
from app.db import Order, as_utc, utcnow
from app.deps import extractor_dep
from app.extraction import ExtractedOrder, ExtractionError, Extractor, pdf_page_count
from app.validation import (
    FieldCheck,
    FieldStatus,
    check_birth_date_value,
    check_date_of_birth,
    check_npi,
    check_person_name,
    check_provider_name,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/orders", tags=["orders"])

FIELDS = (
    "patient_first_name",
    "patient_last_name",
    "patient_date_of_birth",
    "provider_name",
    "provider_npi",
)
MAX_EXTRACTED_TEXT = 200
_VERSION = re.compile(r"[0-9]{1,10}")


class FieldValidationError(Exception):
    """User-entered fields failed their checks; rendered as a 422 naming field and reason."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__("invalid fields")
        self.problems = problems


class FieldReport(BaseModel):
    status: FieldStatus
    reason: str | None = None
    source: Literal["extracted", "manual"]
    extracted_text: str | None = Field(
        default=None, description="What the document said, kept for review when it failed."
    )
    page: int | None = None


class FieldWarning(BaseModel):
    field: str
    code: str
    message: str


WARNING_MESSAGES = {
    "invalid_checksum": "NPI fails the check digit (Luhn with prefix 80840). "
    "Stored as extracted; verify it before use.",
    "needs_review": "The model cited a page the document does not have. "
    "Stored as extracted; verify it before use.",
}


class OrderOut(BaseModel):
    id: str
    status: Literal["ready", "needs_review"]
    source: Literal["upload", "manual"]
    patient_first_name: str | None
    patient_last_name: str | None
    patient_date_of_birth: date | None
    provider_name: str | None
    provider_npi: str | None
    version: int = Field(description="Send as If-Match on PATCH to reject stale updates.")
    fields: dict[str, FieldReport]
    warnings: list[FieldWarning] = Field(
        description="Values that were saved but need a human check, such as an NPI that fails "
        "its check digit."
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, order: Order) -> "OrderOut":
        return cls(
            id=order.id,
            status="ready" if order.status == "ready" else "needs_review",
            source="upload" if order.source == "upload" else "manual",
            patient_first_name=order.patient_first_name,
            patient_last_name=order.patient_last_name,
            patient_date_of_birth=order.patient_date_of_birth,
            provider_name=order.provider_name,
            provider_npi=order.provider_npi,
            version=order.version,
            fields={k: FieldReport.model_validate(v) for k, v in order.field_report.items()},
            warnings=[
                FieldWarning(field=k, code=v["status"], message=WARNING_MESSAGES[v["status"]])
                for k, v in order.field_report.items()
                if v.get("status") in WARNING_MESSAGES
            ],
            created_at=as_utc(order.created_at),
            updated_at=as_utc(order.updated_at),
        )


class OrderFields(BaseModel):
    """Order fields entered by a user. Omitted fields are left as they are; null clears one."""

    model_config = ConfigDict(extra="forbid")

    patient_first_name: str | None = Field(default=None, max_length=200)
    patient_last_name: str | None = Field(default=None, max_length=200)
    patient_date_of_birth: date | None = None
    provider_name: str | None = Field(default=None, max_length=300)
    provider_npi: str | None = Field(default=None, max_length=20)


def _check_manual(name: str, value: Any, today: date) -> FieldCheck[Any]:
    if name == "patient_date_of_birth":
        return check_birth_date_value(value, today)
    if name == "provider_name":
        return check_provider_name(value)
    if name == "provider_npi":
        return check_npi(value)
    return check_person_name(value)


def _check_extracted(extracted: ExtractedOrder, today: date) -> dict[str, FieldCheck[Any]]:
    return {
        "patient_first_name": check_person_name(extracted.patient_first_name.value),
        "patient_last_name": check_person_name(extracted.patient_last_name.value),
        "patient_date_of_birth": check_date_of_birth(extracted.patient_date_of_birth.value, today),
        "provider_name": check_provider_name(extracted.provider_name.value),
        "provider_npi": check_npi(extracted.provider_npi.value),
    }


def _apply(order: Order, checks: Mapping[str, FieldCheck[Any]], reports: Mapping[str, Any]) -> None:
    report = dict(order.field_report or {})
    for name, check in checks.items():
        setattr(order, name, check.value)
        report[name] = reports[name]
    order.field_report = report
    all_valid = all(report.get(name, {}).get("status") == "valid" for name in FIELDS)
    order.status = "ready" if all_valid else "needs_review"


def _apply_manual(order: Order, body: OrderFields, names: set[str]) -> None:
    today = utcnow().date()
    checks = {name: _check_manual(name, getattr(body, name), today) for name in sorted(names)}
    problems = [
        {"field": f"body.{name}", "problem": check.reason or "invalid"}
        for name, check in checks.items()
        if check.status == "invalid"
    ]
    if problems:
        raise FieldValidationError(problems)
    reports = {
        name: FieldReport(status=check.status, reason=check.reason, source="manual").model_dump()
        for name, check in checks.items()
    }
    _apply(order, checks, reports)


def _owned_order(session: Session, order_id: str, user_id: int) -> Order:
    order = session.scalar(select(Order).where(Order.id == order_id, Order.owner_id == user_id))
    if order is None:
        # Same answer whether the order is someone else's or does not exist.
        raise HTTPException(status_code=404, detail="order_not_found")
    return order


def _log_order(event: str, order: Order) -> None:
    log.info(
        event,
        extra={
            "order_id": order.id,
            "user_id": order.owner_id,
            "order_status": order.status,
            "field_status": {k: v.get("status") for k, v in order.field_report.items()},
        },
    )


@router.post("", status_code=201)
def create_order(body: OrderFields, user: CurrentUser, session: DbSession) -> OrderOut:
    """Create an order from fields entered by hand. Every field is checked; missing is allowed."""
    order = Order(owner_id=user.id, source="manual", field_report={})
    _apply_manual(order, body, set(FIELDS))
    session.add(order)
    session.commit()
    _log_order("order_created", order)
    return OrderOut.of(order)


@router.post("/upload", status_code=201)
def upload_order(
    file: UploadFile,
    user: CurrentUser,
    session: DbSession,
    limiter: Limiter,
    settings: AppSettings,
    extractor: Annotated[Extractor, Depends(extractor_dep)],
) -> OrderOut:
    """Upload an order PDF (text or scanned). Extracted fields are checked before saving;
    fields that are missing or fail a check are saved as null with a status and reason."""
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="file_too_large")
    if not data:
        raise HTTPException(status_code=422, detail="empty_file")
    # Decide by content, not by filename or the client's content type.
    if b"%PDF-" not in data[:1024]:
        raise HTTPException(status_code=415, detail="not_a_pdf")

    limiter.hit(f"upload-user:{user.id}", settings.upload_limit_per_hour, 3600)
    limiter.hit("upload-global", settings.upload_global_limit_per_hour, 3600)
    try:
        extracted = extractor.extract(data)
    except ExtractionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

    checks = _check_extracted(extracted, utcnow().date())
    page_count = pdf_page_count(data)
    reports = {}
    for name, check in checks.items():
        field = getattr(extracted, name)
        raw = (field.value or "").strip()[:MAX_EXTRACTED_TEXT] or None
        page = field.page if raw else None
        status, reason = check.status, check.reason
        if check.status == "missing":
            reason = "not_found_in_document"
        # A page the document does not have means the model's answer is suspect.
        if page is not None and (page < 1 or (page_count is not None and page > page_count)):
            page = None
            if status == "valid":
                status, reason = "needs_review", "page_out_of_range"
        reports[name] = FieldReport(
            status=status, reason=reason, source="extracted", extracted_text=raw, page=page
        ).model_dump()
    order = Order(
        owner_id=user.id,
        source="upload",
        field_report={},
        document_sha256=hashlib.sha256(data).hexdigest(),
    )
    _apply(order, checks, reports)
    session.add(order)
    session.commit()
    _log_order("order_created", order)
    return OrderOut.of(order)


@router.get("")
def list_orders(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[OrderOut]:
    """Your orders, newest first."""
    orders = session.scalars(
        select(Order)
        .where(Order.owner_id == user.id)
        .order_by(Order.created_at.desc(), Order.id)
        .limit(limit)
        .offset(offset)
    )
    return [OrderOut.of(order) for order in orders]


@router.get("/{order_id}")
def get_order(order_id: str, user: CurrentUser, session: DbSession) -> OrderOut:
    return OrderOut.of(_owned_order(session, order_id, user.id))


def _expected_version(if_match: str) -> int:
    value = if_match.strip().removeprefix("W/").strip('"')
    # ASCII digits only, bounded: str.isdigit() also accepts characters int() rejects.
    if not _VERSION.fullmatch(value):
        raise HTTPException(status_code=422, detail="invalid_request")
    return int(value)


@router.patch("/{order_id}", responses={409: {"description": "If-Match version is stale"}})
def update_order(
    order_id: str,
    body: OrderFields,
    user: CurrentUser,
    session: DbSession,
    if_match: Annotated[str | None, Header()] = None,
) -> OrderOut:
    """Correct or fill in fields. Only the fields sent are changed, each checked again.
    Send the order's `version` as `If-Match` to get 409 instead of overwriting a newer edit."""
    order = _owned_order(session, order_id, user.id)
    if if_match is not None and _expected_version(if_match) != order.version:
        raise HTTPException(status_code=409, detail="version_conflict")
    _apply_manual(order, body, body.model_fields_set)
    order.updated_at = utcnow()
    try:
        session.commit()
    except StaleDataError:
        # Another request updated the order between our read and this write.
        session.rollback()
        raise HTTPException(status_code=409, detail="version_conflict") from None
    _log_order("order_updated", order)
    return OrderOut.of(order)


@router.delete(
    "/{order_id}", status_code=204, responses={409: {"description": "Changed concurrently"}}
)
def delete_order(order_id: str, user: CurrentUser, session: DbSession) -> Response:
    order = _owned_order(session, order_id, user.id)
    session.delete(order)
    try:
        session.commit()
    except StaleDataError:
        # The order changed between our read and the delete; let the client re-read it.
        session.rollback()
        raise HTTPException(status_code=409, detail="version_conflict") from None
    log.info("order_deleted", extra={"order_id": order_id, "user_id": user.id})
    return Response(status_code=204)
