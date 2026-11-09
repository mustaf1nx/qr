"""Allow unlimited ongoing registration."""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"


def upgrade():
    with op.batch_alter_table("events") as batch:
        for column in ("capacity", "starts_at", "expires_at"):
            batch.alter_column(column, existing_type=sa.Integer(), nullable=True)


def downgrade():
    # Refuse a lossy conversion of unbounded registrations.
    connection = op.get_bind()
    missing = connection.execute(sa.text("SELECT count(*) FROM events WHERE capacity IS NULL OR starts_at IS NULL OR expires_at IS NULL")).scalar()
    if missing:
        raise RuntimeError("Set explicit event capacity and dates before downgrade")
    with op.batch_alter_table("events") as batch:
        for column in ("capacity", "starts_at", "expires_at"):
            batch.alter_column(column, existing_type=sa.Integer(), nullable=False)
