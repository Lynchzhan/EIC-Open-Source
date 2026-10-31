"""官方 Embeddings 委托；借入 transport 不借入身份与关闭权。"""
from urllib.parse import urlsplit, urlunsplit, unquote
import asyncio
import httpx
import openai
import re
from langchain_openai import AzureOpenAIEmbeddings, OpenAIEmbeddings
from eic.ai.providers import ModelProviderError
from eic.ai.stream_reader import complete_cleanup
from eic.core.observability import usage_fields

EMBEDDING_BATCH_LIMIT_MESSAGE = '向量化请求条数超过上游批量限制，请调低 EIC_EMBEDDING_PROVIDER_MAX_BATCH_SIZE 后受控重试。'


def _is_batch_limit_response(response):
    """仅在内存中匹配明确的批量限制提示，不保存或返回供应商正文。"""
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        return False
    if not isinstance(body, dict):
        return False
    error = body.get('error')
    message = error.get('message') if isinstance(error, dict) else error
    if not isinstance(message, str):
        message = body.get('message')
    if not isinstance(message, str):
        return False
    return bool(re.search(
        r'\bbatch(?:[\s_-]+size)?\b[^.\n]{0,160}\b'
        r'(?:limit|maximum|max|exceed\w*|larger|greater|less|between|range|at most|(?:must|should) be in)\b'
        r'[^.\n]{0,80}\b[1-9][0-9]*\b', message[:4096], re.IGNORECASE,
    ))


def safe_embedding_error(exc):
    """只保存白名单分类和HTTP数字，不复制响应正文、URL或请求内容。"""
    # SDK可能将transport异常包装成APIConnectionError，沿有限因果链恢复安全分类。
    cause = exc
    for _ in range(5):
        if isinstance(cause, ModelProviderError) and (
            re.fullmatch(r'embedding_http_[1-5][0-9]{2}', cause.reason)
            or cause.reason in {'embedding_response_invalid', 'embedding_batch_limit'}
        ):
            message = EMBEDDING_BATCH_LIMIT_MESSAGE if cause.reason == 'embedding_batch_limit' else 'Embedding provider request failed'
            return ModelProviderError(message, reason=cause.reason, retryable=False)
        if cause.__cause__ is None:
            break
        cause = cause.__cause__
    status = getattr(exc, 'status_code', None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if type(status) is int and 100 <= status <= 599:
        reason = 'embedding_http_' + str(status)
        if status == 400 and isinstance(exc, httpx.HTTPStatusError) and _is_batch_limit_response(exc.response):
            reason = 'embedding_batch_limit'
    elif isinstance(exc, (httpx.TimeoutException, openai.APITimeoutError)):
        reason = 'embedding_timeout'
    elif isinstance(exc, (httpx.TransportError, openai.APIConnectionError)):
        reason = 'embedding_transport_error'
    elif isinstance(exc, (ValueError, KeyError, TypeError)):
        reason = 'embedding_response_invalid'
    else:
        reason = 'embedding_sdk_error'
    message = EMBEDDING_BATCH_LIMIT_MESSAGE if reason == 'embedding_batch_limit' else 'Embedding provider request failed'
    return ModelProviderError(message, reason=reason, retryable=False)

def configuration(model):
    provider=(model.provider or '').strip().lower()
    if provider not in {'openai','openai_compatible','1','azure','2','zhipu','3','dashscope','12','ollama','10'}:
        raise ModelProviderError('Unsupported embedding provider',reason='unsupported_provider',retryable=False)
    parts=urlsplit(model.endpoint.strip())
    if parts.scheme not in {'http','https'} or not parts.netloc or parts.username or parts.password or parts.fragment:
        raise ModelProviderError('Invalid embedding endpoint',reason='invalid_provider_configuration',retryable=False)
    path=parts.path.rstrip('/');query=list(httpx.QueryParams(parts.query).multi_items())
    kind='ollama' if provider in {'ollama','10'} else 'openai';deployment=version=azure_base=None
    if provider in {'azure','2'} and ('/openai' in path or any(k=='api-version' for k,v in query)):
        marker='/openai/deployments/'
        if marker not in path or not path.endswith('/embeddings'):
            raise ModelProviderError('Azure deployment/embeddings required',reason='invalid_provider_configuration',retryable=False)
        prefix,deployment=path.split(marker,1);deployment=deployment.removesuffix('/embeddings')
        versions=[v for k,v in query if k=='api-version']
        if not deployment or '/' in unquote(deployment) or len(versions)!=1 or not versions[0]:
            raise ModelProviderError('Azure unique api-version/deployment required',reason='invalid_provider_configuration',retryable=False)
        kind='azure';version=versions[0];azure_base=urlunsplit((parts.scheme,parts.netloc,prefix,'',''))
    suffix='/api/embed' if kind=='ollama' else '/embeddings'
    target=urlunsplit((parts.scheme,parts.netloc,path if path.endswith(suffix) else path+suffix,parts.query,''))
    base=urlunsplit((parts.scheme,parts.netloc,path[:-len(suffix)] if path.endswith(suffix) else path,'',''))
    return kind,target,base,deployment,version,azure_base

class BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self,client,usage,count,secret,kind,target):
        self.client,self.usage,self.count,self.secret,self.kind,self.target=client,usage,count,secret,kind,target
    async def handle_async_request(self,request):
        if self.client.is_closed:raise ValueError('borrowed client closed')
        headers={k:v for k,v in request.headers.items() if k.lower() not in {'authorization','api-key','cookie','openai-organization','openai-project'}}
        if self.secret:headers['api-key' if self.kind=='azure' else 'authorization']=self.secret if self.kind=='azure' else f'Bearer {self.secret}'
        isolated=httpx.Request(request.method,self.target,headers=headers,content=await request.aread(),extensions=request.extensions)
        self.usage['_invoked']=True
        response=await self.client._transport.handle_async_request(isolated)
        response.request=isolated
        try:
            await response.aread()
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise safe_embedding_error(exc) from None
            try:
                body=response.json()
                if not isinstance(body,dict):raise ValueError('invalid embedding response')
                self.usage.update(usage_fields(body,ollama=self.kind=='ollama'))
                if self.kind!='ollama':
                    if not isinstance(body.get('data'),list):raise ValueError('invalid embedding data')
                    indexed={}
                    for item in body['data']:
                        if not isinstance(item,dict):raise ValueError('invalid embedding item')
                        index=item['index']
                        if type(index) is not int or not 0<=index<self.count or index in indexed:raise ValueError('invalid embedding indices')
                        indexed[index]=item
                    if len(indexed)!=self.count:raise ValueError('incomplete embedding indices')
                    body['data']=[indexed[i] for i in range(self.count)]
            except (ValueError,KeyError,TypeError) as exc:
                # 在 SDK 包装成连接错误之前保留响应无效分类。
                raise safe_embedding_error(exc) from None
            return httpx.Response(response.status_code,json=body,request=request)
        finally:await response.aclose()

async def _cleanup(async_clients,sync_clients):
    failed=cancelled=False
    for client in async_clients:
        try:await client.aclose()
        except asyncio.CancelledError:cancelled=True
        except Exception:failed=True
    for client in sync_clients:
        try:client.close()
        except asyncio.CancelledError:cancelled=True
        except Exception:failed=True
    if cancelled:raise asyncio.CancelledError
    if failed:raise ModelProviderError('Embedding client cleanup failed',reason='provider_cleanup_failed',retryable=False) from None

async def official_embeddings(model,texts,client,secret,usage,*,timeout=60):
    kind,target,base,deployment,version,azure_base=configuration(model)
    async_clients=[];sync_clients=[];cancelled=False
    try:
        facade=httpx.AsyncClient(transport=BorrowedTransport(client,usage,len(texts),secret,kind,target),trust_env=False,follow_redirects=False,headers={},cookies={});async_clients.append(facade)
        sync=httpx.Client(trust_env=False,follow_redirects=False,headers={},cookies={});sync_clients.append(sync)
        if kind=='ollama':
            from langchain_ollama import OllamaEmbeddings
            from ollama import AsyncClient,Client
            adapter=OllamaEmbeddings.model_construct(model=model.model_name,base_url=base)
            adapter._async_client=AsyncClient(host=base,transport=BorrowedTransport(client,usage,len(texts),secret,kind,target),timeout=timeout,trust_env=False,follow_redirects=False);async_clients.append(adapter._async_client._client)
            adapter._client=Client(host=base,trust_env=False,follow_redirects=False);sync_clients.append(adapter._client._client)
        else:
            kwargs=dict(model=model.model_name,api_key=secret or 'eic-anonymous-unused',http_async_client=facade,http_client=sync,max_retries=0,request_timeout=timeout,check_embedding_ctx_length=False,chunk_size=max(1,len(texts)),organization='',model_kwargs={'encoding_format':'float'})
            if kind=='azure':adapter=AzureOpenAIEmbeddings(**kwargs,azure_endpoint=azure_base,azure_deployment=deployment,api_version=version,azure_ad_token=None,azure_ad_token_provider=None)
            else:adapter=OpenAIEmbeddings(**kwargs,base_url=base)
        try:
            return await adapter.aembed_documents(list(texts))
        except ModelProviderError:
            raise
        except Exception as exc:
            raise safe_embedding_error(exc) from None
    except asyncio.CancelledError:
        cancelled=True
        raise
    except ModelProviderError:
        raise
    except Exception:
        raise ModelProviderError('Embedding provider initialization failed',reason='embedding_initialization_failed',retryable=False) from None
    finally:
        try:
            await complete_cleanup(_cleanup(async_clients,sync_clients))
        except ModelProviderError as error:
            if cancelled:
                raise BaseExceptionGroup('Embedding cancellation and cleanup failure',[asyncio.CancelledError(),error]) from None
            raise
