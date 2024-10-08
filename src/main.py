import dotenv
import env
import json
import os
import time
import psutil
import sentry_sdk

from fastapi import FastAPI, Response
from api.models import InsertBody, ChunkBody
from reranking.dto.RerankedSearchResult import RerankedSearchResult
from flashrank import RerankRequest
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

dotenv.load_dotenv()

sentry_dns = os.getenv('SENTRY_DNS')
if sentry_dns:
    sentry_sdk.init(
        dsn=sentry_dns,
        traces_sample_rate=os.getenv('SENTRY_TRACES_SAMPLE_RATE', 1.0),
        profiles_sample_rate=os.getenv('SENTRY_PROFILES_SAMPLE_RATE', 1.0)
    )


vector_database = env.get_vector_database()
reranking_model = env.get_reranking_model()


app = FastAPI()


@app.get('/status')
def status():
    return {
        'status': 'ok',
    }


@app.post('/insert')
def insert(body: InsertBody):
    documents = [Document(
        page_content=item.text,
        metadata={'id': item.id, 'entity': item.entity,
                  'payload': item.payload},
    ) for item in body.data]
    ids = vector_database.store.add_documents(
        documents, ids=[item.id for item in body.data])

    return {
        'success': True,
        'ids': ids,
    }


@app.post('/chunk')
def chunk(body: ChunkBody):
    documents = [Document(
        page_content=item.text,
        metadata={'id': item.id},
    ) for item in body.data]

    chunk_size = body.chunk_size if body.chunk_size > 0 else os.getenv(
        'CHUNK_SIZE', 1000)
    chunk_overlap = body.chunk_overlap if body.chunk_overlap > 0 else os.getenv(
        'CHUNK_OVERLAP', 200)
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    splits = text_splitter.split_documents(documents)

    return {
        'success': True,
        'chunks': [{'id': item.metadata['id'], 'text': item.page_content} for item in splits],
    }


@app.delete('/delete/{id}')
def delete(id, response: Response):
    try:
        vector_database.store.delete([id])
    except Exception as e:
        response.status_code = 400
        return {
            'success': False,
            'error': str(e),
        }

    return {
        'success': True,
    }


@app.get('/query')
def query(query: str, k: int = 5, entities: str = None, where=None, min_score=0.05):
    where = json.loads(where) if where is not None else {}

    query_embeddings = vector_database.store.embeddings.embed_query(query)
    print(f"Memory usage before search: {psutil.virtual_memory().percent}%")
    start = time.time()
    results = vector_database.store.similarity_search_by_vector(
        embedding=query_embeddings,
        k=os.getenv('MAX_K', 100),
        **vector_database.get_search_kwargs(entities=entities, filters=where)
    )
    if len(results) == 0:
        return {
            'success': True,
            'results': [],
            'filters': where,
        }

    end = time.time()
    print(f'Search time: {end - start}')

    print(f"Memory usage before reranking: {psutil.virtual_memory().percent}%")
    start = time.time()
    reranked_results = reranking_model.rerank(
        RerankRequest(
            query=query,
            passages=[
                {
                    'id': item.metadata['id'],
                    'text': item.page_content,
                    'meta': item.metadata,
                } for item in results
            ],
        )
    )

    end = time.time()
    print(f'Reranking time: {end - start}')
    print(f"Memory usage after reranking: {psutil.virtual_memory().percent}%")

    if min_score > 0:
        reranked_results = [
            item for item in reranked_results if item['score'] >= min_score]

    if k > 0:
        reranked_results = reranked_results[:k]

    reranked_results = [RerankedSearchResult(
        id=result['id'],
        entity=result['meta']['entity'],
        text=result['text'],
        payload=result['meta']['payload'] if 'payload' in result['meta'] else {},
        score=result['score'],
    ) for result in reranked_results]
    return {
        'success': True,
        'results': reranked_results,
        'filters': where,
    }
