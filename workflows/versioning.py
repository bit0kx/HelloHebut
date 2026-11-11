"""Content-based workflow compatibility, independent of paths and modification times."""
import hashlib
import json
from pathlib import Path


SOURCE_DIRS = ('core', 'agents', 'rag', 'tooling', 'memory', 'workflows')


def workflow_fingerprint(root=None, *, skills_root=None, admission_path=None, settings=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    skills = Path(skills_root) if skills_root is not None else root/'skills'
    csv = Path(admission_path) if admission_path is not None else root/'data/hebut_admission.csv'
    entries = []

    def include(label, path):
        # Absence is explicit, so removing a dependency invalidates saved state.
        content = path.read_bytes() if path.is_file() else None
        entries.append((label, hashlib.sha256(content).hexdigest() if content is not None else 'missing'))

    for folder in SOURCE_DIRS:
        for path in sorted((root/folder).rglob('*.py')):
            include(path.relative_to(root).as_posix(), path)
    include('requirements.txt', root/'requirements.txt')
    include('data/admission.csv', csv)
    if skills.is_dir():
        for path in sorted(skills.rglob('*')):
            if not path.is_file():
                continue
            relative = path.relative_to(skills)
            if (path.name == 'SKILL.md' or 'references' in relative.parts
                    or (len(relative.parts) == 1 and path.suffix.lower() in {'.md', '.txt', '.json'}
                        and path.stem.lower() != 'readme')):
                include('skills/' + relative.as_posix(), path)
    payload = {'schema': 2, 'files': sorted(entries), 'runtime': settings or {}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()
    return 'chat-v2-' + hashlib.sha256(raw).hexdigest()


def service_fingerprint(service):
    orchestrator = service._orchestrator
    skills = getattr(orchestrator, '_skill_manager', None)
    manager = service._tool_manager
    tool = getattr(manager, '_tools', {}).get(service._admission_tool) if manager else None
    admission = getattr(getattr(tool, 'handler', None), '__self__', None)
    agents = [agent for pool in getattr(orchestrator, '_pool', {}).values() for agent in pool]
    settings = {
        'role_mode': getattr(orchestrator, 'role_mode', 'single'),
        'tool_loop': getattr(orchestrator, 'tool_loop', False),
        'agents': sorted(({'model': agent._model, 'system': agent.system_prompt,
                           'max_input_tokens': agent._max_input_tokens} for agent in agents),
                         key=lambda item: (item['model'], item['system'])),
        'loaded_skills': sorted((skill.to_summary() for skill in getattr(skills, '_skills', [])),
                                key=lambda item: item['name']),
        'skills_budget': getattr(skills, 'max_prompt_chars', None),
        'loaded_csv_version': getattr(admission, '_version', None),
        'recognizer_model': getattr(service._recognizer, '_model', None),
        'tool_model': getattr(manager, '_model', None),
        'rag': {name: getattr(service._rag, name, None) for name in ('max_rounds', 'max_queries', 'final_k')},
    }
    return workflow_fingerprint(skills_root=getattr(skills, 'root_dir', None),
                                admission_path=getattr(admission, 'path', None), settings=settings)
