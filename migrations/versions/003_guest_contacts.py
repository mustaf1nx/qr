"""Store guest names and phone numbers."""
from alembic import op
import sqlalchemy as sa
revision = "003"
down_revision = "002"
def upgrade():
    for name, length in (("first_name",50),("last_name",50),("phone",20)):
        op.add_column("participants",sa.Column(name,sa.String(length),nullable=True))
def downgrade():
    with op.batch_alter_table("participants") as batch:
        for name in ("phone","last_name","first_name"):
            batch.drop_column(name)
