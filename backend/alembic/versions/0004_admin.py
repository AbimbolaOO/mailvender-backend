"""admin dashboard: admin users and sessions, role inboxes, DMARC rows, activity days

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('user_activity_days',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'day')
    )
    op.create_index(op.f('ix_user_activity_days_day'), 'user_activity_days', ['day'], unique=False)
    op.create_table('admin_users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('must_change_password', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('totp_secret_encrypted', sa.Text(), nullable=True),
    sa.Column('totp_enabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('totp_last_step', sa.BigInteger(), nullable=True),
    sa.Column('recovery_code_hashes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('failed_logins', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('admin_sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('admin_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=512), nullable=True),
    sa.ForeignKeyConstraint(['admin_id'], ['admin_users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_admin_sessions_admin_id'), 'admin_sessions', ['admin_id'], unique=False)
    op.create_table('admin_issue_states',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.UUID(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['updated_by'], ['admin_users.id'], ),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('inbound_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('mailbox', sa.String(length=16), nullable=False),
    sa.Column('message_id_header', sa.String(length=500), nullable=True),
    sa.Column('from_address', sa.String(length=320), nullable=False),
    sa.Column('from_name', sa.String(length=200), nullable=True),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('object_key', sa.Text(), nullable=False),
    sa.Column('has_attachments', sa.Boolean(), nullable=False),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mailbox', 'message_id_header')
    )
    op.create_index('ix_inbound_messages_mailbox_created', 'inbound_messages', ['mailbox', 'created_at'], unique=False)
    op.create_table('dmarc_report_rows',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('inbound_message_id', sa.UUID(), nullable=True),
    sa.Column('reporter', sa.String(length=200), nullable=False),
    sa.Column('report_id', sa.String(length=200), nullable=False),
    sa.Column('domain', sa.String(length=253), nullable=False),
    sa.Column('begin_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source_ip', sa.String(length=64), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('disposition', sa.String(length=16), nullable=False),
    sa.Column('dkim', sa.String(length=16), nullable=False),
    sa.Column('spf', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['inbound_message_id'], ['inbound_messages.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_dmarc_report_rows_inbound_message_id'), 'dmarc_report_rows', ['inbound_message_id'], unique=False)
    op.create_index('ix_message_recipients_created', 'message_recipients', ['created_at'], unique=False)
    op.create_index('ix_message_recipients_accepted', 'message_recipients', ['accepted_at'], unique=False)
    op.create_index('ix_delivery_events_created', 'delivery_events', ['created_at'], unique=False)
    op.create_index('ix_audit_actor_created', 'audit_events', ['actor_type', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_audit_actor_created', table_name='audit_events')
    op.drop_index('ix_delivery_events_created', table_name='delivery_events')
    op.drop_index('ix_message_recipients_accepted', table_name='message_recipients')
    op.drop_index('ix_message_recipients_created', table_name='message_recipients')
    op.drop_index(op.f('ix_dmarc_report_rows_inbound_message_id'), table_name='dmarc_report_rows')
    op.drop_table('dmarc_report_rows')
    op.drop_index('ix_inbound_messages_mailbox_created', table_name='inbound_messages')
    op.drop_table('inbound_messages')
    op.drop_table('admin_issue_states')
    op.drop_index(op.f('ix_admin_sessions_admin_id'), table_name='admin_sessions')
    op.drop_table('admin_sessions')
    op.drop_table('admin_users')
    op.drop_index(op.f('ix_user_activity_days_day'), table_name='user_activity_days')
    op.drop_table('user_activity_days')
