"""Behavior checks for shared local retrieval, independent of model providers."""

from pg_test_support import test_database_url
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import retrieval
from app.chat import evidence
from app.models import Product, ProductAlias, Source, Technique


def document(id, title, text='', *, kind='technique', sku='', aliases=()):
    return {
        'key': f'{kind}:{id}',
        'type': kind,
        'id': id,
        'title': title,
        'text': text,
        'excerpt': text[:3000],
        'sku': sku,
        'aliases': list(aliases),
        'source_ids': [1],
        'sources': [{'id': 1, 'title': '人工资料', 'url': None}],
    }


def keys(documents, query, **kwargs):
    return [hit['key'] for hit in retrieval.rank_documents(documents, query, **kwargs)]


def test_normalized_identifier_precedes_repeated_body_and_does_not_match_prefix():
    docs = [
        document(1, '水性漆', kind='product', sku='XF-2'),
        document(2, 'XF20', kind='product', sku='XF-20'),
        document(3, '经验记录', 'xf 2 白色 ' * 80),
    ]
    hits = retrieval.rank_documents(docs, '请找 ＸＦ ２ 白色')
    assert hits[0]['key'] == 'product:1'
    assert 'product:2' not in [hit['key'] for hit in hits]
    assert any('编号' in reason for reason in hits[0]['reasons'])


def test_alias_is_priority_and_short_ascii_alias_has_word_boundaries():
    docs = [
        document(1, '精密切削工具', kind='product', aliases=['神之手', 'ABS']),
        document(2, '神之手使用笔记', '神之手 ' * 50),
    ]
    assert keys(docs, '我想找神之手')[0] == 'product:1'
    assert keys(docs[:1], 'absolutely unrelated') == []


def test_natural_chinese_and_limited_synonyms_find_relevant_technique():
    docs = [
        document(1, '水口处理', '使用砂纸逐级打磨切口'),
        document(2, '水贴定位', '水转印贴纸浸水后移动'),
    ]
    assert keys(docs, '零件剪下来以后，浇口应该怎么处理？')[0] == 'technique:1'
    assert keys(docs, '水转印贴纸怎么贴')[0] == 'technique:2'
    assert keys([document(1, '白色')], '白') == ['technique:1']


def test_ascii_typo_recovers_one_edit_but_not_short_sku_substitution():
    docs = [
        document(1, 'Airbrush 清洁'),
        document(2, '白色漆', kind='product', sku='XF-2'),
        document(3, 'Vallejo'),
    ]
    hits = retrieval.rank_documents(docs, 'airbrsuh')
    assert hits and hits[0]['key'] == 'technique:1'
    assert any('容错' in reason for reason in hits[0]['reasons'])
    assert keys(docs, 'Vallejp') == ['technique:3']
    assert keys(docs, 'XF-3') == []
    assert keys([document(1, '颜料', kind='product', sku='XF-12345')], 'XF-12354') == ['product:1']


@pytest.mark.parametrize(
    'query',
    ['', '   ', '？？？', '你好，请帮我推荐一下模型制作的方法', '量子纠缠实验', 'the and how'],
)
def test_empty_generic_or_unrelated_queries_do_not_invent_hits(query):
    docs = [
        document(1, '模型制作入门', '这是模型制作的方法，请参考资料并注意安全。'),
        document(2, '打磨方法', '用砂纸处理表面'),
    ]
    assert keys(docs, query) == []


def test_rank_is_deterministic_and_does_not_mutate_corpus():
    docs = [document(3, '打磨'), document(1, '打磨'), document(2, '打磨')]
    before = json.dumps(docs, ensure_ascii=False)
    assert keys(docs, '打磨', k=2) == ['technique:1', 'technique:2']
    assert keys(list(reversed(docs)), '打磨', k=2) == ['technique:1', 'technique:2']
    assert json.dumps(docs, ensure_ascii=False) == before
    with pytest.raises(ValueError):
        retrieval.rank_documents(docs, '打磨', strategy='unknown')


def test_frozen_baseline_keeps_v2_bigram_and_substring_scoring():
    docs = [document(1, '水口处理', '水口打磨'), document(2, '处理', '水口')]
    hits = retrieval.rank_documents(docs, '水口处理', strategy='baseline')
    assert [(hit['id'], hit['score']) for hit in hits] == [(1, 40), (2, 34)]
    assert keys([document(1, 'XF-20')], 'xf-2', strategy='baseline') == ['technique:1']


def test_live_permission_filter_source_redaction_and_withdrawal():
    engine = create_engine(test_database_url(''))
    with Session(engine) as db:
        source = Source(title='本地证据')
        foreign = Source(title='私密证据', owner_id='other')
        withdrawn = Source(title='撤回证据', status='withdrawn')
        good = Technique(title='打磨', sources=[source, foreign, withdrawn])
        product = Product(
            name='工具',
            sku='AB-12',
            aliases=[ProductAlias(alias='砂磨棒', alias_key='砂磨棒')],
            sources=[source],
        )
        db.add_all(
            [
                good,
                product,
                Technique(title='打磨', owner_id='other', sources=[source]),
                Technique(title='打磨', status='draft', sources=[source]),
                Technique(title='打磨', sources=[foreign]),
                Technique(title='打磨', sources=[withdrawn]),
                Technique(title='打磨'),
            ]
        )
        from app.services.ingestion import IngestionService

        IngestionService(db).backfill()
        db.commit()
        docs = retrieval.load_documents(db)
        assert {doc['key'] for doc in docs} == {f'technique:{good.id}', f'product:{product.id}'}
        assert all(doc['source_ids'] == [source.id] for doc in docs)
        assert all(doc['sources'][0]['title'] == '本地证据' for doc in docs)
        assert keys(docs, 'ab12')[0] == f'product:{product.id}'
        source.status = 'withdrawn'
        from app.services.ingestion import IngestionService

        IngestionService(db).backfill()
        db.commit()
        assert retrieval.load_documents(db) == []


def test_chat_uses_shared_ranker_readable_evidence_and_existing_budget():
    engine = create_engine(test_database_url(''))
    with Session(engine) as db:
        source = Source(title='证据')
        db.add_all(
            [
                Technique(
                    title=f'喷笔清洁 {i}',
                    summary='清洁 ' * 2500,
                    content_blocks=[{'type': 'paragraph', 'text': '操作：拆卸\n确认：否\n数量：0'}],
                    sources=[source],
                )
                for i in range(9)
            ]
        )
        from app.services.ingestion import IngestionService

        IngestionService(db).backfill()
        db.commit()
        citations = evidence(db, ('我想知道更多。' * 2000) + 'airbrush')
        assert 1 <= len(citations) <= 6
        assert sum(len(json.dumps(c, ensure_ascii=False)) for c in citations) <= 24000
        assert all(
            set(c) == {'type', 'id', 'title', 'source_ids', 'sources', 'excerpt', 'attachment_ids'}
            for c in citations
        )
        assert all(c['attachment_ids'] == [] for c in citations)
        doc = retrieval.load_documents(db)[0]
        assert '确认：否' in doc['text'] and '数量：0' in doc['text']
        assert not doc['text'].startswith('{')
