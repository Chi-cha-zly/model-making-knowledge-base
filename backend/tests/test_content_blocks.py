from test_api import client, source
from io import BytesIO
from zipfile import ZipFile
import json
import pytest


def image(client):
    return client.post('/api/v1/attachments', files={'file': ('x.png', b'\x89PNG\r\n\x1a\nxx', 'image/png')}).json()['id']


def test_blocks_roundtrip_and_edit_protection(client):
    aid = image(client)
    blocks = [{'type':'paragraph','text':'先打磨'}, {'type':'image','attachment_id':aid,'caption':'细节说明'}, {'type':'heading','text':'最后抛光'}]
    r = client.post('/api/v1/techniques', json={'title':'正文','original':True,'content_blocks':blocks})
    assert r.status_code == 201, r.text
    item = r.json()
    assert item['content_blocks'] == blocks
    assert 'steps' not in item
    assert item['attachment_ids'] == [aid]
    url = f"/api/v1/techniques/{item['id']}"
    assert client.patch(url,json={'steps':['丢失正文']}).status_code == 422
    assert client.patch(url,json={'attachment_ids':[]}).status_code == 422
    assert client.patch(url,json={'summary':'保留'}).json()['content_blocks'] == blocks
    assert client.patch(url,json={'content_blocks':[]}).json()['content_blocks'] == []


@pytest.mark.parametrize('blocks', [None, [{'type':'html','text':'x'}], [{'type':'image','attachment_id':True,'caption':''}], [{'type':'image','attachment_id':999,'caption':''}], [{'type':'image','attachment_id':1,'caption':'','url':'https://x'}], [{'type':'heading','text':'x'*301}], [{'type':'paragraph','text':'x'*20001}], [{'type':'paragraph','text':'x'}]*201, [{'type':'paragraph','text':'x'*20000}]*6])
def test_invalid_blocks_original_is_atomic(client, blocks):
    before = client.get('/api/v1/sources').json()
    response = client.post('/api/v1/techniques',json={'title':'失败','original':True,'content_blocks':blocks})
    assert response.status_code == 422, response.text
    assert client.get('/api/v1/sources').json() == before


def rewrite(raw, edit):
    out = BytesIO()
    with ZipFile(BytesIO(raw)) as src, ZipFile(out,'w') as dst:
        data = json.loads(src.read('manifest.json')); edit(data)
        for name in src.namelist():
            dst.writestr(name, json.dumps(data) if name == 'manifest.json' else src.read(name))
    return out.getvalue()


def restore(client, raw):
    return client.post('/api/v1/backup',files={'file':('backup.zip',raw,'application/zip')})


def test_backup_blocks_old_restore_and_invalid_nonmutation(client):
    aid = image(client)
    blocks = [{'type':'paragraph','text':'正文'},{'type':'image','attachment_id':aid,'caption':'图注'}]
    item = client.post('/api/v1/techniques',json={'title':'原文','original':True,'content_blocks':blocks}).json()
    raw = client.get('/api/v1/backup').content
    with ZipFile(BytesIO(raw)) as z:
        assert json.loads(z.read('manifest.json'))['version'] == 6
    assert restore(client,raw).status_code == 200
    assert client.get(f"/api/v1/techniques/{item['id']}").json()['content_blocks'] == blocks
    bad = rewrite(raw,lambda d:d['techniques'][0]['content_blocks'][1].update(attachment_id=999))
    assert restore(client,bad).status_code == 400
    assert client.get(f"/api/v1/techniques/{item['id']}").json()['content_blocks'] == blocks
    def legacy(d):
        d['version']=4
        for t in d['techniques']:
            t.pop('content_blocks',None)
            t['steps'] = ['旧步骤仍需保留']
    assert restore(client,rewrite(raw,legacy)).status_code == 200
    assert client.get(f"/api/v1/techniques/{item['id']}").json()['content_blocks'] == [{'type':'paragraph','text':'旧步骤仍需保留'}]


def test_blocks_union_unrelated_attachments_search_caption_and_withdrawn(client):
    aid, extra = image(client), image(client)
    r = client.post('/api/v1/techniques',json={'title':'正文','original':True,'attachment_ids':[extra], 'content_blocks':[{'type':'paragraph','text':'开头'},{'type':'image','attachment_id':aid,'caption':'独特检索图注'},{'type':'paragraph','text':'末尾'}]})
    assert r.status_code == 201, r.text
    item = r.json(); url = f"/api/v1/techniques/{item['id']}"
    assert set(item['attachment_ids']) == {aid,extra}
    assert client.get('/api/v1/search',params={'q':'独特检索图注'}).json()['techniques'][0]['id'] == item['id']
    assert client.patch(url,json={'content_blocks':[{'type':'heading','text':'替换'}]}).json()['attachment_ids'] == item['attachment_ids']
    assert client.delete(url).json()['status'] == 'withdrawn'
    assert client.patch(url,json={'content_blocks':[{'type':'paragraph','text':'更新'}]}).json()['status'] == 'withdrawn'


def test_patch_null_blocks_and_legacy_steps_rejected(client):
    item = client.post('/api/v1/techniques',json={'title':'旧文','original':True,'content_blocks':[{'type':'paragraph','text':'旧步骤'}]}).json()
    url = f"/api/v1/techniques/{item['id']}"
    assert item['content_blocks'] == [{'type':'paragraph','text':'旧步骤'}]
    assert client.patch(url,json={'content_blocks':None}).status_code == 422
    assert client.patch(url,json={'steps':['改旧步骤']}).status_code == 422


@pytest.mark.parametrize('change', ['unlinked', 'inactive', 'foreign', 'unknown_field'])
def test_backup_invalid_block_relations_leave_database_and_images(client, change):
    aid = image(client)
    blocks = [{'type':'image','attachment_id':aid,'caption':'证据'}]
    item = client.post('/api/v1/techniques',json={'title':'保留','original':True,'content_blocks':blocks}).json()
    raw = client.get('/api/v1/backup').content
    def corrupt(d):
        if change == 'unlinked': d['techniques'][0]['attachment_ids'] = []
        elif change == 'inactive': d['attachments'][0]['status'] = 'withdrawn'
        elif change == 'foreign': d['attachments'][0]['owner_id'] = 'other'
        else: d['techniques'][0]['content_blocks'][0]['url'] = 'https://example.com/x.png'
    assert restore(client,rewrite(raw,corrupt)).status_code == 400
    assert client.get(f"/api/v1/techniques/{item['id']}").json()['content_blocks'] == blocks
    assert client.get(f'/api/v1/attachments/{aid}').status_code == 200


@pytest.mark.parametrize('with_blocks', [False, True])
def test_patch_null_attachment_ids_rejected_atomically(client, with_blocks):
    aid = image(client)
    blocks = [{'type':'image','attachment_id':aid,'caption':'保留'}]
    item = client.post('/api/v1/techniques',json={'title':'原文','original':True,'content_blocks':blocks}).json()
    payload = {'attachment_ids':None}
    if with_blocks: payload['content_blocks'] = [{'type':'paragraph','text':'替换'}]
    response = client.patch(f"/api/v1/techniques/{item['id']}",json=payload)
    assert response.status_code == 422
    assert client.get(f"/api/v1/techniques/{item['id']}").json() == item


def test_backup_null_modern_blocks_rejected_without_erasing_content(client):
    item = client.post('/api/v1/techniques', json={'title':'保留','original':True,
        'content_blocks':[{'type':'paragraph','text':'不得丢失'}]}).json()
    raw = client.get('/api/v1/backup').content
    bad = rewrite(raw, lambda data: data['techniques'][0].update(content_blocks=None))
    assert restore(client, bad).status_code == 400
    assert client.get(f"/api/v1/techniques/{item['id']}").json()['content_blocks'] == [{'type':'paragraph','text':'不得丢失'}]


def test_backup_preserves_image_analysis_and_clears_old_job_identity(client):
    from sqlalchemy.orm import Session
    from app.models import Attachment
    from app.vision_models import ImageAnalysis
    from app.job_models import Job
    aid = image(client)
    client.post('/api/v1/techniques', json={'title':'图文','original':True,
        'content_blocks':[{'type':'image','attachment_id':aid,'caption':'图注'}]}).raise_for_status()
    description = {'description':'青绿色罩染', 'materials':['黄铜'], 'techniques':['罩染'],
        'colors':['青绿色'], 'step_number':1, 'visible_text':'', 'uncertainties':['材料仅为推断']}
    with Session(client.app.state.engine) as db:
        attachment = db.get(Attachment,aid)
        db.add(ImageAnalysis(attachment_id=aid,image_hash=attachment.sha256, model='vision-test',
            ocr_text='真实 OCR 文字', description=description))
        db.add(Job(kind='experiment',request_id='experiment:1',request_hash='old',status='completed'))
        db.commit()
    archive = client.get('/api/v1/backup').content
    with ZipFile(BytesIO(archive)) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['version'] == 6
        assert manifest['image_analyses'][0]['description'] == description
    assert restore(client, archive).status_code == 200
    with Session(client.app.state.engine) as db:
        assert db.get(ImageAnalysis,aid).ocr_text == '真实 OCR 文字'
        assert db.query(Job).count() == 0
    assert client.get('/api/v1/search', params={'q':'青绿色罩染'}).json()['ranking']
    for changes in ({'image_hash':'0'*64}, {'attachment_id':999}, {'model':''}, {'description':{'description':'bad'}}):
        corrupted = rewrite(archive, lambda data: data['image_analyses'][0].update(changes))
        assert restore(client,corrupted).status_code == 400
        assert client.get('/api/v1/search', params={'q':'青绿色罩染'}).json()['ranking']


def test_failed_restore_rolls_back_sequence_restart(client, monkeypatch):
    from pathlib import Path
    from app.services import backups
    source(client)
    image(client)
    raw = client.get('/api/v1/backup').content
    second = client.post('/api/v1/sources', json={'title':'保留来源'}).json()['id']
    original = backups.os.replace
    def fail_final(src, dst):
        if Path(dst).parent == client.app.state.storage and Path(dst).suffix == '.png':
            raise OSError('simulated final image write failure')
        return original(src, dst)
    monkeypatch.setattr(backups.os, 'replace', fail_final)
    assert restore(client,raw).status_code == 500
    monkeypatch.setattr(backups.os,'replace',original)
    added = client.post('/api/v1/sources', json={'title':'下一条来源'})
    assert added.status_code == 201
    assert added.json()['id'] > second


def test_legacy_restore_preserves_experiment_jobs_only(client):
    from sqlalchemy.orm import Session
    from app.job_models import Job
    source(client)
    archive = client.get('/api/v1/backup').content
    with Session(client.app.state.engine) as db:
        for kind in ('experiment','embedding','vision'):
            db.add(Job(kind=kind,request_id='old',request_hash='old',status='completed'))
        db.commit()
    assert restore(client,rewrite(archive,lambda data: data.update(version=3))).status_code == 200
    with Session(client.app.state.engine) as db:
        assert [row.kind for row in db.query(Job).all()] == ['experiment']
