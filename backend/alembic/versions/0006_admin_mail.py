"""admin mail: emails admins send (and their attachments)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('admin_mails',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('admin_id', sa.UUID(), nullable=False),
    sa.Column('from_address', sa.String(length=320), nullable=False),
    sa.Column('to', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('cc', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('bcc', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('format', sa.String(length=16), nullable=False),
    sa.Column('message_id_header', sa.String(length=500), nullable=False),
    sa.Column('in_reply_to_id', sa.UUID(), nullable=True),
    sa.Column('in_reply_to_header', sa.String(length=500), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['admin_id'], ['admin_users.id'], ),
    sa.ForeignKeyConstraint(['in_reply_to_id'], ['inbound_messages.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_admin_mails_created', 'admin_mails', ['created_at'], unique=False)
    op.create_table('admin_mail_attachments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('admin_id', sa.UUID(), nullable=False),
    sa.Column('mail_id', sa.UUID(), nullable=True),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=255), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('object_key', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['admin_id'], ['admin_users.id'], ),
    sa.ForeignKeyConstraint(['mail_id'], ['admin_mails.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_admin_mail_attachments_mail_id'), 'admin_mail_attachments', ['mail_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_admin_mail_attachments_mail_id'), table_name='admin_mail_attachments')
    op.drop_table('admin_mail_attachments')
    op.drop_index('ix_admin_mails_created', table_name='admin_mails')
    op.drop_table('admin_mails')
