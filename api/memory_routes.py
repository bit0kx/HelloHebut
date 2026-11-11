"""Owner-bound preference management; clients never choose an owner or provenance."""
import logging
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException

from memory.fact_store import MemoryConflict, MemoryNotFound
from memory.preferences import LABELS, PreferenceAction, PreferenceKey, PreferenceSave

logger = logging.getLogger(__name__)


def build_memory_router(get_memory):
    router = APIRouter(prefix='/memories', tags=['我的偏好'])

    async def identity(authorization: str | None = Header(default=None)):
        memory = get_memory()
        token = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
        user = await memory.turns.session_user(token) if memory else None
        if not user:
            raise HTTPException(401, '请先获取有效会话令牌')
        if not getattr(memory, 'verified_memory_mode', False) or not getattr(memory, 'facts', None):
            raise HTTPException(503, '可验证偏好服务未启用')
        return user, memory.facts

    async def run(identity, operation=None, *, history=False):
        user, store = identity
        try:
            if operation:
                await store.mutate(user, **operation)
            return {**await store.snapshot(user, history=history), 'labels': LABELS}
        except MemoryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except MemoryNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            logger.warning('偏好服务不可用: %s', type(exc).__name__)
            raise HTTPException(503, '偏好服务暂不可用，请稍后刷新') from exc

    @router.get('')
    async def listing(history: bool = False, owner=Depends(identity)):
        return await run(owner, history=history)

    @router.post('')
    async def save(body: PreferenceSave, owner=Depends(identity)):
        return await run(owner, {'action': 'save', **body.model_dump()})

    @router.post('/{memory_id}/confirm')
    async def confirm(memory_id: UUID, body: PreferenceAction, owner=Depends(identity)):
        return await run(owner, {'action': 'confirm', 'memory_id': str(memory_id), **body.model_dump()})

    @router.post('/{memory_id}/reject')
    async def reject(memory_id: UUID, body: PreferenceAction, owner=Depends(identity)):
        return await run(owner, {'action': 'reject', 'memory_id': str(memory_id), **body.model_dump()})

    @router.delete('')
    async def clear(body: PreferenceAction, owner=Depends(identity)):
        return await run(owner, {'action': 'clear', **body.model_dump()})

    @router.delete('/{key}')
    async def delete(key: PreferenceKey, body: PreferenceAction, owner=Depends(identity)):
        return await run(owner, {'action': 'delete', 'key': key, **body.model_dump()})

    return router
