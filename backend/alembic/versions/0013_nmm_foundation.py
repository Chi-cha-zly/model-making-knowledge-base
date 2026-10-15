"""NMM local projects, versioned assets and deterministic plans."""

from alembic import op

revision = '0013_nmm_foundation'
down_revision = '0012_agent_traces'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE nmm_projects (
    id UUID NOT NULL,
    title VARCHAR(300) NOT NULL,
    status VARCHAR(40) NOT NULL,
    phase VARCHAR(40) NOT NULL,
    params JSON NOT NULL,
    revision INTEGER NOT NULL,
    input_snapshots JSON NOT NULL,
    analysis JSON,
    selected_candidate_id UUID,
    workflow_thread_id VARCHAR(200),
    active_job_id INTEGER,
    generation_count INTEGER NOT NULL,
    execution JSON NOT NULL,
    warnings JSON NOT NULL,
    error TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CHECK (revision >= 1),
    CHECK (generation_count >= 0),
    CHECK (status IN (
        'draft','generating','awaiting_confirmation','planning',
        'rendering','done','failed','stopped'
    )),
    FOREIGN KEY(active_job_id) REFERENCES jobs (id) ON DELETE SET NULL
)
""")
    op.execute(
        'CREATE INDEX ix_nmm_projects_status_updated_at ON nmm_projects (status, updated_at)'
    )
    op.execute("""
CREATE TABLE nmm_assets (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    role VARCHAR(40) NOT NULL,
    storage_name TEXT NOT NULL,
    content_type VARCHAR(40) NOT NULL,
    size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    sort_order INTEGER NOT NULL,
    meta JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CHECK (revision >= 1),
    CHECK (size > 0 AND width > 0 AND height > 0),
    CHECK (sort_order >= 0),
    CHECK (content_type IN ('image/png', 'image/jpeg', 'image/webp')),
    CHECK (length(sha256) = 64),
    CHECK (role IN (
        'original','mask','reference_style','reference_palette','reference_steps',
        'candidate','final','step','step_diff','contact_sheet'
    )),
    FOREIGN KEY(project_id) REFERENCES nmm_projects (id) ON DELETE CASCADE,
    UNIQUE (storage_name)
)
""")
    op.execute('CREATE INDEX ix_nmm_assets_project_id ON nmm_assets (project_id)')
    op.execute(
        'CREATE INDEX ix_nmm_assets_project_revision_role_sort '
        'ON nmm_assets (project_id, revision, role, sort_order)'
    )
    op.execute(
        'CREATE UNIQUE INDEX uq_nmm_assets_input_role '
        'ON nmm_assets (project_id, revision, role) '
        "WHERE role IN ('original', 'mask')"
    )
    op.execute("""
CREATE TABLE nmm_plans (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    candidate_id UUID NOT NULL,
    plan JSON NOT NULL,
    palette JSON NOT NULL,
    renderer_version VARCHAR(100) NOT NULL,
    render_params JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CHECK (revision >= 1),
    FOREIGN KEY(project_id) REFERENCES nmm_projects (id) ON DELETE CASCADE,
    FOREIGN KEY(candidate_id) REFERENCES nmm_assets (id) ON DELETE RESTRICT
)
""")
    op.execute('CREATE INDEX ix_nmm_plans_project_id ON nmm_plans (project_id)')
    op.execute('CREATE INDEX ix_nmm_plans_project_revision ON nmm_plans (project_id, revision)')
    op.execute("""
CREATE TABLE nmm_steps (
    id UUID NOT NULL,
    plan_id UUID NOT NULL,
    index INTEGER NOT NULL,
    title VARCHAR(300) NOT NULL,
    image_asset_id UUID NOT NULL,
    diff_asset_id UUID,
    description JSON NOT NULL,
    warnings JSON NOT NULL,
    PRIMARY KEY (id),
    CHECK (index >= 1 AND index <= 9),
    UNIQUE (plan_id, index),
    FOREIGN KEY(plan_id) REFERENCES nmm_plans (id) ON DELETE CASCADE,
    FOREIGN KEY(image_asset_id) REFERENCES nmm_assets (id) ON DELETE RESTRICT,
    FOREIGN KEY(diff_asset_id) REFERENCES nmm_assets (id) ON DELETE RESTRICT
)
""")
    op.execute('CREATE INDEX ix_nmm_steps_plan_id ON nmm_steps (plan_id)')
    op.execute(
        'ALTER TABLE nmm_projects ADD CONSTRAINT fk_nmm_selected_candidate '
        'FOREIGN KEY(selected_candidate_id) REFERENCES nmm_assets (id) ON DELETE SET NULL'
    )


def downgrade():
    op.drop_constraint('fk_nmm_selected_candidate', 'nmm_projects', type_='foreignkey')
    op.drop_table('nmm_steps')
    op.drop_table('nmm_plans')
    op.drop_table('nmm_assets')
    op.drop_table('nmm_projects')
