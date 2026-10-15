"""Flush-only operations; the caller owns commit/rollback and file compensation."""

from copy import deepcopy
from pathlib import PurePath
from uuid import UUID

from sqlalchemy import delete, select

from .models import NmmAsset, NmmPlan, NmmProject, NmmStep
from .schemas import AssetRole, NmmParams


class NmmRepository:
    def __init__(self, session):
        self.session = session

    def get_project(self, project_id):
        row = self.session.get(NmmProject, project_id)
        if row is None:
            raise ValueError('NMM project does not exist')
        return row

    def create_project(self, title, params=None):
        if not title.strip() or len(title) > 300:
            raise ValueError('Invalid project title')
        parsed = params if isinstance(params, NmmParams) else NmmParams.model_validate(params or {})
        row = NmmProject(title=title, params=parsed.model_dump())
        self.session.add(row)
        self.session.flush()
        return row

    def add_asset(self, project_id, *, revision=None, role, **fields):
        allowed = {
            'storage_name',
            'content_type',
            'size',
            'width',
            'height',
            'sha256',
            'sort_order',
            'meta',
        }
        if set(fields) - allowed:
            raise ValueError('Unsupported asset fields')
        project = self.get_project(project_id)
        revision = project.revision if revision is None else revision
        if revision != project.revision:
            raise ValueError('Asset revision is not current')
        name = fields.get('storage_name', '')
        if not name or PurePath(name).name != name or '\\' in name:
            raise ValueError('Asset storage name must be a UUID basename')
        try:
            UUID(PurePath(name).stem)
        except ValueError as exc:
            raise ValueError('Asset storage name must be a UUID basename') from exc
        row = NmmAsset(
            project_id=project.id, revision=revision, role=AssetRole(role).value, **deepcopy(fields)
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _asset(self, project, asset_id, role):
        row = self.session.get(NmmAsset, asset_id)
        if row is None or (row.project_id, row.revision, row.role) != (
            project.id,
            project.revision,
            role,
        ):
            raise ValueError('Asset project, revision or role mismatch')
        return row

    def select_candidate(self, project_id, asset_id):
        project = self.get_project(project_id)
        self._asset(project, asset_id, 'candidate')
        project.selected_candidate_id = asset_id
        self.session.flush()
        return project

    def create_plan(
        self, project_id, candidate_id, *, plan, palette, renderer_version, render_params=None
    ):
        project = self.get_project(project_id)
        self._asset(project, candidate_id, 'candidate')
        row = NmmPlan(
            project_id=project.id,
            revision=project.revision,
            candidate_id=candidate_id,
            plan=deepcopy(plan),
            palette=deepcopy(palette),
            renderer_version=renderer_version,
            render_params=deepcopy(render_params or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def add_step(
        self,
        plan_id,
        *,
        index,
        title,
        image_asset_id,
        diff_asset_id=None,
        description=None,
        warnings=None,
    ):
        plan = self.session.get(NmmPlan, plan_id)
        if plan is None:
            raise ValueError('NMM plan does not exist')
        project = self.get_project(plan.project_id)
        if plan.revision != project.revision:
            raise ValueError('Plan revision is not current')
        self._asset(project, image_asset_id, 'step')
        if diff_asset_id is not None:
            self._asset(project, diff_asset_id, 'step_diff')
        row = NmmStep(
            plan_id=plan_id,
            index=index,
            title=title,
            image_asset_id=image_asset_id,
            diff_asset_id=diff_asset_id,
            description=deepcopy(description or {}),
            warnings=deepcopy(warnings or []),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def delete_asset(self, asset_id):
        row = self.session.get(NmmAsset, asset_id)
        if row is None:
            raise ValueError('NMM asset does not exist')
        for project in self.session.scalars(
            select(NmmProject).where(NmmProject.selected_candidate_id == asset_id)
        ):
            project.selected_candidate_id = None
        self.session.flush()
        self.session.delete(row)
        self.session.flush()

    def delete_project(self, project_id):
        project = self.get_project(project_id)
        project.selected_candidate_id = None
        self.session.flush()
        plans = select(NmmPlan.id).where(NmmPlan.project_id == project_id)
        self.session.execute(delete(NmmStep).where(NmmStep.plan_id.in_(plans)))
        self.session.execute(delete(NmmPlan).where(NmmPlan.project_id == project_id))
        self.session.execute(delete(NmmAsset).where(NmmAsset.project_id == project_id))
        self.session.delete(project)
        self.session.flush()
