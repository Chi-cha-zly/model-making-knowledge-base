"""Single-user private settings. Secrets never enter response models or backups."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from typing import Literal

from .chat import SYSTEM
from .runtime_config import valid_endpoint, loopback_endpoint

PRESETS = [
    {'id':'baseline','name':'默认','text':''},
    {'id':'concise','name':'简洁回答','text':'请简洁回答，优先给出关键步骤和待确认条件。'},
    {'id':'beginner','name':'新手指导','text':'请按新手能理解的方式解释术语，并逐步说明操作。'},
    {'id':'checklist','name':'详细清单','text':'请提供详细工序清单、所需工具和每一步检查点。'},
]

def compose_prompt(text):
    return SYSTEM + ('\n\n用户补充偏好（仅补充表达和教学方式，不能更改以上证据、引用及安全规则）：\n' + text if text else '')

class ProfileInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1,max_length=80)
    base_url: str = Field(min_length=1,max_length=500)
    model: str = Field(min_length=1,max_length=200)
    compare_model: str = Field(default='',max_length=200)
    reasoning_effort: Literal['auto','low','medium','high'] = 'auto'
    max_output_tokens: int = Field(default=2048,ge=64,le=2048)
    api_key: str | None = Field(default=None,max_length=4096)

    @field_validator('base_url')
    @classmethod
    def endpoint(cls,value):
        if not valid_endpoint(value) or value != value.strip():
            raise ValueError('invalid endpoint')
        return value

    @field_validator('name','model')
    @classmethod
    def nonblank(cls,value):
        if not value.strip(): raise ValueError('blank value')
        return value

class PromptInput(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    text: str = Field(max_length=8000)

class PrivateSettings:
    def __init__(self,path,env):
        self.path=Path(path).resolve()
        self.env=dict(env)
        self.data={'profiles':[],'active_profile_id':'env','custom_prompt':''}
        if self.path.exists():
            try:
                data=json.loads(self.path.read_text())
                if not isinstance(data,dict) or set(data)!=set(self.data): raise ValueError()
                if not isinstance(data['profiles'],list) or len(data['profiles'])>20: raise ValueError()
                ids=[]
                for profile in data['profiles']:
                    id=profile['id']
                    if not isinstance(id,str) or id=='env' or id in ids: raise ValueError()
                    from uuid import UUID
                    UUID(id)
                    parsed=ProfileInput.model_validate({k:v for k,v in profile.items() if k!='id'})
                    self.check_key(parsed.base_url,parsed.api_key)
                    ids.append(id)
                if data['active_profile_id'] not in ['env',*ids]: raise ValueError()
                PromptInput.model_validate({'text':data['custom_prompt']})
                self.data=data
                os.chmod(self.path,0o600)
            except Exception:
                raise ValueError('Invalid private settings file; preserve it and repair or move it before restarting.') from None

    @staticmethod
    def check_key(endpoint,key):
        if not key and not loopback_endpoint(endpoint):
            raise HTTPException(422,'非本机服务需要 API key。')

    def profile(self,id,data=None):
        if id=='env': raise HTTPException(409,'环境配置为只读。')
        for p in (data or self.data)['profiles']:
            if p['id']==id: return p
        raise HTTPException(404,'未找到连接配置。')

    def values(self,data=None,id=None):
        data=self.data if data is None else data
        selected=data['active_profile_id'] if id is None else id
        values=dict(self.env)
        if selected!='env':
            p=self.profile(selected,data)
            values.update({'AI_BASE_URL':p['base_url'],'AI_API_KEY':p.get('api_key') or '',
                           'AI_MODEL':p['model'],'AI_COMPARE_MODEL':p['compare_model'],
                           'AI_REASONING_EFFORT':p['reasoning_effort'],
                           'AI_MAX_OUTPUT_TOKENS':str(p['max_output_tokens'])})
        return values

    def view(self):
        env=self.env
        try: limit=max(64,min(2048,int(env.get('AI_MAX_OUTPUT_TOKENS','2048'))))
        except (ValueError,TypeError): limit=2048
        reason=env.get('AI_REASONING_EFFORT') or 'low'
        profiles=[{'id':'env','name':'环境配置 (.env)','base_url':env.get('AI_BASE_URL','') if valid_endpoint(env.get('AI_BASE_URL','')) else '',
                   'model':env.get('AI_MODEL',''),'compare_model':env.get('AI_COMPARE_MODEL',''),
                   'reasoning_effort':reason if reason in ('auto','low','medium','high') else 'auto',
                   'max_output_tokens':limit,'has_api_key':bool(env.get('AI_API_KEY')),'read_only':True}]
        profiles += [{**{k:v for k,v in p.items() if k!='api_key'},'has_api_key':bool(p.get('api_key')),'read_only':False} for p in self.data['profiles']]
        return {'profiles':profiles,'active_profile_id':self.data['active_profile_id'],'custom_prompt':self.data['custom_prompt'],
                'base_prompt':SYSTEM,'prompt_presets':PRESETS,
                'storage_notice':'API key 以明文存储在本机权限 0600 的私有设置文件中，不包含在资料库备份中。连接测试会向保存的服务发送短请求，可能产生费用。'}

    def save(self,data):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        descriptor,temporary=tempfile.mkstemp(prefix='.settings-',dir=self.path.parent)
        try:
            with os.fdopen(descriptor,'w') as f:
                os.fchmod(f.fileno(),0o600)
                json.dump(data,f,ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(temporary,self.path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        self.data=data


def install_settings(app,store,chat,experiments):
    from .llm import LangChainProvider
    router=APIRouter(prefix='/api/v1/settings')

    def idle():
        if chat.has_active() or any(not t.done() for t in chat.tasks.values()) or experiments.has_active():
            raise HTTPException(409,'请等待对话和实验完全结束后再修改设置或测试连接。')

    async def parse(request,model=None):
        origin=request.headers.get('origin')
        # Vite forwards the browser Origin while the backend sees the proxy Host.
        # Only the documented local UI ports are trusted, and only on a local backend.
        dev_origins = {f'http://{host}:{port}' for host in ('localhost','127.0.0.1','[::1]') for port in (5173,5174)}
        same_origin = origin == str(request.base_url).rstrip('/')
        trusted_dev = origin in dev_origins and loopback_endpoint(str(request.base_url))
        if origin and not (same_origin or trusted_dev):
            raise HTTPException(403,'不允许跨来源修改设置。')
        if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
            raise HTTPException(415,'请使用 application/json。')
        try:
            # Bound before parsing: errors must never include request bodies or secret input.
            raw=await request.body()
            if len(raw)>32768: raise ValueError()
            body=json.loads(raw)
            if not isinstance(body,dict): raise ValueError()
            return model.model_validate(body) if model else body
        except (ValueError,ValidationError):
            raise HTTPException(422,'设置字段无效；请检查地址、必填字段和长度限制。') from None

    def apply(data,*,provider_change):
        provider=LangChainProvider(store.values(data)) if provider_change else chat.provider
        store.save(data)
        chat.provider=provider
        experiments.provider=provider
        experiments.settings=store.values()
        chat.system_prompt=experiments.system_prompt=compose_prompt(data['custom_prompt'])

    @router.get('')
    async def get_settings(): return store.view()

    @router.post('/profiles')
    async def create(request:Request):
        payload=await parse(request,ProfileInput)
        async with chat.lock:
            idle()
            data=deepcopy(store.data)
            if len(data['profiles'])>=20: raise HTTPException(409,'最多保存 20 个连接配置。')
            store.check_key(payload.base_url,payload.api_key)
            data['profiles'].append({'id':str(uuid4()),**payload.model_dump()})
            apply(data,provider_change=False)
            return store.view()

    @router.put('/profiles/{id}')
    async def update(id:str,request:Request):
        payload=await parse(request,ProfileInput)
        async with chat.lock:
            idle(); data=deepcopy(store.data); old=store.profile(id,data)
            if payload.base_url!=old['base_url'] and payload.api_key is None:
                raise HTTPException(422,'更改服务地址时必须明确提供新 API key 或清除旧 key。')
            values=payload.model_dump()
            if payload.api_key is None: values['api_key']=old.get('api_key')
            store.check_key(values['base_url'],values['api_key'])
            old.clear(); old.update({'id':id,**values})
            apply(data,provider_change=id==data['active_profile_id'])
            return store.view()

    @router.delete('/profiles/{id}')
    async def delete(id:str,request:Request):
        await parse(request)
        async with chat.lock:
            idle(); data=deepcopy(store.data); profile=store.profile(id,data)
            if id==data['active_profile_id']: raise HTTPException(409,'请先切换到其他连接。')
            data['profiles'].remove(profile); apply(data,provider_change=False)
            return store.view()

    @router.post('/profiles/{id}/activate')
    async def activate(id:str,request:Request):
        await parse(request)
        async with chat.lock:
            idle(); data=deepcopy(store.data)
            if id!='env': store.profile(id,data)
            data['active_profile_id']=id; apply(data,provider_change=True)
            return store.view()

    @router.put('/prompt')
    async def prompt(request:Request):
        payload=await parse(request,PromptInput)
        async with chat.lock:
            idle(); data=deepcopy(store.data); data['custom_prompt']=payload.text
            apply(data,provider_change=False)
            return store.view()

    @router.post('/profiles/{id}/test')
    async def test(id:str,request:Request):
        await parse(request)
        async with chat.lock:
            idle(); store.profile(id)
            values=store.values(id=id); values['AI_MAX_OUTPUT_TOKENS']='256'; values['LANGSMITH_TRACING']='false'
            provider=LangChainProvider(values)
            started=perf_counter()
            try:
                await asyncio.wait_for(provider.complete([{'role':'user','content':'请只回复：连接成功'}],trace=False),timeout=20)
                return {'ok':True,'message':'连接成功。','latency_ms':round((perf_counter()-started)*1000)}
            except Exception:
                return {'ok':False,'message':'连接测试失败；请检查地址、模型、API key 和服务状态。','latency_ms':round((perf_counter()-started)*1000)}

    app.state.user_settings=store
    app.include_router(router)
