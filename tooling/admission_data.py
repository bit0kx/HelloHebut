"""河北工业大学分省专业录取数据的确定性查询与风险分析。"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import statistics
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdmissionRecord:
    province: str
    year: int
    subject_type: str
    major: str
    min_score: Optional[float]
    min_rank: Optional[int]
    batch: str
    source_file: str
    source_url: str


class AdmissionDataStore:
    """从长表 CSV 加载录取数据，提供精确查询、风险分析和候选专业推荐。"""

    REQUIRED_COLUMNS = {
        "province", "year", "subject_type", "major", "min_score", "min_rank",
        "batch", "source_file", "source_url",
    }
    SUPPORTED_PROVINCES = ("河北", "天津")
    KNOWN_PROVINCES = (
        "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
        "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北",
        "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏",
        "陕西", "甘肃", "青海", "宁夏", "新疆",
    )
    MAJOR_ALIASES = {
        "电气": "电气工程及其自动化",
        "电气专业": "电气工程及其自动化",
        "计算机": "计算机科学与技术",
        "计算机专业": "计算机科学与技术",
        "土木": "土木工程",
        "土木专业": "土木工程",
        "会计": "会计学",
        "金融": "金融学",
        "法学专业": "法学",
        "自动化专业": "自动化",
    }

    def __init__(self, csv_path: str):
        self.path = Path(csv_path).expanduser().resolve()
        self._records: List[AdmissionRecord] = []
        self._majors_by_province: Dict[str, List[str]] = {}
        self._encoding = ""
        self._version = ""
        self._load()

    @property
    def stats(self) -> Dict[str, Any]:
        valid = sum(record.min_score is not None and record.min_rank is not None for record in self._records)
        return {
            "source_file": self.path.name,
            "encoding": self._encoding,
            "data_version": self._version,
            "total_rows": len(self._records),
            "valid_rows": valid,
            "incomplete_rows": len(self._records) - valid,
            "provinces": sorted({record.province for record in self._records}),
            "years": sorted({record.year for record in self._records}),
            "major_count": len({record.major for record in self._records}),
        }

    async def query_handler(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        return self.query(params)

    def query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """解析自然语言和实体，返回确定性录取数据结果。"""
        resolved = self._resolve_query(params)
        province = resolved.get("province", "")
        if not province:
            missing_fields = ["province"]
            if resolved.get("candidate_score") is not None or resolved.get("candidate_rank") is not None:
                if not resolved.get("subject_type"):
                    missing_fields.append("subject_type")
                if resolved.get("candidate_rank") is None:
                    missing_fields.append("rank")
                if not resolved.get("majors"):
                    missing_fields.append("major")
            labels = {
                "province": "省份", "subject_type": "科类或选科", "rank": "位次", "major": "目标专业",
            }
            readable = "、".join(labels[field] for field in missing_fields)
            return self._clarification(
                resolved,
                missing_fields,
                f"请补充{readable}；当前结构化数据覆盖河北和天津。",
            )
        if province not in self.SUPPORTED_PROVINCES:
            return {
                "status": "unsupported",
                "message": f"当前结构化录取数据暂不覆盖{province}，仅覆盖河北和天津。",
                "resolved": resolved,
                "supported_provinces": list(self.SUPPORTED_PROVINCES),
                "sources": [],
            }

        majors = list(resolved.get("majors") or [])
        ambiguous = list(resolved.get("ambiguous_majors") or [])
        if ambiguous and not majors:
            return self._clarification(
                resolved,
                ["major"],
                "专业名称可能对应多个专业，请从候选项中确认。",
                suggestions=ambiguous[:10],
            )

        if not majors:
            if resolved.get("candidate_rank") is not None or resolved.get("candidate_score") is not None:
                return self._recommend(resolved)
            return self._clarification(resolved, ["major"], "请提供要查询的目标专业。")

        analyses = []
        subject_missing = False
        for major in majors[:5]:
            analysis = self._analyze_major(resolved, major)
            if analysis.get("status") == "needs_subject":
                subject_missing = True
            elif analysis.get("status") == "ok":
                analyses.append(analysis)

        if not analyses and subject_missing:
            return self._clarification(
                resolved,
                ["subject_type"],
                "河北数据需要区分物理类、历史类或艺术类口径，请补充科类。",
            )
        if not analyses:
            return {
                "status": "not_found",
                "message": "在当前数据覆盖范围内没有找到匹配记录；这不代表该专业没有招生。",
                "resolved": resolved,
                "sources": self._sources(province),
            }

        return {
            "status": "ok",
            "message": "已按省份、专业、科类和年份完成确定性查询。",
            "resolved": resolved,
            "analyses": analyses,
            "sources": self._sources(province),
            "caveats": self._caveats(resolved),
        }

    def _load(self) -> None:
        if not self.path.is_file():
            raise FileNotFoundError(f"录取数据文件不存在: {self.path}")
        raw = self.path.read_bytes()
        self._version = hashlib.sha256(raw).hexdigest()
        text, self._encoding = self._decode(raw)
        reader = csv.DictReader(io.StringIO(text))
        columns = {str(name or "").strip() for name in (reader.fieldnames or [])}
        missing = self.REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"录取数据缺少字段: {', '.join(sorted(missing))}")

        keys = set()
        records = []
        for line_no, row in enumerate(reader, start=2):
            clean = {str(key).strip(): str(value or "").strip() for key, value in row.items()}
            try:
                record = AdmissionRecord(
                    province=clean["province"],
                    year=int(clean["year"]),
                    subject_type=clean["subject_type"],
                    major=clean["major"],
                    min_score=self._optional_float(clean["min_score"]),
                    min_rank=self._optional_int(clean["min_rank"]),
                    batch=clean["batch"],
                    source_file=clean["source_file"],
                    source_url=clean["source_url"],
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"录取数据第 {line_no} 行格式错误: {exc}") from exc
            if not all((record.province, record.subject_type, record.major)):
                raise ValueError(f"录取数据第 {line_no} 行缺少省份、科类或专业")
            if (record.min_score is None) != (record.min_rank is None):
                raise ValueError(f"录取数据第 {line_no} 行最低分和最低位次必须同时填写或同时留空")
            key = (record.province, record.year, record.subject_type, self._normalize(record.major))
            if key in keys:
                raise ValueError(f"录取数据第 {line_no} 行存在重复键: {key}")
            keys.add(key)
            records.append(record)

        if not records:
            raise ValueError("录取数据文件没有有效行")
        self._records = records
        for province in {record.province for record in records}:
            self._majors_by_province[province] = sorted(
                {record.major for record in records if record.province == province},
                key=lambda item: (-len(item), item),
            )
        logger.info("录取数据已加载: %s", self.stats)

    @staticmethod
    def _decode(raw: bytes) -> Tuple[str, str]:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(encoding)
                if text.lstrip().startswith("province,"):
                    return text, encoding
            except UnicodeDecodeError:
                continue
        raise ValueError("录取数据编码无法识别，请使用 UTF-8、UTF-8 BOM 或 GB18030")

    def _resolve_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = str(params.get("query", "")).strip()
        entities = params.get("entities") if isinstance(params.get("entities"), dict) else {}
        history = params.get("history") if isinstance(params.get("history"), list) else []
        user_history = [
            str(item.get("content", ""))
            for item in history
            if isinstance(item, dict) and item.get("role") == "user"
        ]
        texts = [query] + list(reversed(user_history))

        province = self._normalize_province(
            self._first_entity(entities, "province") or self._find_province(texts)
        )
        subject = self._first_entity(entities, "subject_combination", "exam_mode") or self._find_subject(texts)
        if province == "天津":
            subject = "综合改革"
        elif subject:
            subject = self._normalize_subject(subject)

        candidate_score = self._number_entity(entities, "score", as_int=False)
        candidate_rank = self._number_entity(entities, "rank", as_int=True)
        if candidate_score is None:
            candidate_score = self._find_score(texts)
        if candidate_rank is None:
            candidate_rank = self._find_rank(texts)

        raw_majors = self._entity_values(entities, "major")
        majors, ambiguous = self._resolve_majors(province, raw_majors, texts)
        data_years, target_year = self._resolve_years(texts, entities)
        return {
            "province": province,
            "subject_type": subject,
            "majors": majors,
            "ambiguous_majors": ambiguous,
            "candidate_score": candidate_score,
            "candidate_rank": candidate_rank,
            "data_years": data_years,
            "target_year": target_year,
        }

    def _analyze_major(self, resolved: Dict[str, Any], major: str) -> Dict[str, Any]:
        province = resolved["province"]
        rows = [record for record in self._records if record.province == province and record.major == major]
        subjects = sorted({record.subject_type for record in rows})
        subject = resolved.get("subject_type")
        if not subject:
            if len(subjects) != 1:
                return {"status": "needs_subject", "major": major, "available_subjects": subjects}
            subject = subjects[0]
        rows = [record for record in rows if record.subject_type == subject]
        if resolved.get("data_years"):
            rows = [record for record in rows if record.year in resolved["data_years"]]
        rows.sort(key=lambda record: record.year)
        if not rows:
            return {"status": "not_found", "major": major}

        records = [self._record_payload(record, resolved) for record in rows]
        assessment = self._assessment(rows, resolved)
        return {
            "status": "ok",
            "major": major,
            "subject_type": subject,
            "records": records,
            "coverage": {
                "available": sum(item["available"] for item in records),
                "total": len(records),
                "years": [item["year"] for item in records if item["available"]],
            },
            "assessment": assessment,
        }

    def _recommend(self, resolved: Dict[str, Any]) -> Dict[str, Any]:
        province = resolved["province"]
        subject = resolved.get("subject_type")
        if province == "天津":
            subject = "综合改革"
        if not subject:
            return self._clarification(
                resolved,
                ["subject_type", "major"],
                "如果希望推荐候选专业，请补充河北物理类、历史类或艺术类口径；也可以直接给出目标专业。",
            )

        groups: Dict[str, List[AdmissionRecord]] = {}
        for record in self._records:
            if record.province == province and record.subject_type == subject:
                if resolved.get("data_years") and record.year not in resolved["data_years"]:
                    continue
                if record.min_rank is None or record.min_score is None:
                    continue
                groups.setdefault(record.major, []).append(record)

        candidates = []
        for major, rows in groups.items():
            assessment = self._assessment(rows, resolved)
            if not assessment:
                continue
            candidates.append({
                "major": major,
                "subject_type": subject,
                "historical_median_rank": assessment.get("historical_median_rank"),
                "historical_median_score": assessment.get("historical_median_score"),
                "risk_level": assessment.get("risk_level"),
                "basis": assessment.get("basis"),
                "available_years": sorted(record.year for record in rows),
            })

        if not candidates:
            return {
                "status": "not_found",
                "message": "当前省份和科类下没有可用于候选专业分析的完整记录。",
                "resolved": {**resolved, "subject_type": subject},
                "sources": self._sources(province),
            }

        if resolved.get("candidate_rank") is not None:
            value = int(resolved["candidate_rank"])
            candidates.sort(key=lambda item: abs(int(item["historical_median_rank"]) - value))
        else:
            value = float(resolved["candidate_score"])
            candidates.sort(key=lambda item: abs(float(item["historical_median_score"]) - value))
        candidates = candidates[:12]
        return {
            "status": "ok",
            "message": "已按三年历史中位值返回最接近的候选专业；未出现的专业不代表没有招生。",
            "resolved": {**resolved, "subject_type": subject},
            "candidates": candidates,
            "sources": self._sources(province),
            "caveats": self._caveats(resolved) + ["候选专业仅按历史录取数据接近程度生成，未纳入兴趣、体检和选科限制。"],
        }

    def _record_payload(self, record: AdmissionRecord, resolved: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "year": record.year,
            "min_score": self._display_number(record.min_score),
            "min_rank": record.min_rank,
            "batch": record.batch,
            "available": record.min_score is not None and record.min_rank is not None,
        }
        if payload["available"] and resolved.get("candidate_rank") is not None:
            margin = int(record.min_rank or 0) - int(resolved["candidate_rank"])
            payload.update({
                "rank_margin": margin,
                "rank_comparison": "考生位次更靠前或持平" if margin >= 0 else "考生位次更靠后",
            })
        if payload["available"] and resolved.get("candidate_score") is not None:
            margin = float(resolved["candidate_score"]) - float(record.min_score or 0)
            payload["score_margin"] = self._display_number(margin)
        return payload

    def _assessment(self, rows: Sequence[AdmissionRecord], resolved: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        valid = [row for row in rows if row.min_score is not None and row.min_rank is not None]
        if not valid:
            return None
        median_rank = int(statistics.median([int(row.min_rank or 0) for row in valid]))
        median_score = statistics.median([float(row.min_score or 0) for row in valid])
        candidate_rank = resolved.get("candidate_rank")
        if candidate_rank is not None:
            rank = int(candidate_rank)
            better_years = sum(rank <= int(row.min_rank or 0) for row in valid)
            ratio = (median_rank - rank) / median_rank if median_rank else 0.0
            level = self._risk_level(better_years, len(valid), ratio)
            return {
                "risk_level": level,
                "basis": "位次优先",
                "candidate_rank": rank,
                "historical_median_rank": median_rank,
                "historical_median_score": self._display_number(median_score),
                "better_or_equal_years": better_years,
                "available_years": len(valid),
                "median_rank_margin_ratio": round(ratio, 4),
                "method": "相对稳妥需三年均优于最低位次且领先历史中位位次至少8%；相对匹配需至少三分之二年份不差于最低位次；距中位位次5%以内或至少命中一年为可冲，其余为偏冲。",
            }

        candidate_score = resolved.get("candidate_score")
        if candidate_score is not None:
            score = float(candidate_score)
            better_years = sum(score >= float(row.min_score or 0) for row in valid)
            median_margin = score - median_score
            if len(valid) < 2:
                level = "数据不足"
            elif better_years == len(valid) and median_margin >= 10:
                level = "相对稳妥"
            elif better_years >= (2 * len(valid) + 2) // 3:
                level = "相对匹配"
            elif better_years > 0 or median_margin >= -5:
                level = "可冲"
            else:
                level = "偏冲"
            return {
                "risk_level": level,
                "basis": "仅按裸分粗略比较（低置信）",
                "candidate_score": self._display_number(score),
                "historical_median_score": self._display_number(median_score),
                "historical_median_rank": median_rank,
                "better_or_equal_years": better_years,
                "available_years": len(valid),
                "method": "裸分受试卷难度影响，只作低置信参考；应补充位次后重新判断。",
            }
        return None

    @staticmethod
    def _risk_level(better_years: int, total: int, median_margin_ratio: float) -> str:
        if total < 2:
            return "数据不足"
        if better_years == total and median_margin_ratio >= 0.08:
            return "相对稳妥"
        if better_years >= (2 * total + 2) // 3 and median_margin_ratio >= 0:
            return "相对匹配"
        if better_years > 0 or median_margin_ratio >= -0.05:
            return "可冲"
        return "偏冲"

    def _resolve_majors(
        self,
        province: str,
        raw_majors: Sequence[str],
        texts: Sequence[str],
    ) -> Tuple[List[str], List[str]]:
        available = self._majors_by_province.get(province) or sorted(
            {record.major for record in self._records}, key=lambda item: (-len(item), item)
        )
        resolved: List[str] = []
        ambiguous: List[str] = []
        for raw in raw_majors:
            matches = self._match_major(raw, available)
            if len(matches) == 1:
                resolved.extend(matches)
            elif len(matches) > 1:
                ambiguous.extend(matches)

        if not raw_majors:
            current_text = texts[0] if texts else ""
            full_matches = [major for major in available if self._normalize(major) in self._normalize(current_text)]
            if full_matches:
                max_len = max(len(self._normalize(major)) for major in full_matches)
                resolved.extend(major for major in full_matches if len(self._normalize(major)) == max_len)
            else:
                normalized_text = self._normalize(current_text)
                for alias, target in self.MAJOR_ALIASES.items():
                    if self._normalize(alias) in normalized_text and target in available:
                        resolved.append(target)

        if not resolved and not ambiguous:
            for text in texts[1:]:
                matches = [major for major in available if self._normalize(major) in self._normalize(text)]
                if matches:
                    max_len = max(len(self._normalize(major)) for major in matches)
                    resolved.extend(major for major in matches if len(self._normalize(major)) == max_len)
                    break
        return list(dict.fromkeys(resolved)), list(dict.fromkeys(ambiguous))

    def _match_major(self, raw: str, available: Sequence[str]) -> List[str]:
        normalized = self._normalize(raw)
        without_suffix = normalized[:-2] if normalized.endswith("专业") else normalized
        exact = [major for major in available if self._normalize(major) in {normalized, without_suffix}]
        if exact:
            return exact
        alias = self.MAJOR_ALIASES.get(raw.strip()) or self.MAJOR_ALIASES.get(without_suffix)
        if alias and alias in available:
            return [alias]
        if len(without_suffix) >= 2:
            return [major for major in available if without_suffix in self._normalize(major)]
        return []

    def _resolve_years(self, texts: Sequence[str], entities: Dict[str, Any]) -> Tuple[List[int], Optional[int]]:
        current_year = datetime.now().year
        max_data_year = max(record.year for record in self._records)
        candidates = self._entity_values(entities, "admission_year")
        for text in texts:
            if "去年" in text:
                return [current_year - 1], None
            if "前年" in text:
                return [current_year - 2], None
            years = [int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)]
            if years:
                data_years = sorted({year for year in years if year <= max_data_year})
                target_years = [year for year in years if year > max_data_year]
                return data_years, target_years[0] if target_years else None
        for value in candidates:
            match = re.search(r"20\d{2}", value)
            if match:
                year = int(match.group(0))
                return ([year], None) if year <= max_data_year else ([], year)
        return [], current_year

    @classmethod
    def _find_province(cls, texts: Sequence[str]) -> str:
        for text in texts:
            for province in cls.KNOWN_PROVINCES:
                if province in text:
                    return province
        return ""

    @staticmethod
    def _find_subject(texts: Sequence[str]) -> str:
        for text in texts:
            if re.search(r"物理(?:类|组|选科|方向)", text):
                return "物理"
            if re.search(r"历史(?:类|组|选科|方向)", text):
                return "历史"
            if "综合改革" in text or "新高考" in text:
                return "综合改革"
        return ""

    @staticmethod
    def _find_score(texts: Sequence[str]) -> Optional[float]:
        for text in texts:
            match = re.search(r"(?<!\d)(\d{3}(?:\.\d+)?)\s*分(?!钟)", text.replace(",", ""))
            if match:
                return float(match.group(1))
        return None

    @staticmethod
    def _find_rank(texts: Sequence[str]) -> Optional[int]:
        patterns = (
            r"(?:位次|排名|排位)(?:是|为|约|大约)?\s*[:：]?\s*(\d[\d,]*)",
            r"(?<!\d)(\d[\d,]*)\s*(?:名|位)(?!次)",
        )
        for text in texts:
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    return int(match.group(1).replace(",", ""))
        return None

    @staticmethod
    def _normalize_subject(value: str) -> str:
        if "物理" in value:
            return "物理"
        if "历史" in value:
            return "历史"
        if "综合" in value or "新高考" in value:
            return "综合改革"
        if "不限" in value or "艺术" in value:
            return "不限"
        return value.strip()

    @staticmethod
    def _normalize_province(value: str) -> str:
        value = str(value or "").strip()
        aliases = {
            "河北省": "河北", "天津市": "天津", "北京市": "北京", "上海市": "上海",
            "重庆市": "重庆", "内蒙古自治区": "内蒙古", "广西壮族自治区": "广西",
            "宁夏回族自治区": "宁夏", "新疆维吾尔自治区": "新疆", "西藏自治区": "西藏",
        }
        return aliases.get(value, value[:-1] if value.endswith(("省", "市")) else value)

    @classmethod
    def _normalize(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", str(value or "")).lower()
        return re.sub(r"[\s·•,，、()（）\-—_/]", "", value)

    @staticmethod
    def _entity_values(entities: Dict[str, Any], *keys: str) -> List[str]:
        values = []
        for key in keys:
            value = entities.get(key)
            if isinstance(value, list):
                values.extend(str(item).strip() for item in value if str(item).strip())
            elif value not in (None, ""):
                values.append(str(value).strip())
        return values

    @classmethod
    def _first_entity(cls, entities: Dict[str, Any], *keys: str) -> str:
        values = cls._entity_values(entities, *keys)
        return values[0] if values else ""

    @classmethod
    def _number_entity(cls, entities: Dict[str, Any], key: str, *, as_int: bool) -> Optional[Any]:
        for value in cls._entity_values(entities, key):
            match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
            if match:
                number = float(match.group(0))
                return int(number) if as_int else number
        return None

    def _sources(self, province: str = "") -> List[Dict[str, Any]]:
        rows = [record for record in self._records if not province or record.province == province]
        urls = sorted({record.source_url for record in rows if record.source_url})
        years = sorted({record.year for record in rows})
        label = province or "河北、天津"
        return [{
            "title": f"河北工业大学{label}专业录取最低分和最低位次（{min(years)}—{max(years)}）",
            "source_file": self.path.name,
            "source_url": urls[0] if len(urls) == 1 else "",
            "data_version": self._version,
            "effective_year": max(years),
        }]

    @staticmethod
    def _caveats(resolved: Dict[str, Any]) -> List[str]:
        caveats = ["历史最低分和最低位次仅供下一年度报考参考，不构成录取承诺。"]
        if resolved.get("candidate_rank") is None and resolved.get("candidate_score") is not None:
            caveats.append("当前只提供了裸分；不同年份试卷难度不同，应补充位次后重新判断。")
        caveats.append("风险判断未纳入当年招生计划变化、选科限制、体检要求和报考热度。")
        return caveats

    @staticmethod
    def _clarification(
        resolved: Dict[str, Any],
        missing_fields: Iterable[str],
        message: str,
        *,
        suggestions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        result = {
            "status": "needs_clarification",
            "message": message,
            "missing_fields": list(missing_fields),
            "resolved": resolved,
        }
        if suggestions:
            result["suggestions"] = suggestions
        return result

    @staticmethod
    def _optional_float(value: str) -> Optional[float]:
        return float(value) if value else None

    @staticmethod
    def _optional_int(value: str) -> Optional[int]:
        return int(value) if value else None

    @staticmethod
    def _display_number(value: Optional[float]) -> Optional[Any]:
        if value is None:
            return None
        return int(value) if float(value).is_integer() else round(float(value), 2)
