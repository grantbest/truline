from sqlalchemy import (
    CheckConstraint,
    Column,
    String,
    DateTime,
    ForeignKey,
    Float,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
import uuid

Base = declarative_base()

class Bead(Base):
    __tablename__ = "bead"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_bead_confidence_range",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    namespace = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False, index=True)
    state = Column(String, nullable=False, index=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("bead.id"), nullable=True)
    context = Column(JSONB, nullable=False, server_default='{}')
    content = Column(JSONB, nullable=False, server_default='{}')
    confidence = Column(Float, nullable=True)
    trust_tier = Column(String, nullable=False)
    provenance = Column(JSONB, nullable=False, server_default='{}')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by = Column(String, nullable=False)

class BeadLink(Base):
    """A typed, directed edge between two beads — ARCHITECTURE.md §3.1 ``link``.

    Edges used to live inside ``content`` (``dev.note.answers_ref``,
    ``dev.task.source_bead_ids``, and the EA model's ``realizes``/``depends_on``
    arrays). That has two costs this table removes.

    **Integrity.** A content string pointing at a deleted bead dangles
    silently — a deleted question leaves ``open_questions()`` treating it as
    unanswered forever. Both FKs cascade, so an edge cannot outlive either
    endpoint.

    **Queryability.** ALE Fernet-encrypts every leaf value in ``content``
    (``crypto.py``), so a ref stored there is ciphertext at rest and can never
    be filtered in SQL. ``source_id``/``target_id``/``link_type`` are plain
    indexed columns precisely so traversal is a real query. ``content`` carries
    optional edge annotation and stays encrypted like any other bead payload —
    the semantics are queryable, the commentary is not.
    """

    __tablename__ = "bead_link"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "target_id", "link_type", name="uq_bead_link_edge"
        ),
        CheckConstraint("source_id <> target_id", name="ck_bead_link_no_self"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bead.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bead.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    link_type = Column(String, nullable=False, index=True)
    content = Column(JSONB, nullable=False, server_default='{}')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by = Column(String, nullable=False)


class BeadEvent(Base):
    __tablename__ = "bead_event"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bead_id = Column(UUID(as_uuid=True), ForeignKey("bead.id"), nullable=False)
    event_type = Column(String, nullable=False)  # 'created', 'transitioned', 'updated'
    from_state = Column(String, nullable=True)
    to_state = Column(String, nullable=True)
    payload = Column(JSONB, nullable=False, server_default='{}')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by = Column(String, nullable=False)
