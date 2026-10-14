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
    assert item['steps'] == ['先打磨','细节说明','最后抛光']
    assert item['attachment_ids'] == [aid]
    url = f"/api/v1/techniques/{item['id']}"
    assert client.patch(url,json={'steps':['丢失正文']}).status_code == 422
    assert client.patch(url,json={'attachment_ids':[]}).status_code == 422
    assert client.patch(url,json={'summary':'保留'}).json()['content_blocks'] == blocks
    assert client.patch(url,json={'content_blocks':[]}).json()['steps'] == []


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
        assert json.loads(z.read('manifest.json'))['version'] == 5
    assert restore(client,raw).status_code == 200
    assert client.get(f"/api/v1/techniques/{item['id']}").json()['content_blocks'] == blocks
    bad = rewrite(raw,lambda d:d['techniques'][0]['content_blocks'][1].update(attachment_id=999))
    assert restore(client,bad).status_code == 400
    assert client.get(f"/api/v1/techniques/{item['id']}").json()['content_blocks'] == blocks
    def legacy(d):
        d['version']=4
        for t in d['techniques']: t.pop('content_blocks',None)
    assert restore(client,rewrite(raw,legacy)).status_code == 200
    assert client.get(f"/api/v1/techniques/{item['id']}").json()['content_blocks'] == []


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


def test_patch_null_blocks_rejected_and_legacy_steps_preserved(client):
    item = client.post('/api/v1/techniques',json={'title':'旧文','original':True,'steps':['旧步骤']}).json()
    url = f"/api/v1/techniques/{item['id']}"
    assert item['content_blocks'] == []
    assert client.patch(url,json={'content_blocks':None}).status_code == 422
    assert client.patch(url,json={'steps':['改旧步骤']}).json()['steps'] == ['改旧步骤']


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
