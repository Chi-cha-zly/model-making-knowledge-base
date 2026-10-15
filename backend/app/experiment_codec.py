from datetime import datetime
import json
import re
from .experiment_schemas import ExperimentInput, BackupPayload
from .experiment_metrics import answer_metrics
from .evaluation_schemas import Case
from .evaluation_metrics import fingerprint, case_metrics

ACTIVE = ('queued', 'running')


def validate_experiment_backup(data):
    if data['version'] < 4:
        return
    if not isinstance(data.get('experiments'), list):
        raise ValueError('Missing experiments')
    ids = set()
    requests = set()
    for record in data['experiments']:
        if not isinstance(record, dict) or set(record) != {
            'request_id',
            'request_hash',
            'payload',
            'snapshot',
        }:
            raise ValueError('Invalid experiment record')
        p = record['payload']
        s = record['snapshot']
        BackupPayload.model_validate(p)
        if not isinstance(s, dict):
            raise ValueError('Invalid snapshot')
        if set(s) not in (
            {'request', 'cases', 'retrieval_version', 'corpus_hash'},
            {'request', 'cases', 'retrieval_version', 'corpus_hash', 'system_prompt'},
        ):
            raise ValueError('Invalid snapshot fields')
        if 'system_prompt' in s and (
            not isinstance(s['system_prompt'], str) or len(s['system_prompt']) > 16000
        ):
            raise ValueError('Invalid system prompt')
        if not re.fullmatch('[0-9a-f]{64}', s['corpus_hash']) or not isinstance(
            s['retrieval_version'], str
        ):
            raise ValueError('Invalid snapshot metadata')
        req = ExperimentInput.model_validate(s['request'])
        if p['dataset_id'] != req.dataset_id:
            raise ValueError('Invalid dataset identity')
        if (
            req.request_id != record['request_id']
            or fingerprint(req.model_dump()) != record['request_hash']
        ):
            raise ValueError('Invalid experiment request')
        if fingerprint(s) != p['snapshot_hash']:
            raise ValueError('Invalid experiment snapshot hash')
        if type(p['id']) is not int or p['id'] < 1 or p['id'] in ids or req.request_id in requests:
            raise ValueError('Duplicate experiment identity')
        ids.add(p['id'])
        requests.add(req.request_id)
        if (
            p['status'] not in (*ACTIVE, 'completed', 'failed', 'stopped')
            or p['mode'] != req.mode
            or p['corpus'] not in ('live', 'demo')
        ):
            raise ValueError('Invalid experiment status')
        if not isinstance(s['cases'], list) or not 1 <= len(s['cases']) <= req.limit:
            raise ValueError('Invalid experiment cases')
        case_ids = set()
        for case in s['cases']:
            if not isinstance(case, dict) or not isinstance(case.get('evidence'), dict):
                raise ValueError('Invalid case snapshot')
            Case.model_validate({k: v for k, v in case.items() if k != 'evidence'})
            if case['id'] in case_ids:
                raise ValueError('Duplicate case')
            case_ids.add(case['id'])
            if set(case['evidence']) != {'baseline', 'ranked'}:
                raise ValueError('Invalid evidence')
            for hits in case['evidence'].values():
                if (
                    not isinstance(hits, list)
                    or len(hits) > req.k
                    or len(json.dumps(hits, ensure_ascii=False)) > 25000
                ):
                    raise ValueError('Invalid evidence budget')
                if any(
                    not isinstance(h, dict)
                    or not re.fullmatch(r'(technique|product):[1-9][0-9]*', h.get('key', ''))
                    or not isinstance(h.get('excerpt'), str)
                    for h in hits
                ):
                    raise ValueError('Invalid evidence hit')
        variants = p['models'] if p['mode'] == 'answers' else ['baseline', 'ranked']
        if not 1 <= len(variants) <= 2 or len(set(variants)) != len(variants):
            raise ValueError('Invalid variants')
        if (
            p['total'] != len(s['cases']) * len(variants)
            or type(p['progress']) is not int
            or p['progress'] != len(p['results'])
            or not 0 <= p['progress'] <= p['total']
        ):
            raise ValueError('Invalid experiment progress')
        if p['status'] == 'completed' and p['progress'] != p['total']:
            raise ValueError('Incomplete experiment')
        seen = set()
        for result in p['results']:
            pair = (result['variant'], result['case_id'])
            if pair in seen or pair[0] not in variants or pair[1] not in case_ids:
                raise ValueError('Invalid experiment result')
            seen.add(pair)
            case = next(c for c in s['cases'] if c['id'] == pair[1])
            hits = case['evidence'][pair[0] if p['mode'] == 'retrieval' else 'ranked']
            if result['query'] != case['query'] or result['hit_keys'] != [h['key'] for h in hits]:
                raise ValueError('Invalid result snapshot')
            expected = case_metrics(hits, case['judgments'], req.k)
            if p['mode'] == 'answers':
                if not isinstance(result['answer'], str) or not 1 <= len(result['answer']) <= 32000:
                    raise ValueError('Invalid answer')
                expected.update(answer_metrics(result['answer'], hits))
            if result['metrics'] != expected:
                raise ValueError('Invalid mechanical metrics')
        for key in ('created_at', 'updated_at'):
            datetime.fromisoformat(p[key])
        for link in p['links']:
            from urllib.parse import urlsplit

            url = urlsplit(link['url'])
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError('Invalid experiment link')
