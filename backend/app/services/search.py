"""Translate authorized retrieval results into the existing catalog collections."""

import asyncio
from sqlalchemy.orm import Session
from ..models import Technique, Product
from ..repositories.retrieval import revalidate
from .serializers import _technique_out, _product_out


class SearchService:
    def __init__(self, engine, retrieval):
        self.engine, self.retrieval = engine, retrieval

    def _catalog(self, result):
        with Session(self.engine) as db:
            hits, _ = revalidate(db, result.hits, result.evidence_refs)
            output = {
                'techniques': [],
                'products': [],
                'ranking': [],
                'retrieval': result.metadata(),
            }
            for hit in hits:
                model = Technique if hit['type'] == 'technique' else Product
                row = db.get(model, hit['id'])
                if row is None or row.owner_id != 'local' or row.status != 'published':
                    continue
                record = _technique_out(row) if model is Technique else _product_out(row)
                record['source_ids'] = hit['source_ids']
                output['techniques' if model is Technique else 'products'].append(record)
                output['ranking'].append(hit['key'])
            return output

    async def search(self, query):
        result = await self.retrieval.search(query, k=20)
        return await asyncio.to_thread(self._catalog, result)
