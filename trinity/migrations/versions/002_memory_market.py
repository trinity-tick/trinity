"""Memory Market — new tables for market_assets, market_transactions, reputation_ledger.

Revision ID: 002_memory_market
Revises: 38260df6ba71
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = '002_memory_market'
down_revision = '38260df6ba71'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── market_assets — listed memory assets ──────────────────────────
    op.create_table(
        'market_assets',
        sa.Column('asset_id', sa.String(), nullable=False),
        sa.Column('memory_id', sa.String(), nullable=False),
        sa.Column('owner_agent', sa.String(), nullable=False),
        sa.Column('content_hash', sa.String(), nullable=False),
        sa.Column('modality', sa.String(), nullable=False, server_default='text'),
        sa.Column('tags', sa.String(), server_default='[]'),
        sa.Column('price', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('currency', sa.String(), nullable=False, server_default='trust_score'),
        sa.Column('license', sa.String(), nullable=False, server_default='CC-BY'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('listed_at', sa.String(), nullable=False),
        sa.Column('delisted_at', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('asset_id'),
    )
    op.create_index('idx_market_assets_modality', 'market_assets', ['modality'])
    op.create_index('idx_market_assets_owner', 'market_assets', ['owner_agent'])
    op.create_index('idx_market_assets_active', 'market_assets', ['is_active'])

    # ── market_transactions — trade records ───────────────────────────
    op.create_table(
        'market_transactions',
        sa.Column('tx_id', sa.String(), nullable=False),
        sa.Column('buyer_agent', sa.String(), nullable=False),
        sa.Column('seller_agent', sa.String(), nullable=False),
        sa.Column('asset_id', sa.String(), nullable=False),
        sa.Column('memory_id', sa.String(), nullable=False),
        sa.Column('price', sa.Float(), nullable=False),
        sa.Column('currency', sa.String(), nullable=False, server_default='trust_score'),
        sa.Column('status', sa.String(), nullable=False, server_default='completed'),
        sa.Column('timestamp', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('tx_id'),
    )
    op.create_index('idx_market_tx_buyer', 'market_transactions', ['buyer_agent'])
    op.create_index('idx_market_tx_seller', 'market_transactions', ['seller_agent'])
    op.create_index('idx_market_tx_asset', 'market_transactions', ['asset_id'])
    op.create_index('idx_market_tx_time', 'market_transactions', ['timestamp'])

    # ── reputation_ledger — reputation events ─────────────────────────
    op.create_table(
        'reputation_ledger',
        sa.Column('event_id', sa.String(), nullable=False),
        sa.Column('agent_id', sa.String(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('from_agent', sa.String(), server_default=''),
        sa.Column('reason', sa.String(), server_default=''),
        sa.Column('timestamp', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('event_id'),
    )
    op.create_index('idx_reputation_agent', 'reputation_ledger', ['agent_id'])
    op.create_index('idx_reputation_type', 'reputation_ledger', ['event_type'])
    op.create_index('idx_reputation_time', 'reputation_ledger', ['timestamp'])


def downgrade() -> None:
    op.drop_table('reputation_ledger')
    op.drop_table('market_transactions')
    op.drop_table('market_assets')
