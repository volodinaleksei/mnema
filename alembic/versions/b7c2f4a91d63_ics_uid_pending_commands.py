"""P3-17: jobs.ics_uid (dedup importov); P3-18: pending_commands (stop/salvage fallback)

Revision ID: b7c2f4a91d63
Revises: aa3c91f6d2b5
Create Date: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa

revision = "b7c2f4a91d63"
down_revision = "aa3c91f6d2b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("ics_uid", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_jobs_ics_uid"), "jobs", ["ics_uid"], unique=False)
    op.create_table(
        "pending_commands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cmd", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pushed_ts", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_pending_commands_cmd"), "pending_commands", ["cmd"], unique=False)
    op.create_index(op.f("ix_pending_commands_job_id"), "pending_commands", ["job_id"], unique=False)
    op.create_index(op.f("ix_pending_commands_status"), "pending_commands", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_pending_commands_status"), table_name="pending_commands")
    op.drop_index(op.f("ix_pending_commands_job_id"), table_name="pending_commands")
    op.drop_index(op.f("ix_pending_commands_cmd"), table_name="pending_commands")
    op.drop_table("pending_commands")
    op.drop_index(op.f("ix_jobs_ics_uid"), table_name="jobs")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("ics_uid")
