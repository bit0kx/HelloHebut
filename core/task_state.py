"""Explicit admission conditions; historical scores must not cross candidate scopes."""
import re
from core.contracts import TaskState
from memory.preferences import other_candidate

FIELDS = {'province', 'exam_mode', 'subject_combination', 'batch', 'score', 'rank',
          'major', 'college', 'campus', 'candidate_type', 'admission_year'}
SCOPE = {'province', 'exam_mode', 'subject_combination', 'admission_year', 'candidate_type'}


def merge_state(previous: TaskState, entities: dict, message: str, request_id: str) -> TaskState:
    current = {key: [str(v).strip() for v in values if str(v).strip()]
               for key, values in entities.items() if key in FIELDS and isinstance(values, list)}
    current = {key: values for key, values in current.items() if values}
    own = bool(re.search(r'回到我(?:自己)?|问我自己|我本人(?:的情况)?', message))
    other = other_candidate(message)
    reset = other or own or bool(re.search(r'重新开始|清空条件', message))
    state = TaskState(revision=previous.revision + 1) if reset else previous.model_copy(deep=True)
    state.revision = previous.revision + 1
    state.invalidated = list(previous.entities) if reset else []
    if other:
        state.subject_is_self = False
    elif own:
        state.subject_is_self = True
    elif reset:
        state.subject_is_self = previous.subject_is_self
    changed = any(key in current and previous.entities.get(key)
                  and current[key] != previous.entities[key] for key in SCOPE)
    if changed and not reset:
        for key in ('score', 'rank'):
            state.entities.pop(key, None)
            state.sources.pop(key, None)
            state.invalidated.append(key)
        if 'province' in current and current['province'] != previous.entities.get('province'):
            for key in ('subject_combination', 'exam_mode', 'batch'):
                state.entities.pop(key, None)
                state.sources.pop(key, None)
                state.invalidated.append(key)
    for key, values in current.items():
        state.entities[key] = values
        state.sources[key] = request_id
    return state


def needs_admission(message: str, intent: str, entities: dict) -> bool:
    """Numerical subtasks are mandatory even when the primary intent is comparison."""
    if intent == 'score_risk':
        return True
    if re.search(r'位次|排名|最低分|录取分|分数线|冲稳保|录取风险|\d+\s*分', message):
        return True
    follow_up = bool(re.search(r'呢|比较|对比|怎么选|风险|能上|能报|稳吗|换成|换.*考生', message))
    return follow_up and bool(entities.get('rank') or entities.get('score'))
