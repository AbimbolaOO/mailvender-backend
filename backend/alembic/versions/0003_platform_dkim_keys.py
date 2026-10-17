"""platform dkim keys

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17 19:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('platform_dkim_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('domain', sa.String(length=253), nullable=False),
    sa.Column('selector', sa.String(length=63), nullable=False),
    sa.Column('private_key_encrypted', sa.Text(), nullable=False),
    sa.Column('public_key', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('selector')
    )
    op.create_index(op.f('ix_platform_dkim_keys_domain'), 'platform_dkim_keys', ['domain'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_platform_dkim_keys_domain'), table_name='platform_dkim_keys')
    op.drop_table('platform_dkim_keys')
