"""Render trusted CSV results without asking a language model to reproduce numbers."""
import re


def numeric_only(message: str) -> bool:
    text = message.strip().rstrip('？?。！!')
    if re.search(r'[？?]|还有|另外|并且|以及|同时|然后|而且', text):
        return False
    numerical = re.search(r'录取风险|最低分|最低位次|分数线|冲稳保|稳吗|能上|能报|录取概率', message)
    other_task = re.search(r'怎么选|适合|就业|课程|介绍|区别|学费|费用|住宿|体检|选科|招生计划|宿舍|校园|校区|培养|优势|职业|发展|转专业|保研|学制|地址|政策|要求|联系|招生办', message)
    return bool(numerical and not other_task)


def render_admission(data: dict) -> str:
    if data.get('status') != 'ok' or not data.get('sources'):
        raise ValueError('admission_not_grounded')
    resolved = data.get('resolved') or {}
    lines = [f"已收录的历史录取数据（{resolved.get('province', '')}，{resolved.get('subject_type', '')}）："]
    rows_count = 0
    for analysis in data.get('analyses', []):
        lines.append(str(analysis['major']))
        for row in analysis.get('records', []):
            if not row.get('available'):
                continue
            lines.append(f"{row['year']}年：最低分 {row['min_score']}，最低位次 {row['min_rank']}。")
            rows_count += 1
        assessment = analysis.get('assessment') or {}
        if assessment.get('risk_level'):
            lines.append('历史比较结果：' + assessment['risk_level'] + '。')
            if assessment.get('basis'):
                lines.append(str(assessment['basis']))
    for candidate in data.get('candidates', []):
        lines.append(f"{candidate['major']}：历史中位分 {candidate['historical_median_score']}，"
                     f"历史中位位次 {candidate['historical_median_rank']}；历史比较结果：{candidate['risk_level']}。")
        rows_count += 1
    if not rows_count:
        raise ValueError('admission_has_no_complete_records')
    lines.append(' '.join(f'[来源{n}]' for n in range(1, len(data['sources']) + 1)))
    lines.extend(str(value) for value in data.get('caveats', []))
    lines.append('以上为历史数据比较，不是未来录取概率或录取承诺。')
    return '\n\n'.join(lines)
