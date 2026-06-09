import argparse
import html
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from generate_course_recommendations import build_recommendations


DEFAULT_WEIGHTS = {
    "gpa": 5.0,
    "teacher": 1.2,
    "checkin": 1.5,
    "workload": 1.5,
    "relevance": 1.0,
    "confidence": 1.0,
    "risk": 1.3,
}

WEIGHT_LABELS = {
    "gpa": "课程 GPA",
    "teacher": "老师评分",
    "checkin": "少点名",
    "workload": "事少轻松",
    "relevance": "专业相关",
    "confidence": "样本可信度",
    "risk": "跨专业风险",
}


@dataclass
class Preference:
    mode: str = "course"
    limit: int = 50
    min_gpa: float = 3.8
    min_sample: int = 10
    max_checkin: float | None = None
    exclusions: list[str] = field(default_factory=list)
    inclusions: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS)
    )


def _first_number(prompt, patterns, converter):
    for pattern in patterns:
        match = re.search(pattern, prompt, re.IGNORECASE)
        if match:
            return converter(match.group(1))
    return None


def _parse_exclusions(prompt):
    exclusions = []
    for match in re.finditer(r"(?:排除|不选)\s*([^，。；;]+)", prompt):
        text = re.split(r"(?:前\s*\d+|按(?:课程|老师)|排序)", match.group(1))[0]
        for value in re.split(r"[、,/和与及\s]+", text):
            value = value.strip("课程老师类的")
            if value and value not in exclusions:
                exclusions.append(value)
    return exclusions


def parse_preference(prompt):
    text = re.sub(r"\s+", "", prompt)
    preference = Preference()

    if re.search(r"按老师|老师排序|推荐老师|教师排序", text):
        preference.mode = "teacher"

    limit = _first_number(
        text,
        [r"前(\d+)(?:门|个|位)?", r"(?:显示|推荐)(\d+)(?:门|个|位)"],
        int,
    )
    if limit is not None:
        preference.limit = max(1, min(limit, 500))

    min_gpa = _first_number(
        text,
        [
            r"(?:GPA|绩点|平均分).{0,6}(?:至少|不低于|>=|≥)(\d(?:\.\d+)?)",
            r"(?:至少|不低于|>=|≥)(\d(?:\.\d+)?)(?:的)?(?:GPA|绩点)",
        ],
        float,
    )
    if min_gpa is not None:
        preference.min_gpa = min_gpa

    min_sample = _first_number(
        text,
        [
            r"(?:样本|评价|评分人数).{0,5}(?:至少|不低于|>=|≥)(\d+)",
            r"(?:至少|不低于|>=|≥)(\d+)(?:个|条|人)?(?:样本|评价)",
        ],
        int,
    )
    if min_sample is not None:
        preference.min_sample = min_sample

    max_checkin = _first_number(
        text,
        [
            r"(?:点名率|签到率).{0,5}(?:低于|不高于|不超过|<=|≤)(\d+(?:\.\d+)?)%?",
            r"(?:点名率|签到率)(\d+(?:\.\d+)?)%?以下",
        ],
        float,
    )
    if max_checkin is not None:
        preference.max_checkin = max_checkin

    weights = preference.weights
    if re.search(r"只看.{0,4}(?:给分|绩点|GPA)", text, re.IGNORECASE):
        for key in weights:
            weights[key] = 0
        weights["gpa"] = 12
    elif re.search(r"(?:给分高|高绩点|刷分|GPA).{0,4}(?:最重要|优先|核心)", text, re.IGNORECASE):
        weights["gpa"] = 10
    elif re.search(r"给分高|给分好|高绩点|刷分|GPA|满绩", text, re.IGNORECASE):
        weights["gpa"] = 7

    if re.search(r"老师评分|好老师|老师好|教学好", text):
        weights["teacher"] = 3
    if re.search(r"(?:不点名|不签到|少点名|少签到|点名少|签到少).{0,4}(?:最重要|优先|核心)", text):
        weights["checkin"] = 10
    elif re.search(r"不点名|不签到|少点名|少签到|点名少|签到少", text):
        weights["checkin"] = 3
    if re.search(r"事少|事情少|作业少|轻松|水课|无考试|没有考试", text):
        weights["workload"] = 3
    if re.search(r"软件|计算机|人工智能|数据|算法|专业相关", text):
        weights["relevance"] = 2.5
    if re.search(r"样本多|评价多|评论多|靠谱|可信", text):
        weights["confidence"] = 2.5
    if re.search(r"不要太难|不能太难|低风险|跨专业简单|门槛低", text):
        weights["risk"] = 3

    if re.search(r"点名无所谓|签到无所谓|不在意点名|不在意签到", text):
        weights["checkin"] = 0
    if re.search(r"不要求专业相关|专业无所谓|可以不相关|不在意专业", text):
        weights["relevance"] = 0
    if re.search(r"绩点要求不高|GPA要求不高|不在意绩点|给分无所谓", text, re.IGNORECASE):
        weights["gpa"] = 1.5
    if re.search(r"难度无所谓|不在意难度", text):
        weights["risk"] = 0
        weights["workload"] = 0

    preference.exclusions = _parse_exclusions(prompt)
    return preference


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _course_components(row):
    teacher_score = row.get("teacher_score")
    checkin = row.get("checkin_ratio")
    easy = row.get("course_easy", 0) + row.get("teacher_easy", 0) * 0.08
    hard = row.get("course_hard", 0) + row.get("teacher_hard", 0) * 0.10
    relevance = {"强相关": 1.0, "相关": 0.7, "跨专业": 0.2}.get(
        row.get("relevance"), 0.2
    )
    return {
        "gpa": _clamp((row["avg_gpa"] - 3.0) / 2.0),
        "teacher": _clamp((teacher_score if teacher_score is not None else 7.0) / 10),
        "checkin": 0.5 if checkin is None else _clamp(1 - checkin / 100),
        "workload": _clamp(0.5 + math.tanh((easy - hard) / 8) * 0.5),
        "relevance": relevance,
        "confidence": _clamp(math.log10(row["sample_count"] + 1) / math.log10(501)),
        "risk": _clamp(row.get("risk", 0) / 8),
    }


def _matches_filters(row, preference):
    if row["avg_gpa"] < preference.min_gpa:
        return False
    if row["sample_count"] < preference.min_sample:
        return False
    if preference.max_checkin is not None:
        checkin = row.get("checkin_ratio")
        if checkin is None or checkin > preference.max_checkin:
            return False

    haystack = " ".join(
        str(row.get(key, "")) for key in ("course", "teacher", "college")
    ).lower()
    if any(keyword.lower() in haystack for keyword in preference.exclusions):
        return False
    if preference.inclusions and not any(
        keyword.lower() in haystack for keyword in preference.inclusions
    ):
        return False
    return True


def _score_course(row, preference):
    components = _course_components(row)
    positive_keys = [
        "gpa", "teacher", "checkin", "workload", "relevance", "confidence"
    ]
    positive_weight = sum(preference.weights[key] for key in positive_keys)
    if positive_weight == 0:
        positive_weight = 1
    positive = sum(
        components[key] * preference.weights[key] for key in positive_keys
    ) / positive_weight
    score = positive * 100 - components["risk"] * preference.weights["risk"] * 8

    reasons = []
    if row["avg_gpa"] >= 4.4:
        reasons.append(f"GPA {row['avg_gpa']:.2f}")
    if row["sample_count"] >= 50:
        reasons.append(f"{row['sample_count']} 个样本")
    if row.get("checkin_ratio") is not None and row["checkin_ratio"] <= 30:
        reasons.append(f"点名率 {row['checkin_ratio']:.0f}%")
    if components["workload"] >= 0.65:
        reasons.append("评论偏轻松")
    if row.get("relevance") in {"强相关", "相关"}:
        reasons.append(row["relevance"])

    result = dict(row)
    result["personalized_score"] = round(score, 1)
    result["match_reasons"] = reasons[:4] or ["符合基础筛选"]
    return result


def rank_courses(rows, preference, apply_limit=True):
    ranked = [
        _score_course(row, preference)
        for row in rows
        if _matches_filters(row, preference)
    ]
    ranked.sort(
        key=lambda row: (
            -row["personalized_score"],
            -row["avg_gpa"],
            -row["sample_count"],
        )
    )
    return ranked[:preference.limit] if apply_limit else ranked


def rank_teachers(rows, preference):
    grouped = defaultdict(list)
    for row in rank_courses(rows, preference, apply_limit=False):
        grouped[row["teacher_id"]].append(row)

    teachers = []
    for teacher_id, courses in grouped.items():
        courses.sort(key=lambda row: -row["personalized_score"])
        top = courses[:3]
        best = top[0]
        aggregate = best["personalized_score"] * 0.65 + (
            sum(row["personalized_score"] for row in top) / len(top)
        ) * 0.35
        teachers.append({
            "teacher_id": teacher_id,
            "teacher": best["teacher"],
            "college": best["college"],
            "personalized_score": round(aggregate, 1),
            "teacher_score": best.get("teacher_score"),
            "checkin_ratio": best.get("checkin_ratio"),
            "course_count": len(courses),
            "top_courses": [row["course"] for row in top],
            "best_gpa": max(row["avg_gpa"] for row in courses),
            "total_samples": sum(row["sample_count"] for row in courses),
            "match_reasons": best["match_reasons"],
            "url": best["url"],
        })

    teachers.sort(
        key=lambda row: (
            -row["personalized_score"],
            -(row["teacher_score"] or 0),
            -row["total_samples"],
        )
    )
    return teachers[:preference.limit]


def _preference_summary(preference):
    priorities = sorted(
        (
            (label, preference.weights[key])
            for key, label in WEIGHT_LABELS.items()
            if preference.weights[key] > 0
        ),
        key=lambda item: -item[1],
    )
    summary = [f"{label} × {weight:g}" for label, weight in priorities]
    summary.extend([
        f"GPA ≥ {preference.min_gpa:g}",
        f"样本 ≥ {preference.min_sample}",
    ])
    if preference.max_checkin is not None:
        summary.append(f"点名率 ≤ {preference.max_checkin:g}%")
    if preference.exclusions:
        summary.append("排除：" + "、".join(preference.exclusions))
    return summary


def _render_course_rows(rows):
    rendered = []
    for index, row in enumerate(rows, 1):
        reasons = "".join(
            f"<span class=\"tag\">{html.escape(reason)}</span>"
            for reason in row["match_reasons"]
        )
        checkin = (
            "未知" if row.get("checkin_ratio") is None
            else f"{row['checkin_ratio']:.1f}%"
        )
        teacher_score = (
            "未知" if row.get("teacher_score") is None
            else f"{row['teacher_score']:.2f}"
        )
        rendered.append(f"""
          <tr data-search="{html.escape((row['course'] + ' ' + row['teacher'] + ' ' + row['college']).lower())}">
            <td class="rank">{index:02d}</td>
            <td><a href="{html.escape(row['url'])}" target="_blank" rel="noopener">{html.escape(row['course'])}</a><small>{html.escape(row['teacher'])} · {html.escape(row['college'])}</small></td>
            <td class="score">{row['personalized_score']:.1f}</td>
            <td class="gpa">{row['avg_gpa']:.2f}</td>
            <td>{row['sample_count']}</td>
            <td>{teacher_score}</td>
            <td>{checkin}</td>
            <td>{reasons}</td>
          </tr>""")
    return "".join(rendered)


def _render_teacher_rows(rows):
    rendered = []
    for index, row in enumerate(rows, 1):
        reasons = "".join(
            f"<span class=\"tag\">{html.escape(reason)}</span>"
            for reason in row["match_reasons"]
        )
        courses = "、".join(row["top_courses"])
        checkin = (
            "未知" if row.get("checkin_ratio") is None
            else f"{row['checkin_ratio']:.1f}%"
        )
        teacher_score = (
            "未知" if row.get("teacher_score") is None
            else f"{row['teacher_score']:.2f}"
        )
        rendered.append(f"""
          <tr data-search="{html.escape((row['teacher'] + ' ' + row['college'] + ' ' + courses).lower())}">
            <td class="rank">{index:02d}</td>
            <td><a href="{html.escape(row['url'])}" target="_blank" rel="noopener">{html.escape(row['teacher'])}</a><small>{html.escape(row['college'])}</small></td>
            <td class="score">{row['personalized_score']:.1f}</td>
            <td class="gpa">{row['best_gpa']:.2f}</td>
            <td>{row['course_count']}</td>
            <td>{teacher_score}</td>
            <td>{checkin}</td>
            <td>{html.escape(courses)}<br>{reasons}</td>
          </tr>""")
    return "".join(rendered)


def render_personalized_html(rows, preference, prompt, generated_at):
    mode_label = "老师" if preference.mode == "teacher" else "课程"
    chips = "".join(
        f"<span class=\"chip\">{html.escape(item)}</span>"
        for item in _preference_summary(preference)
    )
    body_rows = (
        _render_teacher_rows(rows)
        if preference.mode == "teacher"
        else _render_course_rows(rows)
    )
    fifth_header = "候选课程数" if preference.mode == "teacher" else "样本"
    last_header = "代表课程 / 理由" if preference.mode == "teacher" else "匹配理由"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>我的查老师推荐表</title>
  <style>
    :root {{
      --paper: #f3f0e7; --surface: #fffef8; --ink: #17211d;
      --muted: #667069; --line: #c9c8bd; --green: #176247;
      --blue: #245c8a; --amber: #9a5b0b;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--ink); background: var(--paper); font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; letter-spacing: 0; }}
    header {{ background: var(--surface); border-bottom: 1px solid var(--ink); }}
    .head, main {{ max-width: 1320px; margin: 0 auto; padding: 24px; }}
    .head {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 24px; align-items: end; }}
    h1 {{ margin: 0; font-family: "Noto Serif CJK SC", "Songti SC", serif; font-size: 42px; }}
    .prompt {{ margin: 12px 0 0; color: var(--muted); line-height: 1.7; overflow-wrap: anywhere; }}
    .stamp {{ border-left: 4px solid var(--green); padding-left: 12px; white-space: nowrap; }}
    .stamp strong, .stamp small, td small {{ display: block; }}
    .stamp small, td small {{ color: var(--muted); margin-top: 5px; }}
    .understanding {{ padding: 18px 0 22px; border-bottom: 1px solid var(--ink); }}
    h2 {{ margin: 0 0 12px; font: 700 20px "Noto Serif CJK SC", "Songti SC", serif; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 7px; }}
    .chip, .tag {{ display: inline-block; border-radius: 3px; font-size: 11px; }}
    .chip {{ padding: 6px 9px; color: var(--blue); background: #dce8f0; }}
    .tag {{ margin: 2px 4px 2px 0; padding: 3px 6px; color: var(--green); background: #dce9df; }}
    .toolbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-top: 24px; }}
    .toolbar p {{ color: var(--muted); font-size: 12px; }}
    input {{ width: min(360px, 100%); min-height: 40px; padding: 8px 10px; border: 1px solid var(--ink); border-radius: 4px; background: var(--surface); font: inherit; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--ink); background: var(--surface); }}
    table {{ width: 100%; min-width: 960px; border-collapse: collapse; }}
    th {{ padding: 10px 9px; color: var(--surface); background: var(--ink); text-align: left; font-size: 11px; white-space: nowrap; }}
    td {{ padding: 12px 9px; border-bottom: 1px solid var(--line); font-size: 12px; vertical-align: top; }}
    tbody tr:hover {{ background: #edf2e9; }}
    a {{ color: var(--ink); font-size: 14px; font-weight: 800; text-decoration: none; }}
    a:hover {{ color: var(--green); text-decoration: underline; }}
    .rank {{ color: var(--muted); font-family: ui-monospace, monospace; }}
    .score {{ color: var(--blue); font: 800 16px ui-monospace, monospace; }}
    .gpa {{ color: var(--green); font: 900 18px ui-monospace, monospace; }}
    .empty {{ padding: 32px; color: var(--muted); text-align: center; }}
    footer {{ max-width: 1320px; margin: 0 auto; padding: 0 24px 36px; color: var(--muted); font-size: 11px; line-height: 1.7; }}
    @media (max-width: 700px) {{
      .head {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 32px; }}
      .toolbar {{ align-items: stretch; flex-direction: column; }}
      input {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="head">
      <div><h1>我的{mode_label}推荐表</h1><p class="prompt">“{html.escape(prompt)}”</p></div>
      <div class="stamp"><strong>{generated_at}</strong><small>{len(rows)} 个结果</small></div>
    </div>
  </header>
  <main>
    <section class="understanding"><h2>系统理解</h2><div class="chips">{chips}</div></section>
    <div class="toolbar"><h2>{mode_label}排序</h2><input id="search" type="search" placeholder="搜索课程、老师或学院"></div>
    <p id="count">{len(rows)} 个结果</p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>{mode_label}</th><th>匹配分</th><th>最高 / 平均 GPA</th><th>{fifth_header}</th><th>老师评分</th><th>点名率</th><th>{last_header}</th></tr></thead>
        <tbody id="rows">{body_rows}</tbody>
      </table>
      <div id="empty" class="empty" hidden>没有匹配的结果。</div>
    </div>
  </main>
  <footer>结果来自本地公开数据快照。GPA 是课程维度；老师评分、点名率和部分评论是老师维度。自然语言采用规则解析，选课前请核对当学期开课信息与培养方案。</footer>
  <script>
    const search = document.getElementById("search");
    const rows = [...document.querySelectorAll("#rows tr")];
    search.addEventListener("input", () => {{
      const query = search.value.trim().toLowerCase();
      let visible = 0;
      rows.forEach(row => {{
        const show = !query || row.dataset.search.includes(query);
        row.hidden = !show;
        if (show) visible += 1;
      }});
      document.getElementById("count").textContent = `${{visible}} 个结果`;
      document.getElementById("empty").hidden = visible !== 0;
    }});
  </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(
        description="根据中文自然语言偏好生成查老师课程或老师推荐 HTML。"
    )
    parser.add_argument("--prompt", required=True, help="自然语言选课偏好")
    parser.add_argument("--db", default="chalaoshi_public_data.sqlite")
    parser.add_argument("--output", default="my_recommendations.html")
    args = parser.parse_args()

    preference = parse_preference(args.prompt)
    conn = sqlite3.connect(args.db)
    try:
        rows = build_recommendations(conn, min_sample=1, min_gpa=0)
    finally:
        conn.close()

    ranked = (
        rank_teachers(rows, preference)
        if preference.mode == "teacher"
        else rank_courses(rows, preference)
    )
    output = Path(args.output)
    output.write_text(
        render_personalized_html(
            ranked,
            preference,
            args.prompt,
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ),
        encoding="utf-8",
    )
    mode_label = "老师" if preference.mode == "teacher" else "课程"
    print(f"已生成 {output}，共 {len(ranked)} 个{mode_label}结果。")


if __name__ == "__main__":
    main()
