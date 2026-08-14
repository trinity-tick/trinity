"""Self Evolution — access_log, feedback_records, evolution_history, mutation_suggestions.

Revision ID: 003_self_evolution
Revises: 002_memory_market
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = '003_self_evolution'
down_revision = '002_memory_market'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── access_log — memory access tracking ─────────────────────────
    op.create_table(
        'access_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('memory_id', sa.String(), nullable=False),
        sa.Column('agent_id', sa.String(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('context', sa.String(), server_default=''),
        sa.Column('timestamp', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_access_log_memory', 'access_log', ['memory_id'])
    op.create_index('idx_access_log_agent', 'access_log', ['agent_id'])
    op.create_index('idx_access_log_time', 'access_log', ['timestamp'])
    op.create_index('idx_access_log_memory_time', 'access_log', ['memory_id', 'timestamp'])

    # ── feedback_records — agent feedback on memory quality ─────────
    op.create_table(
        'feedback_records',
        sa.Column('feedback_id', sa.String(), nullable=False),
        sa.Column('memory_id', sa.String(), nullable=False),
        sa.Column('agent_id', sa.String(), nullable=False),
        sa.Column('rating', sa.Float(), nullable=False),
        sa.Column('comment', sa.String(), server_default=''),
        sa.Column('context', sa.String(), server_default=''),
        sa.Column('timestamp', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('feedback_id'),
    )
    op.create_index('idx_feedback_memory', 'feedback_records', ['memory_id'])
    op.create_index('idx_feedback_agent', 'feedback_records', ['agent_id'])
    op.create_index('idx_feedback_rating', 'feedback_records', ['rating'])
    op.create_index('idx_feedback_time', 'feedback_records', ['timestamp'])

    # ── evolution_history — cycle execution records ─────────────────
    op.create_table(
        'evolution_history',
        sa.Column('cycle_id', sa.String(), nullable=False),
        sa.Column('timestamp', sa.String(), nullable=False),
        sa.Column('strategy_triggers', sa.String(), server_default='[]'),
        sa.Column('applied_mutations', sa.Integer(), server_default='0'),
        sa.Column('index_changes', sa.Integer(), server_default='0'),
        sa.Column('prune_candidates', sa.Integer(), server_default='0'),
        sa.Column('quality_alerts', sa.Integer(), server_default='0'),
        sa.Column('status', sa.String(), server_default='completed'),
        sa.Column('details', sa.String(), server_default='{}'),
        sa.PrimaryKeyConstraint('cycle_id'),
    )
    op.create_index('idx_evolution_history_time', 'evolution_history', ['timestamp'])
    op.create_index('idx_evolution_history_status', 'evolution_history', ['status'])

    # ── mutation_suggestions — pending/queued mutation proposals ─────
    op.create_table(
        'mutation_suggestions',
        sa.Column('suggestion_id', sa.String(), nullable=False),
        sa.Column('mutation_type', sa.String(), nullable=False),
        sa.Column('target_ids', sa.String(), server_default='[]'),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('reason', sa.String(), server_default=''),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('auto_applied', sa.Boolean(), server_default=sa.text('0')),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('applied_at', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('suggestion_id'),
    )
    op.create_index('idx_mutation_type', 'mutation_suggestions', ['mutation_type'])
    op.create_index('idx_mutation_status', 'mutation_suggestions', ['status'])
    op.create_index('idx_mutation_confidence', 'mutation_suggestions', ['confidence'])


def downgrade() -> None:
    op.drop_table('mutation_suggestions')
    op.drop_table('evolution_history')
    op.drop_table('feedback_records')
    op.drop_table('access_log')
