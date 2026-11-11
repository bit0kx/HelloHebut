"""Conservative, source-exact preference candidates; never infer exam conditions."""
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

PreferenceKey = Literal['major_preference', 'interests', 'budget_preference', 'location_preference']
LABELS = {'major_preference': '专业偏好', 'interests': '兴趣方向',
          'budget_preference': '费用偏好', 'location_preference': '地域偏好'}


class PreferenceValue(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: PreferenceKey
    value: str = Field(min_length=1, max_length=120)

    @field_validator('value')
    @classmethod
    def clean_value(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('偏好不能为空或包含控制字符')
        # Conditions/credentials are outside this durable store, even under a misleading key.
        if value.isdigit() or re.search(r'位次|分数|排名|准考证|身份证|手机号|考生号|\d{7,}|\d+\s*分(?:\b|[，。])', value):
            raise ValueError('长期偏好不保存分数、位次或身份凭证')
        return value


class PreferenceSave(PreferenceValue):
    expected_revision: int = Field(ge=0)
    operation_id: str = Field(min_length=1, max_length=100)
    valid_days: int = Field(default=180, ge=1, le=365)


class PreferenceAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    operation_id: str = Field(min_length=1, max_length=100)


def other_candidate(message: str) -> bool:
    return bool(re.search(r'另一个(?:人|考生)|帮(?:我(?:的)?)?(?:朋友|同学|别人)|换个考生|(?:朋友|同学|他|她)的(?:偏好|兴趣|专业)', message))


def asks_preferences(message: str) -> bool:
    return bool(re.fullmatch(r'(?:你?还记得)?我(?:的)?(?:已确认的?)?(?:专业偏好|兴趣方向|费用偏好|地域偏好|偏好)(?:是什么|有哪些|吗)?[？?。!！]?', message.strip())
                or message.strip('？?。!！ ') == '我有哪些已确认偏好')


def extract_candidates(message: str) -> list[dict]:
    """Only explicit first-person declarations; ambiguous text is skipped, not guessed.

    All outputs remain pending until the authenticated user confirms them.
    """
    if (other_candidate(message) or len(message) > 1000
            or re.search(r'不要记|别记|忘记|删除|假设|如果|例如|比如|他说|她说|[“”"`<>]', message)):
        return []
    patterns = {
        'major_preference': r'(?:请)?(?:记住[：:，,]?\s*)?我(?:更|现在更)?(?:喜欢|偏好|倾向)(?:的专业是|的专业为)?\s*[：:]?\s*(.+)',
        'interests': r'我(?:的)?(?:兴趣(?:方向)?是|对)(.+?)(?:感兴趣)?',
        'budget_preference': r'我(?:的)?(?:费用偏好是|希望学费|倾向学费)(.+)',
        'location_preference': r'我(?:的)?(?:地域偏好是|希望在|倾向在)(.+)',
    }
    found = {}
    for sentence in re.split(r'[。！？!?；;\n]', message):
        quote = sentence.strip()
        for key, pattern in patterns.items():
            match = re.fullmatch(pattern, quote)
            if not match:
                continue
            value = match.group(1).strip(' ，,：:')
            # Do not convert negated preferences into positive ones.
            if re.search(r'不喜欢|不想|不考虑|不要|而不是|但是|还是|或者|能否|吗|如何|怎么', value):
                continue
            try:
                candidate = PreferenceValue(key=key, value=value)
            except ValueError:
                continue
            found[key] = {**candidate.model_dump(), 'source_excerpt': quote}
            break
    return list(found.values())[:4]
