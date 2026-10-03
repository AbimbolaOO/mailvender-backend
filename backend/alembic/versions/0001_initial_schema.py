"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-03 11:21:23.107495
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('physical_address', sa.Text(), nullable=True),
    sa.Column('hourly_recipient_limit', sa.Integer(), nullable=False),
    sa.Column('daily_recipient_limit', sa.Integer(), nullable=False),
    sa.Column('sending_suspended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('suspension_reason', sa.Text(), nullable=True),
    sa.Column('deletion_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('job_runs',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('last_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('rate_limit_counters',
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('key', 'window_start')
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_operator', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('default_account_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['default_account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('account_exports',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('object_key', sa.Text(), nullable=True),
    sa.Column('byte_size', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_account_exports_account_id'), 'account_exports', ['account_id'], unique=False)
    op.create_table('api_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('prefix', sa.String(length=32), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    op.create_index(op.f('ix_api_keys_account_id'), 'api_keys', ['account_id'], unique=False)
    op.create_table('assets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('upload_key', sa.Text(), nullable=False),
    sa.Column('public_key', sa.Text(), nullable=True),
    sa.Column('public_url', sa.Text(), nullable=True),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('mime_type', sa.String(length=64), nullable=False),
    sa.Column('byte_size', sa.BigInteger(), nullable=True),
    sa.Column('declared_size', sa.BigInteger(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('upload_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assets_account_id'), 'assets', ['account_id'], unique=False)
    op.create_table('audit_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=True),
    sa.Column('actor_type', sa.String(length=16), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=True),
    sa.Column('target_id', sa.UUID(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_account_created', 'audit_events', ['account_id', 'created_at'], unique=False)
    op.create_table('domains',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=253), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('return_path_host', sa.String(length=253), nullable=False),
    sa.Column('record_status', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('bulk_eligible', sa.Boolean(), nullable=False),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_domains_account_id'), 'domains', ['account_id'], unique=False)
    op.create_index('uq_domains_live_name', 'domains', ['name'], unique=True, postgresql_where=sa.text('disabled_at IS NULL'))
    op.create_table('email_tokens',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_email_tokens_user_id'), 'email_tokens', ['user_id'], unique=False)
    op.create_table('invitations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('invited_by', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('accepted_by', sa.UUID(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['accepted_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['invited_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_invitations_account_id'), 'invitations', ['account_id'], unique=False)
    op.create_table('memberships',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'user_id')
    )
    op.create_index(op.f('ix_memberships_account_id'), 'memberships', ['account_id'], unique=False)
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)
    op.create_table('outbox_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outbox_events_account_id'), 'outbox_events', ['account_id'], unique=False)
    op.create_index('ix_outbox_pending', 'outbox_events', ['status', 'available_at'], unique=False)
    op.create_table('sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_agent', sa.String(length=512), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('sync_records',
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('collection', sa.String(length=32), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('deleted', sa.Boolean(), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('account_id', 'collection', 'id')
    )
    op.create_index('ix_sync_records_account_seq', 'sync_records', ['account_id', 'seq'], unique=False)
    op.create_index('ix_sync_records_parent', 'sync_records', ['account_id', 'collection', 'parent_id'], unique=False)
    op.create_table('dkim_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('domain_id', sa.UUID(), nullable=False),
    sa.Column('selector', sa.String(length=63), nullable=False),
    sa.Column('private_key_encrypted', sa.Text(), nullable=False),
    sa.Column('public_key', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('selector')
    )
    op.create_index(op.f('ix_dkim_keys_account_id'), 'dkim_keys', ['account_id'], unique=False)
    op.create_index(op.f('ix_dkim_keys_domain_id'), 'dkim_keys', ['domain_id'], unique=False)
    op.create_table('sender_identities',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('domain_id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'email')
    )
    op.create_index(op.f('ix_sender_identities_account_id'), 'sender_identities', ['account_id'], unique=False)
    op.create_index(op.f('ix_sender_identities_domain_id'), 'sender_identities', ['domain_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('domain_id', sa.UUID(), nullable=False),
    sa.Column('sender_identity_id', sa.UUID(), nullable=False),
    sa.Column('from_email', sa.String(length=320), nullable=False),
    sa.Column('from_name', sa.String(length=200), nullable=True),
    sa.Column('reply_to', sa.String(length=320), nullable=True),
    sa.Column('subject', sa.Text(), nullable=False),
    sa.Column('html', sa.Text(), nullable=True),
    sa.Column('text_body', sa.Text(), nullable=True),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('created_by_user_id', sa.UUID(), nullable=True),
    sa.Column('created_by_api_key_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('content_purged_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['created_by_api_key_id'], ['api_keys.id'], ),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ),
    sa.ForeignKeyConstraint(['sender_identity_id'], ['sender_identities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'idempotency_key')
    )
    op.create_index(op.f('ix_messages_account_id'), 'messages', ['account_id'], unique=False)
    op.create_table('message_recipients',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('domain_id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('bounced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('complained_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_message_recipients_account_created', 'message_recipients', ['account_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_message_recipients_account_id'), 'message_recipients', ['account_id'], unique=False)
    op.create_index(op.f('ix_message_recipients_message_id'), 'message_recipients', ['message_id'], unique=False)
    op.create_table('delivery_attempts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('recipient_id', sa.UUID(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('outcome', sa.String(length=24), nullable=False),
    sa.Column('smtp_code', sa.Integer(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['recipient_id'], ['message_recipients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_delivery_attempts_account_id'), 'delivery_attempts', ['account_id'], unique=False)
    op.create_index(op.f('ix_delivery_attempts_recipient_id'), 'delivery_attempts', ['recipient_id'], unique=False)
    op.create_table('delivery_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('recipient_id', sa.UUID(), nullable=True),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('type', sa.String(length=16), nullable=False),
    sa.Column('classification', sa.String(length=16), nullable=True),
    sa.Column('status_code', sa.String(length=16), nullable=True),
    sa.Column('diagnostic', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['recipient_id'], ['message_recipients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_delivery_events_account_created', 'delivery_events', ['account_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_delivery_events_account_id'), 'delivery_events', ['account_id'], unique=False)
    op.create_index(op.f('ix_delivery_events_recipient_id'), 'delivery_events', ['recipient_id'], unique=False)
    op.create_table('suppressions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('reason', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('recipient_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.ForeignKeyConstraint(['recipient_id'], ['message_recipients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'email')
    )
    op.create_index(op.f('ix_suppressions_account_id'), 'suppressions', ['account_id'], unique=False)


    # Change-feed ordering for cloud sync.
    op.execute("CREATE SEQUENCE sync_seq")
    # Audit entries are immutable.
    op.execute(
        """
        CREATE FUNCTION audit_events_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events rows are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_immutable()")
    op.execute("DROP SEQUENCE IF EXISTS sync_seq")
    op.drop_index(op.f('ix_suppressions_account_id'), table_name='suppressions')
    op.drop_table('suppressions')
    op.drop_index(op.f('ix_delivery_events_recipient_id'), table_name='delivery_events')
    op.drop_index(op.f('ix_delivery_events_account_id'), table_name='delivery_events')
    op.drop_index('ix_delivery_events_account_created', table_name='delivery_events')
    op.drop_table('delivery_events')
    op.drop_index(op.f('ix_delivery_attempts_recipient_id'), table_name='delivery_attempts')
    op.drop_index(op.f('ix_delivery_attempts_account_id'), table_name='delivery_attempts')
    op.drop_table('delivery_attempts')
    op.drop_index(op.f('ix_message_recipients_message_id'), table_name='message_recipients')
    op.drop_index(op.f('ix_message_recipients_account_id'), table_name='message_recipients')
    op.drop_index('ix_message_recipients_account_created', table_name='message_recipients')
    op.drop_table('message_recipients')
    op.drop_index(op.f('ix_messages_account_id'), table_name='messages')
    op.drop_table('messages')
    op.drop_index(op.f('ix_sender_identities_domain_id'), table_name='sender_identities')
    op.drop_index(op.f('ix_sender_identities_account_id'), table_name='sender_identities')
    op.drop_table('sender_identities')
    op.drop_index(op.f('ix_dkim_keys_domain_id'), table_name='dkim_keys')
    op.drop_index(op.f('ix_dkim_keys_account_id'), table_name='dkim_keys')
    op.drop_table('dkim_keys')
    op.drop_index('ix_sync_records_parent', table_name='sync_records')
    op.drop_index('ix_sync_records_account_seq', table_name='sync_records')
    op.drop_table('sync_records')
    op.drop_index(op.f('ix_sessions_user_id'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_index('ix_outbox_pending', table_name='outbox_events')
    op.drop_index(op.f('ix_outbox_events_account_id'), table_name='outbox_events')
    op.drop_table('outbox_events')
    op.drop_index(op.f('ix_memberships_user_id'), table_name='memberships')
    op.drop_index(op.f('ix_memberships_account_id'), table_name='memberships')
    op.drop_table('memberships')
    op.drop_index(op.f('ix_invitations_account_id'), table_name='invitations')
    op.drop_table('invitations')
    op.drop_index(op.f('ix_email_tokens_user_id'), table_name='email_tokens')
    op.drop_table('email_tokens')
    op.drop_index('uq_domains_live_name', table_name='domains', postgresql_where=sa.text('disabled_at IS NULL'))
    op.drop_index(op.f('ix_domains_account_id'), table_name='domains')
    op.drop_table('domains')
    op.drop_index('ix_audit_account_created', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index(op.f('ix_assets_account_id'), table_name='assets')
    op.drop_table('assets')
    op.drop_index(op.f('ix_api_keys_account_id'), table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_index(op.f('ix_account_exports_account_id'), table_name='account_exports')
    op.drop_table('account_exports')
    op.drop_table('users')
    op.drop_table('rate_limit_counters')
    op.drop_table('job_runs')
    op.drop_table('accounts')
