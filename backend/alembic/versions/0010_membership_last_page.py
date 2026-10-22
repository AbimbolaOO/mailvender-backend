"""memberships.last_page_id: the studio page each person last had open, per account

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('memberships', sa.Column('last_page_id', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('memberships', 'last_page_id')
