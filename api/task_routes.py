"""Durable chat/evaluation tasks and owner-bound SSE with resumable sequence numbers."""
import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.runtime import RequestConflict
from memory.job_store import QueueFull


class ChatTaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=10000)
    client_request_id: str = Field(min_length=1, max_length=100)
    conv_id: str | None = Field(default=None, max_length=100)


def build_task_router(get_components, require_admin, eval_model):
    router = APIRouter(tags=['持久任务'])

    async def identity(authorization: str | None = Header(default=None)):
        memory, store = get_components()
        token = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
        user = await memory.turns.session_user(token) if memory else None
        if not user:
            raise HTTPException(401, '请先获取有效会话令牌')
        if not store:
            raise HTTPException(503, '持久任务尚未就绪')
        return user, store

    async def submit(store, owner, kind, key, payload):
        try:
            return await store.enqueue(owner, kind, key, payload)
        except RequestConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except QueueFull as exc:
            raise HTTPException(429, str(exc), headers={'Retry-After': '3'}) from exc

    @router.post('/chat/tasks', status_code=202)
    async def chat_task(body: ChatTaskInput, owner=Depends(identity)):
        user, store = owner
        conv = body.conv_id or str(uuid.uuid5(uuid.NAMESPACE_URL, user + ':' + body.client_request_id))
        payload = {**body.model_dump(), 'conv_id': conv, 'user_id': user}
        result = await submit(store, user, 'chat', conv + ':' + body.client_request_id, payload)
        return {**result, 'conv_id': conv}

    async def owned(store, user, run_id, kind):
        row = await store.get(user, run_id)
        if not row or row['kind'] != kind:
            raise HTTPException(404, '任务不存在')
        return row

    @router.get('/chat/tasks/{run_id}')
    async def chat_status(run_id: str, owner=Depends(identity)):
        return await owned(owner[1], owner[0], run_id, 'chat')

    @router.get('/chat/tasks/{run_id}/events')
    async def chat_events(run_id: str, request: Request, after_sequence: int = 0,
                          last_event_id: str | None = Header(default=None), owner=Depends(identity)):
        user, store = owner
        await owned(store, user, run_id, 'chat')
        try:
            after = max(0, after_sequence, int(last_event_id or 0))
        except ValueError as exc:
            raise HTTPException(400, '无效的事件序号') from exc

        async def stream():
            nonlocal after
            for tick in range(600):
                if await request.is_disconnected():
                    return
                # Revalidate the bearer session on each poll, including long-lived streams.
                auth = request.headers.get('authorization', '')
                memory, _ = get_components()
                if await memory.turns.session_user(auth[7:]) != user:
                    return
                row = await owned(store, user, run_id, 'chat')
                for event in row['events']:
                    if event['sequence'] > after:
                        after = event['sequence']
                        yield f'id: {after}\nevent: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                if row['status'] in {'completed','failed'}:
                    # The only answer-bearing event is sent AFTER the chat transaction commits.
                    event = {'run_id': run_id, 'sequence': row['sequence'] + 1, 'type': 'final',
                             'status': row['status'], 'result': row['result'], 'error_type': row['error_type']}
                    if event['sequence'] > after:
                        yield f"id: {event['sequence']}\nevent: final\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    return
                if tick % 20 == 0:
                    yield ': keepalive\n\n'
                await asyncio.sleep(.5)
        return StreamingResponse(stream(), media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @router.get('/conversations/{conv_id}/state')
    async def conversation_state(conv_id: str, owner=Depends(identity)):
        memory, _ = get_components()
        return (await memory.turns.state(owner[0], conv_id)).model_dump()

    @router.post('/eval/jobs', status_code=202, dependencies=[Depends(require_admin)])
    async def eval_job(body: dict, x_request_id: str = Header(default='')):
        _, store = get_components()
        if not store:
            raise HTTPException(503, '持久任务尚未就绪')
        try:
            parsed = eval_model.model_validate(body)
        except ValueError as exc:
            raise HTTPException(422, '评测用例格式不符合Schema') from exc
        payload = parsed.model_dump(exclude_none=True)
        if sum(len(payload.get(key, [])) for key in ('intent_cases','dialog_cases')) > 300:
            raise HTTPException(422, '单次评测最多300个场景')
        return await submit(store, 'admin', 'evaluation', x_request_id or uuid.uuid4().hex, payload)

    @router.get('/eval/jobs/{run_id}', dependencies=[Depends(require_admin)])
    async def eval_status(run_id: str):
        _, store = get_components()
        if not store:
            raise HTTPException(503, '持久任务尚未就绪')
        return await owned(store, 'admin', run_id, 'evaluation')

    return router
