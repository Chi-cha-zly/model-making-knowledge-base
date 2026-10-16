"""Bounded, row-locked authorizations and durable per-call records.

A reservation is never refunded. This is a call-count budget, not a monetary
ceiling or a claim that an external service executes a request exactly once.
"""

import json
from copy import deepcopy
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion import digest
from app.models import now
from app.services.errors import ServiceError
from .models import NmmProject
from .provider_types import NmmProviderError
from .schemas import NmmParams

MAX_RECORDS = 64
MAX_CACHE_BYTES = 512 * 1024


class CostAuthorization(BaseModel):
    model_config = ConfigDict(extra='forbid')
    estimate_id: str = Field(min_length=1, max_length=100)
    max_image_calls: StrictInt = Field(ge=0, le=6)
    max_vision_calls: StrictInt = Field(ge=0, le=30)
    accept_unknown_cost: StrictBool

    @model_validator(mode='after')
    def nonempty(self):
        if not self.max_image_calls and not self.max_vision_calls:
            raise ValueError('At least one call must be authorized')
        return self


class NmmBilling:
    def __init__(self, engine, configuration):
        self.engine = engine
        self.configuration = deepcopy(configuration)

    def _row(self, db, project_id):
        project = db.scalar(
            select(NmmProject).where(NmmProject.id == UUID(str(project_id))).with_for_update()
        )
        if project is None:
            raise ServiceError(404, 'NMM 项目不存在。')
        return project

    def _account(self, project):
        return deepcopy(project.meta.get('accounting', {'quotes': [], 'grants': [], 'calls': []}))

    def _save(self, project, account):
        project.meta = {**project.meta, 'accounting': account}

    def _binding(self, project, params=None):
        target = (
            NmmParams.model_validate(params).model_dump() if params is not None else project.params
        )
        inputs = project.input_snapshots.get(str(project.revision), {}).get('assets', [])
        return {
            'revision': project.revision,
            'target_revision': project.revision + (target != project.params),
            'params_hash': digest(target),
            'inputs_hash': digest(sorted((item['role'], item['sha256']) for item in inputs)),
            'configuration_id': self.configuration['configuration_id'],
        }

    def estimate(self, project_id, *, params=None):
        with Session(self.engine) as db, db.begin():
            project = self._row(db, project_id)
            account = self._account(project)
            binding = self._binding(project, params)
            quote = next(
                (q for q in account['quotes'] if q['binding'] == binding and not q['used']), None
            )
            if quote is None:
                if len(account['quotes']) >= MAX_RECORDS:
                    raise ServiceError(409, '项目估算记录已达上限，请创建新项目。')
                quote = {'id': str(uuid4()), 'binding': binding, 'used': False}
                account['quotes'].append(quote)
                self._save(project, account)
            return {
                'estimate_id': quote['id'],
                'target_revision': binding['target_revision'],
                'current_config': deepcopy(self.configuration['providers']),
                'requires_authorization': True,
                'remaining_image_calls': max(0, 6 - project.generation_count),
                'remaining_vision_calls': max(
                    0, 30 - sum(c['kind'] == 'vision' for c in account['calls'])
                ),
                'cost_notice': '总费用未知；授权仅限制调用次数，不是金额上限。',
            }

    def authorize(self, project_id, value, *, params=None):
        parsed = CostAuthorization.model_validate(value)
        if not parsed.accept_unknown_cost:
            raise ServiceError(422, '需要明确接受总费用未知，并确认调用次数上限。')
        with Session(self.engine) as db, db.begin():
            project = self._row(db, project_id)
            account = self._account(project)
            quote = next((q for q in account['quotes'] if q['id'] == parsed.estimate_id), None)
            if quote is None or quote['binding'] != self._binding(project, params):
                raise ServiceError(409, '估算已失效，请按当前版本、参数和模型重新估算。')
            limits = {'image': parsed.max_image_calls, 'vision': parsed.max_vision_calls}
            existing = next((g for g in account['grants'] if g['quote_id'] == quote['id']), None)
            if existing:
                if existing['limits'] != limits:
                    raise ServiceError(409, '同一估算不能以不同额度重复批准。')
                return deepcopy(existing)
            if len(account['grants']) >= MAX_RECORDS:
                raise ServiceError(409, '项目授权记录已达上限。')
            remaining = {
                'image': max(0, 6 - project.generation_count),
                'vision': 30 - sum(c['kind'] == 'vision' for c in account['calls']),
            }
            if any(limits[kind] > remaining[kind] for kind in limits):
                raise ServiceError(409, '批准次数超过项目剩余额度。')
            grant = {
                'id': str(uuid4()),
                'quote_id': quote['id'],
                'binding': {**quote['binding'], 'revision': quote['binding']['target_revision']},
                'limits': limits,
                'used': {'image': 0, 'vision': 0},
                'thread': None,
                'disabled': False,
            }
            quote['used'] = True
            account['grants'].append(grant)
            account['current_grant'] = grant['id']
            self._save(project, account)
            return deepcopy(grant)

    def _grant(self, project, account, thread, kind=None):
        grant = next(
            (g for g in account['grants'] if g['id'] == account.get('current_grant')), None
        )
        binding = self._binding(project)
        if (
            grant is None
            or grant['disabled']
            or grant['binding'] != binding
            or grant['thread'] not in (None, thread)
        ):
            raise ServiceError(409, '本次调用没有有效授权，请重新估算并明确批准。')
        if kind and grant['used'][kind] >= grant['limits'][kind]:
            raise ServiceError(409, '本次授权额度已用尽，请重新估算并批准。')
        return grant

    def check_access(self, project_id, thread, *, kind=None):
        with Session(self.engine) as db, db.begin():
            project = self._row(db, project_id)
            self._grant(project, self._account(project), thread, kind)

    def has_completed_vision(self, project, logical_key):
        return any(
            call['thread'] == project.workflow_thread_id
            and call['revision'] == project.revision
            and call['configuration_id'] == self.configuration['configuration_id']
            and call['kind'] == 'vision'
            and call['logical_key'] == logical_key
            and call['status'] == 'completed'
            for call in self._account(project)['calls']
        )

    def reserve(
        self,
        project_id,
        *,
        revision,
        thread,
        kind,
        logical_key,
        fingerprint,
        guard=None,
        usage=None,
    ):
        if kind not in ('image', 'vision'):
            raise ValueError('Invalid call kind')
        with Session(self.engine) as db, db.begin():
            project = self._row(db, project_id)
            if guard is not None:
                guard(db)
            if (
                project.revision != revision
                or project.workflow_thread_id != thread
                or project.status not in ('generating', 'planning', 'rendering')
            ):
                raise ServiceError(409, '项目版本或执行状态已改变。')
            account = self._account(project)
            previous = [
                c
                for c in account['calls']
                if c['logical_key'] == logical_key and c['thread'] == thread
            ]
            prior = previous[-1] if previous else None
            if prior:
                if (
                    prior['fingerprint'] != fingerprint
                    or prior['configuration_id'] != self.configuration['configuration_id']
                ):
                    raise ServiceError(409, '恢复调用的输入或配置已改变，请开始新的操作。')
                if prior['status'] == 'completed' and kind == 'vision':
                    return prior['id'], deepcopy(prior['result'])
                if prior['error'] and prior['error']['code'] == 'invalid_output':
                    raise NmmProviderError(**prior['error'])
            grant = self._grant(project, account, thread, kind)
            if prior and prior['grant_id'] == grant['id']:
                raise ServiceError(409, '此调用已有结果或结果未知，不能自动重复发送。')
            count = sum(c['kind'] == kind for c in account['calls'])
            if count >= (6 if kind == 'image' else 30) or (
                kind == 'image' and project.generation_count >= 6
            ):
                raise ServiceError(409, '项目调用次数已达上限。')
            grant['thread'] = thread
            grant['used'][kind] += 1
            call = {
                'id': str(uuid4()),
                'logical_key': logical_key,
                'fingerprint': fingerprint,
                'configuration_id': self.configuration['configuration_id'],
                'grant_id': grant['id'],
                'revision': revision,
                'thread': thread,
                'kind': kind,
                'status': 'requested',
                'created_at': now().isoformat(),
                'usage': deepcopy(usage),
                'error': None,
                'result': None,
            }
            account['calls'].append(call)
            if kind == 'image':
                project.generation_count += 1
            self._save(project, account)
            return call['id'], None

    def finish(self, project_id, call_id, *, status, result=None, usage=None, error=None):
        if status not in ('completed', 'failed', 'unknown'):
            raise ValueError('Invalid call completion')
        if (
            result is not None
            and len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CACHE_BYTES
        ):
            raise NmmProviderError('invalid_output', '模型输出超过保存上限。')
        with Session(self.engine) as db, db.begin():
            project = db.scalar(
                select(NmmProject).where(NmmProject.id == UUID(str(project_id))).with_for_update()
            )
            if project is None:
                return
            account = self._account(project)
            call = next(c for c in account['calls'] if c['id'] == call_id)
            if call['status'] != 'requested':
                return
            call.update(
                status=status, result=deepcopy(result), usage=deepcopy(usage), error=deepcopy(error)
            )
            if status == 'unknown':
                next(g for g in account['grants'] if g['id'] == call['grant_id'])['disabled'] = True
            self._save(project, account)

    def recover(self):
        with Session(self.engine) as db, db.begin():
            for project in db.scalars(select(NmmProject).with_for_update()):
                account = self._account(project)
                changed = False
                for call in account['calls']:
                    if call['status'] == 'requested':
                        call['status'] = 'unknown'
                        call['error'] = {
                            'code': 'network',
                            'message': '服务中断，调用结果未知。',
                            'retryable': True,
                        }
                        next(g for g in account['grants'] if g['id'] == call['grant_id'])[
                            'disabled'
                        ] = True
                        changed = True
                if changed:
                    self._save(project, account)

    def summary(self, project_id):
        with Session(self.engine) as db:
            project = db.get(NmmProject, UUID(str(project_id)))
            return self.public_meta(project)

    def public_meta(self, project):
        account = self._account(project)
        calls = [
            {
                k: deepcopy(c[k])
                for k in ('id', 'kind', 'revision', 'status', 'created_at', 'usage', 'error')
            }
            for c in account['calls']
        ]
        costs = {}
        unknown = 0
        for call in calls:
            usage = call['usage'] or {}
            cost, currency = usage.get('estimated_cost'), usage.get('currency')
            if cost is None or not currency:
                unknown += 1
            else:
                costs[currency] = round(costs.get(currency, 0) + cost, 10)
        grant = next(
            (g for g in account['grants'] if g['id'] == account.get('current_grant')), None
        )
        return {
            'usage': {
                'calls': calls,
                'reserved_image_calls': sum(c['kind'] == 'image' for c in calls),
                'reserved_vision_calls': sum(c['kind'] == 'vision' for c in calls),
                'known_costs': costs,
                'unknown_cost_calls': unknown,
            },
            'authorization': {
                'remaining_image_calls': grant['limits']['image'] - grant['used']['image'],
                'remaining_vision_calls': grant['limits']['vision'] - grant['used']['vision'],
                'disabled': grant['disabled'],
            }
            if grant
            else None,
            'error_info': deepcopy(project.meta.get('error_info')),
        }
