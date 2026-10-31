"""原生rerank薄适配例外：真实协议边界，不声称官方SDK委托。"""
import asyncio,json
import httpx
from eic.ai.rerank import rerank_hits
from eic.models import AIModel
from eic.vector.qdrant_reader import VectorHit

def test_dashscope_native_protocol_retains_source_identity():
    async def run():
        def handler(request):
            body=json.loads(request.content)
            assert body['input']=={'query':'q','documents':['a','b']}
            assert body['parameters']['top_n']==1
            return httpx.Response(200,json={'output':{'results':[{'index':1,'relevance_score':.9}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            m=AIModel(id=1,tenant_id=1,model_type='rerank',provider='dashscope',model_name='gte-rerank-v2',endpoint='http://fixture.invalid/text-rerank',enabled=True)
            hits=await rerank_hits(m,'q',[VectorHit('a',0,{'content':'a'}),VectorHit('b',0,{'content':'b'})],limit=1,client=client)
            assert hits[0].point_id=='b' and not client.is_closed
    asyncio.run(run())
