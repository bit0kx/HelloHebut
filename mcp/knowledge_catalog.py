"""Atomic Redis publication pointer; Chroma revisions are immutable and retained."""
from copy import deepcopy
import hashlib
import json
import redis


class PublicationConflict(RuntimeError):
    pass


class KnowledgeCatalog:
    def __init__(self, url, collection):
        self.client = redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        namespace = hashlib.sha256(collection.encode()).hexdigest()
        self.key = f'v2:knowledge:{{{namespace}}}:active'
        self.history_key = f'v2:knowledge:{{{namespace}}}:history'

    def read(self):
        raw = self.client.get(self.key)
        return json.loads(raw) if raw else {'generation': 0, 'documents': {}}

    def commit(self, previous, documents):
        current = {'generation': previous['generation'] + 1, 'documents': documents}
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(self.key)
                raw = pipe.get(self.key)
                observed = json.loads(raw) if raw else {'generation': 0, 'documents': {}}
                if observed != previous:
                    raise PublicationConflict('资料版本已被其他请求更新，请重新提交')
                pipe.multi()
                if raw:
                    pipe.lpush(self.history_key, raw)
                    pipe.ltrim(self.history_key, 0, 4)
                pipe.set(self.key, json.dumps(current, ensure_ascii=False, separators=(',', ':')))
                pipe.execute()
            except redis.WatchError as exc:
                raise PublicationConflict('资料发布冲突，请重新提交') from exc
        return current

    def history(self):
        return [json.loads(raw) for raw in self.client.lrange(self.history_key, 0, -1)]

    def rollback(self, generation, validate):
        previous = self.read()
        target = next((item for item in self.history() if item['generation'] == generation), None)
        if target is None:
            raise ValueError('目标版本不在最近五次发布历史内')
        validate(target)
        return self.commit(previous, deepcopy(target['documents']))

    def close(self):
        self.client.close()
