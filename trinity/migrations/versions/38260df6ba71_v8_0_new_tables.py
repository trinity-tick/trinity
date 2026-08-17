"""v8_0_new_tables — Trinity v8.0.0 五张新表迁移

Revision ID: 38260df6ba71
Revises:
Create Date: 2026-08-11 13:00:07.788053

Tables:
  - identity_anchors        (Multi-Anchor Identity,  arXiv 2604.09588)
  - audit_runs              (DCSA-EJP 双循环审计运行记录)
  - constitutional_violations (DCSA-EJP 宪法违规记录)
  - a2a_tasks               (A2A v0.3 跨 Agent 任务追踪)
  - agent_registry          (A2A v0.3 Agent 注册中心)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '38260df6ba71'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. identity_anchors — 多锚点身份 ──────────────────────────
    op.create_table(
        'identity_anchors',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('agent_id', sa.String(), nullable=False),
        sa.Column('anchor_type', sa.String(), nullable=False),
        sa.Column('content', sa.String(), nullable=False, server_default='{}'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('checksum', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column('updated_at', sa.String(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index('idx_identity_anchors_agent', 'identity_anchors', ['agent_id'])
    op.create_index('idx_identity_anchors_type', 'identity_anchors', ['agent_id', 'anchor_type'])

    # ── 2. audit_runs — DCSA-EJP 审计运行 ─────────────────────────
    op.create_table(
        'audit_runs',
        sa.Column('run_id', sa.String(), primary_key=True),
        sa.Column('agent_id', sa.String(), nullable=False),
        sa.Column('task', sa.String(), server_default=''),
        sa.Column('executor_result', sa.String(), server_default='{}'),
        sa.Column('auditor_result', sa.String(), server_default='{}'),
        sa.Column('disagreement_flag', sa.Integer(), server_default='0'),
        sa.Column('packet_json', sa.String(), server_default='{}'),
        sa.Column('created_at', sa.String(), server_default=sa.text("(datetime('now'))")),
    )
    op.create_index('idx_audit_runs_agent', 'audit_runs', ['agent_id'])
    op.create_index('idx_audit_runs_time', 'audit_runs', ['created_at'])

    # ── 3. constitutional_violations — 宪法违规 ────────────────────
    op.create_table(
        'constitutional_violations',
        sa.Column('violation_id', sa.String(), primary_key=True),
        sa.Column('run_id', sa.String(), nullable=False),
        sa.Column('invariant', sa.String(), nullable=False),
        sa.Column('severity', sa.String(), nullable=False, server_default='medium'),
        sa.Column('context', sa.String(), server_default='{}'),
        sa.Column('timestamp', sa.String(), server_default=sa.text("(datetime('now'))")),
    )
    op.create_index('idx_violations_run', 'constitutional_violations', ['run_id'])
    op.create_index('idx_violations_invariant', 'constitutional_violations', ['invariant'])

    # ── 4. a2a_tasks — 跨 Agent 任务追踪 ─────────────────────────
    op.create_table(
        'a2a_tasks',
        sa.Column('task_id', sa.String(), primary_key=True),
        sa.Column('from_agent', sa.String(), nullable=False),
        sa.Column('to_agent', sa.String(), nullable=False),
        sa.Column('payload', sa.String(), server_default='{}'),
        sa.Column('status', sa.String(), server_default='pending'),
        sa.Column('result', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column('updated_at', sa.String(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index('idx_a2a_tasks_from', 'a2a_tasks', ['from_agent'])
    op.create_index('idx_a2a_tasks_to', 'a2a_tasks', ['to_agent'])
    op.create_index('idx_a2a_tasks_status', 'a2a_tasks', ['status'])

    # ── 5. agent_registry — Agent 注册中心 ────────────────────────
    op.create_table(
        'agent_registry',
        sa.Column('agent_id', sa.String(), primary_key=True),
        sa.Column('card_json', sa.String(), nullable=False, server_default='{}'),
        sa.Column('registered_at', sa.String(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column('last_heartbeat', sa.String(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column('status', sa.String(), server_default='active'),
    )
    op.create_index('idx_agent_registry_status', 'agent_registry', ['status'])


def downgrade() -> None:
    op.drop_table('agent_registry')
    op.drop_table('a2a_tasks')
    op.drop_table('constitutional_violations')
    op.drop_table('audit_runs')
    op.drop_table('identity_anchors')
