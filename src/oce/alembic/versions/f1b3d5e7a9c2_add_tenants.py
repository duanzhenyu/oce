"""add tenants, tenant_blobs and chains.tenant_id

Revision ID: f1b3d5e7a9c2
Revises: e2f4a6c8d0b1
Create Date: 2026-10-14 10:00:00.000000

多租户：blob/chunk/向量仍全局共享去重，tenant_blobs 记录租户对 blob 的归属，
chains 归属到租户。存量 blob 与 chain 全部归内置 default 租户（其 key 是 env
API_KEY，故 api_key_hash 为 NULL）。chains.tenant_id 不加外键，SQLite 才能直接
ADD COLUMN；删列时 SQLite 走 batch 重建表。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1b3d5e7a9c2'
down_revision: Union[str, None] = 'e2f4a6c8d0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tenants',
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('api_key_hash', sa.String(length=64), nullable=True),
        sa.Column('api_key_last4', sa.String(length=4), server_default='', nullable=False),
        sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('tenant_id'),
        sa.UniqueConstraint('name', name='uq_tenants_name'),
        sa.UniqueConstraint('api_key_hash', name='uq_tenants_api_key_hash'),
    )
    op.execute(
        "INSERT INTO tenants (tenant_id, name, status) VALUES ('default', 'default', 'active')"
    )

    op.create_table(
        'tenant_blobs',
        sa.Column('tenant_id', sa.String(length=64), nullable=False),
        sa.Column('blob_name', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.tenant_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['blob_name'], ['blobs.blob_name'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id', 'blob_name'),
    )
    op.create_index('ix_tenant_blobs_blob_name', 'tenant_blobs', ['blob_name'])
    op.execute(
        "INSERT INTO tenant_blobs (tenant_id, blob_name) SELECT 'default', blob_name FROM blobs"
    )

    op.add_column(
        'chains',
        sa.Column('tenant_id', sa.String(length=64), server_default='default', nullable=False),
    )
    op.create_index('ix_chains_tenant_id', 'chains', ['tenant_id'])


def downgrade() -> None:
    op.drop_index('ix_chains_tenant_id', table_name='chains')
    with op.batch_alter_table('chains') as batch_op:
        batch_op.drop_column('tenant_id')
    op.drop_index('ix_tenant_blobs_blob_name', table_name='tenant_blobs')
    op.drop_table('tenant_blobs')
    op.drop_table('tenants')
