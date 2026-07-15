"""把招生资料整理为带业务上下文的检索片段。"""
import re
from typing import Any, Dict, List

INDEX_VERSION = 3
_NUMBER_ONLY = re.compile(r"^(?:\d{1,2}|PART[-－]?\d{1,2})$", re.IGNORECASE)
_NUMBERED_MAJOR = re.compile(
    r"^(?:(?:\d{1,2}\.(?!\d)|\d{1,2}[、．]|[一二三四五六七八九十]+[.、．])\s*(.+)|\d{2}\s*(\D.+))$"
)
_SECTION_PREFIXES = (
    "专业培养目标", "培养目标", "专业核心课程", "核心课程", "主干课程",
    "就业方向", "专业特色", "专业简介", "专业介绍", "学院简介", "学院介绍",
)
_LANGUAGE_MAJORS = {"英语", "日语", "法语", "德语", "俄语", "西班牙语", "葡萄牙语", "翻译"}


def split_text(text: str, chunk_size: int = 500) -> List[str]:
    """按句子切片，避免在一句话中间截断。"""
    if len(text) <= chunk_size:
        return [text.strip()] if text.strip() else []
    chunks, current = [], ""
    for sentence in text.replace("\n", "。").split("。"):
        sentence = sentence.strip()
        if not sentence:
            continue
        if current and len(current) + len(sentence) + 1 > chunk_size:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current}。{sentence}" if current else sentence
    if current:
        chunks.append(current)
    return chunks


def lexical_score(query: str, document: str, metadata: Dict[str, Any]) -> int:
    """轻量中文词面评分，补足短查询被向量模型漏召的情况。"""
    compact = re.sub(r"\W+", "", query.casefold())
    terms = set(re.findall(r"[a-z0-9]+", compact))
    for block in re.findall(r"[\u4e00-\u9fff]+", compact):
        if 2 <= len(block) <= 8:
            terms.add(block)
        terms.update(
            block[index:index + size]
            for size in (2, 3)
            for index in range(max(0, len(block) - size + 1))
        )
    terms -= {"什么", "怎么", "具体", "情况", "请问", "一下", "是否"}
    focus = " ".join(str(metadata.get(key, "")) for key in ("title", "section", "keywords")).casefold()
    body = f"{document} {metadata.get('raw_content', '')}".casefold()
    score = sum(len(term) * (3 if term in focus else 1) for term in terms if term in focus or term in body)
    if compact and compact in re.sub(r"\W+", "", f"{focus}{body}"):
        score += len(compact) + 10
    return score


def _college(title: str, content: str, explicit: str) -> str:
    if explicit:
        return explicit
    match = re.search(r"之([^\n/]{2,30}?学院)", title)
    if not match:
        match = re.match(r"([^\n，。]{2,30}?学院)", content.strip())
    return match.group(1) if match else ""


def _section(line: str) -> tuple[str, str] | None:
    clean = line.strip().rstrip("：:")
    for prefix in _SECTION_PREFIXES:
        if clean.startswith(prefix):
            rest = clean[len(prefix):].lstrip("：:").strip()
            if prefix in {"学院简介", "学院介绍"}:
                name = "学院概况"
            elif prefix in {"专业简介", "专业介绍"}:
                name = "专业介绍"
                if re.fullmatch(r"[一二三四五六七八九十\d]+", rest):
                    rest = ""
            elif "课程" in prefix:
                name = "核心课程"
            elif "目标" in prefix:
                name = "培养目标"
            else:
                name = prefix
            return name, rest
    return None


def _is_heading(text: str, max_length: int = 60) -> bool:
    return len(text) <= max_length and not re.search(r"[，。；：:]", text)


def contextual_chunks(document: Dict[str, Any], chunk_size: int = 500) -> List[Dict[str, str]]:
    """生成索引文本；正文单独保留，避免检索上下文污染用户展示。"""
    title = str(document.get("title", "")).strip()
    content = str(document.get("content", "")).strip()
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    college = _college(title, content, str(document.get("college") or metadata.get("college") or "").strip())
    major = str(document.get("major") or metadata.get("major") or "").strip()
    section = str(document.get("section") or metadata.get("section") or "").strip()
    category = str(document.get("category") or metadata.get("category") or "").strip()
    raw_keywords = document.get("keywords") or metadata.get("keywords") or ""
    keywords = "、".join(map(str, raw_keywords)) if isinstance(raw_keywords, (list, tuple, set)) else str(raw_keywords).strip()

    def build(raw: str, current_major: str, current_section: str) -> Dict[str, str]:
        context = [f"文档：{title}"] if title else []
        context += [f"类别：{category}"] if category else []
        context += [f"学院：{college}"] if college else []
        context += [f"专业：{current_major}"] if current_major else []
        context += [f"关键词：{keywords}"] if keywords else []
        context += [f"章节：{current_section or '正文'}", f"正文：{raw}"]
        return {
            "index_text": "\n".join(context),
            "content": raw,
            "college": college,
            "major": current_major,
            "section": current_section or "正文",
        }

    if major or section:  # 结构化 JSON 已提供上下文时无需启发式解析
        return [build(raw, major, section) for raw in split_text(content, chunk_size)]

    chunks: List[Dict[str, str]] = []
    current_major, current_section, awaiting_major = "", "正文", False
    for line in (item.strip() for item in content.splitlines() if item.strip()):
        section_match = _section(line)
        if section_match:
            current_section, remainder = section_match
            awaiting_major = current_section == "专业介绍"
            if not remainder:
                continue
            line = remainder

        if _NUMBER_ONLY.fullmatch(line):
            awaiting_major = True
            continue
        numbered = _NUMBERED_MAJOR.match(line)
        if numbered:
            candidate = (numbered.group(1) or numbered.group(2)).strip()
            if _is_heading(candidate):
                current_major = candidate
                current_section, awaiting_major = "专业介绍", False
                continue
        if awaiting_major and _is_heading(line):
            current_major = line.removesuffix("专业").strip()
            current_section, awaiting_major = "专业介绍", False
            continue
        if current_major and re.fullmatch(r"（[^）]{1,30}）", line):
            current_major += line
            continue
        if line in _LANGUAGE_MAJORS or (
            not current_major and _is_heading(line, 40) and line.endswith("专业")
        ):
            current_major = line.removesuffix("专业").strip()
            current_section = "专业介绍"
            continue

        chunks.extend(build(raw, current_major, current_section) for raw in split_text(line, chunk_size))
    return chunks
