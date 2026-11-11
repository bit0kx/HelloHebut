"""Private per-request tools; never expose them through public MCP discovery."""
PRIVATE_TOOLS = {'request_evidence_read', 'skill_reference_read'}
ROLES = ('general', 'policy', 'risk', 'planning')


def register_read_tools(manager):
    from tooling.tool_manager import Tool

    async def evidence(params, context):
        rows = (context or {}).get('evidence', [])
        selected = []
        for index in params['sources']:
            if index > len(rows):
                return {'error': 'unknown_evidence_source'}
            item = rows[index-1]
            selected.append({'evidence_id': f'source:{index}', 'citation': f'[来源{index}]',
                'title': item.get('title', ''),
                'content': item.get('source_excerpt') or item.get('content') or item.get('excerpt') or '',
                'year': item.get('year') or item.get('effective_year'),
                'version': item.get('version') or item.get('revision_id') or item.get('document_version')})
        return {'evidence': selected, 'scope': 'already_registered_current_request'}

    async def reference(params, context):
        skills = (context or {}).get('skill_manager')
        if not skills:
            return {'error': 'skills_unavailable'}
        try:
            return skills.read_reference(params['name'], params['path'],
                message=context.get('skill_message', ''), agent_type=context['agent_type'])
        except (ValueError, OSError):
            return {'error': 'skill_reference_not_allowed'}

    manager.register(Tool(name='request_evidence_read', description='按来源编号读取本轮已注册证据，不检索新资料。',
        handler=evidence, schema={'type': 'object', 'properties': {'sources': {'type': 'array',
            'items': {'type': 'integer', 'minimum': 1, 'maximum': 64}, 'minItems': 1, 'maxItems': 3, 'uniqueItems': True}},
            'required': ['sources'], 'additionalProperties': False}, cache_ttl=0, read_only=True, allowed_agents=ROLES))
    manager.register(Tool(name='skill_reference_read', description='读取当前已激活Skill声明的references文件；不能执行脚本。',
        handler=reference, schema={'type': 'object', 'properties': {'name': {'type': 'string', 'maxLength': 64},
            'path': {'type': 'string', 'maxLength': 160}}, 'required': ['name', 'path'], 'additionalProperties': False},
        cache_ttl=0, read_only=True, allowed_agents=ROLES))


def block_value(block, name, default=None):
    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)


def assistant_exchange_blocks(response):
    """Transport complete protocol blocks in memory; never expose/log reasoning text.

    Anthropic requires unmodified thinking/signature blocks during a tool-use turn.
    They exist only in this local exchange, never in ChatService results or traces.
    """
    blocks, seen = [], set()
    for block in response.content:
        kind = block_value(block, 'type')
        if kind == 'text':
            blocks.append({'type': 'text', 'text': block_value(block, 'text', '')})
        elif kind in {'thinking', 'redacted_thinking'}:
            blocks.append(dict(block) if isinstance(block, dict) else block.model_dump(exclude_none=True))
        elif kind == 'tool_use':
            identifier = block_value(block, 'id', '')
            name, arguments = block_value(block, 'name', ''), block_value(block, 'input')
            if (not isinstance(identifier, str) or not identifier or len(identifier) > 200 or identifier in seen
                    or not isinstance(name, str) or not name or len(name) > 128 or not isinstance(arguments, dict)):
                raise ValueError('invalid_tool_use_block')
            seen.add(identifier)
            blocks.append({'type': kind, 'id': identifier, 'name': name, 'input': arguments})
    return blocks
