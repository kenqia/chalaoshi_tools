import argparse
import csv
import html
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


PRO_STRONG = [
    "软件", "程序设计", "算法", "数据库", "操作系统", "计算机系统", "计算机网络",
    "网络安全", "信息安全", "软件安全", "人工智能", "机器学习", "数据挖掘",
    "自然语言处理", "编译", "web", "b/s", "小程序", "云计算", "数据结构",
]
PRO_RELATED = [
    "数据可视化", "大数据", "数据分析", "计算机", "信息系统", "数字媒体",
    "人机交互", "智能", "机器人", "隐私保护", "工程伦理", "科技史",
]
CS_CONTEXT = ["课程综合实践", "工程实践", "认识实习", "专业综合实践"]
LOW_BARRIER = [
    "导论", "概论", "赏析", "文化", "历史", "伦理", "心理", "电影", "音乐",
    "艺术", "社会", "法律", "博物馆", "通识", "创业", "沟通", "写作",
]
RISK_KEYWORDS = {
    "临床": 3, "实习": 2, "毕业设计": 4, "毕业论文": 4, "专业实习": 4,
    "课程设计": 2, "强化训练": 3, "实验": 1, "试验": 1, "实践": 1,
    "训练": 1, "竞赛": 3, "专题研究": 2, "数论": 3, "量子": 3,
    "高等": 2, "力学": 2, "化学": 2, "医学": 2, "外科": 3,
    "解剖": 3, "病理": 3, "优化基本理论": 2, "农艺实践": 3,
}
EXCLUDE_CROSS_MAJOR = [
    "临床实习", "外科实习", "专业实习", "毕业实习", "毕业论文", "毕业设计",
    "体育训练与比赛", "科研实践",
]

EASY_KEYWORDS = {
    "事少": 3.0,
    "事情少": 3.0,
    "作业少": 2.5,
    "没有作业": 2.5,
    "无作业": 2.5,
    "不点名": 2.0,
    "不签到": 2.0,
    "无需签到": 2.0,
    "给分好": 3.0,
    "给分高": 3.0,
    "给分不错": 2.0,
    "高绩点": 2.0,
    "满绩": 2.0,
    "捞": 1.5,
    "无考试": 2.5,
    "没有考试": 2.5,
    "开卷": 1.0,
    "轻松": 1.5,
    "摸鱼": 1.5,
    "水课": 1.5,
}
HARD_KEYWORDS = {
    "事多": 3.0,
    "事情多": 3.0,
    "作业多": 2.5,
    "作业很多": 3.0,
    "任务多": 2.5,
    "签到": 1.2,
    "点名": 1.2,
    "考试难": 3.0,
    "很难": 2.0,
    "太难": 2.5,
    "难度大": 2.5,
    "卡绩": 3.0,
    "给分低": 3.0,
    "给分差": 3.0,
    "分低": 2.5,
    "很卷": 2.5,
    "卷": 1.0,
    "pre很多": 2.0,
    "汇报多": 2.0,
}


def parse_sample_count(value):
    if value is None:
        return 0
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else 0


def numeric(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def professional_relevance(course_name, college):
    text = course_name.lower()
    strong = sum(1 for keyword in PRO_STRONG if keyword in text)
    related = sum(1 for keyword in PRO_RELATED if keyword in text)
    cs_college = "计算机科学与技术学院" in college or "软件学院" in college

    context = 1 if cs_college and any(keyword in course_name for keyword in CS_CONTEXT) else 0
    score = strong * 3 + related + context
    if score >= 3:
        return score, "强相关"
    if score >= 1:
        return score, "相关"
    return 0, "跨专业"


def course_risk(course_name, college):
    reasons = []
    risk = 0
    for keyword in EXCLUDE_CROSS_MAJOR:
        if keyword in course_name:
            risk += 4
            reasons.append(f"包含“{keyword}”")
    for keyword, weight in RISK_KEYWORDS.items():
        if keyword in course_name and f"包含“{keyword}”" not in reasons:
            risk += weight
            reasons.append(f"包含“{keyword}”")
    if "医学院" in college and "导论" not in course_name and "人工智能" not in course_name:
        risk += 2
        reasons.append("医学专业门槛")
    return risk, reasons


def comment_signals(text):
    text = text or ""
    easy = sum(weight * text.count(keyword) for keyword, weight in EASY_KEYWORDS.items())
    hard = sum(weight * text.count(keyword) for keyword, weight in HARD_KEYWORDS.items())
    return {"easy": round(easy, 2), "hard": round(hard, 2)}


def latest_courses(conn):
    return conn.execute("""
        WITH latest AS (
            SELECT
                g.*,
                ROW_NUMBER() OVER (
                    PARTITION BY teacher_id, course_name
                    ORDER BY crawled_at DESC, id DESC
                ) AS rn
            FROM gpa_courses g
        )
        SELECT
            l.teacher_id,
            l.course_name,
            l.avg_gpa,
            l.sample_count,
            t.name,
            t.college,
            t.score,
            t.rating_count,
            t.checkin_ratio,
            t.comment_count,
            t.url
        FROM latest l
        JOIN teachers t ON t.teacher_id = l.teacher_id
        WHERE l.rn = 1
    """).fetchall()


def load_comments(conn):
    comments = defaultdict(list)
    for teacher_id, content, comment_date in conn.execute("""
        SELECT teacher_id, COALESCE(content, ''), COALESCE(comment_date, date, '')
        FROM comments
        ORDER BY teacher_id, COALESCE(comment_date, date, '') DESC
    """):
        comments[int(teacher_id)].append({
            "content": content or "",
            "date": comment_date or "",
        })
    return comments


def matching_snippets(course_name, teacher_comments, limit=3):
    matches = []
    short_name = re.sub(r"[（(].*?[）)]", "", course_name).strip()
    for comment in teacher_comments:
        text = comment["content"]
        has_course = (
            len(short_name) >= 3
            and (course_name in text or short_name in text)
        )
        has_signal = any(keyword in text for keyword in (*EASY_KEYWORDS, *HARD_KEYWORDS))
        if has_course and has_signal:
            matches.append(comment)
    if not matches:
        matches = [
            comment for comment in teacher_comments
            if any(keyword in comment["content"] for keyword in (*EASY_KEYWORDS, *HARD_KEYWORDS))
        ]
    return matches[:limit]


def clamp(value, low, high):
    return max(low, min(high, value))


def build_recommendations(conn, min_sample=10, min_gpa=3.8):
    comments_by_teacher = load_comments(conn)
    recommendations = []

    for row in latest_courses(conn):
        (
            teacher_id, course_name, avg_gpa_raw, sample_raw, teacher_name, college,
            teacher_score_raw, rating_count_raw, checkin_raw, comment_count_raw, url,
        ) = row

        avg_gpa = numeric(avg_gpa_raw)
        sample_count = parse_sample_count(sample_raw)
        if avg_gpa is None or sample_count < min_sample or avg_gpa < min_gpa:
            continue

        teacher_score = numeric(teacher_score_raw)
        rating_count = parse_sample_count(rating_count_raw)
        checkin_ratio = numeric(checkin_raw)
        relevance_score, relevance_label = professional_relevance(course_name, college or "")
        risk, risk_reasons = course_risk(course_name, college or "")

        teacher_comments = comments_by_teacher.get(int(teacher_id), [])
        course_comments = [
            item for item in teacher_comments
            if course_name in item["content"]
            or (
                len(re.sub(r"[（(].*?[）)]", "", course_name).strip()) >= 3
                and re.sub(r"[（(].*?[）)]", "", course_name).strip() in item["content"]
            )
        ]
        course_text = "\n".join(item["content"] for item in course_comments)
        teacher_text = "\n".join(item["content"] for item in teacher_comments)
        course_signal = comment_signals(course_text)
        teacher_signal = comment_signals(teacher_text)

        sample_for_math = min(sample_count, 500)
        adjusted_gpa = (
            avg_gpa * sample_for_math + 4.0 * 30
        ) / (sample_for_math + 30)
        gpa_points = clamp((adjusted_gpa - 3.5) / 1.5 * 50, 0, 50)
        confidence_points = clamp(math.log10(sample_count + 1) / math.log10(501) * 10, 0, 10)

        if teacher_score is None:
            teacher_points = 7
            adjusted_teacher_score = None
        else:
            adjusted_teacher_score = (
                teacher_score * min(rating_count, 300) + 7.5 * 20
            ) / (min(rating_count, 300) + 20)
            teacher_points = adjusted_teacher_score / 10 * 12

        if checkin_ratio is None:
            checkin_points = 5
        else:
            checkin_points = (100 - checkin_ratio) / 100 * 10

        signal_net = (
            course_signal["easy"] * 1.8
            - course_signal["hard"] * 2.1
            + teacher_signal["easy"] * 0.08
            - teacher_signal["hard"] * 0.10
        )
        workload_points = 6 + math.tanh(signal_net / 8) * 6
        relevance_points = 8 if relevance_label == "强相关" else 4 if relevance_label == "相关" else 0
        low_barrier_bonus = 3 if any(keyword in course_name for keyword in LOW_BARRIER) else 0
        risk_penalty = min(risk * 2.5, 18)

        total = (
            gpa_points
            + confidence_points
            + teacher_points
            + checkin_points
            + workload_points
            + relevance_points
            + low_barrier_bonus
            - risk_penalty
        )

        if relevance_label == "跨专业" and risk >= 4:
            category = "不建议跨选"
        elif relevance_label in {"强相关", "相关"}:
            category = "软件相关"
        else:
            category = "跨专业低风险"

        snippets = matching_snippets(course_name, teacher_comments)
        recommendations.append({
            "teacher_id": int(teacher_id),
            "teacher": teacher_name or "未知",
            "college": college or "未知",
            "course": course_name,
            "avg_gpa": round(avg_gpa, 2),
            "adjusted_gpa": round(adjusted_gpa, 3),
            "sample_count": sample_count,
            "sample_label": sample_raw,
            "teacher_score": teacher_score,
            "adjusted_teacher_score": round(adjusted_teacher_score, 2)
            if adjusted_teacher_score is not None else None,
            "rating_count": rating_count,
            "checkin_ratio": round(checkin_ratio, 1) if checkin_ratio is not None else None,
            "comment_count": parse_sample_count(comment_count_raw),
            "relevance": relevance_label,
            "relevance_score": relevance_score,
            "risk": risk,
            "risk_reasons": risk_reasons,
            "category": category,
            "course_easy": course_signal["easy"],
            "course_hard": course_signal["hard"],
            "teacher_easy": teacher_signal["easy"],
            "teacher_hard": teacher_signal["hard"],
            "evidence_scope": "课程点名评论" if course_comments else "老师整体评论",
            "snippets": snippets,
            "score": round(total, 1),
            "url": url or f"https://chalaoshi.de/t/{teacher_id}/",
        })

    recommendations.sort(
        key=lambda item: (
            item["category"] != "软件相关",
            -item["score"],
            -item["avg_gpa"],
            -item["sample_count"],
        )
    )
    return recommendations


def export_csv(rows, path):
    fields = [
        "category", "score", "course", "teacher", "college", "avg_gpa",
        "sample_count", "teacher_score", "rating_count", "checkin_ratio",
        "relevance", "risk", "course_easy", "course_hard", "url",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def render_html(rows, stats, generated_at):
    safe_json = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    top_software = [
        row for row in rows
        if row["category"] == "软件相关"
        and row["risk"] == 0
        and (row["checkin_ratio"] is None or row["checkin_ratio"] <= 35)
    ][:6]
    top_cross = [
        row for row in rows
        if row["category"] == "跨专业低风险" and row["risk"] <= 1
    ][:6]

    def top_rows(items):
        return "\n".join(
            f"""            <a class="pick" href="{html.escape(item['url'])}" target="_blank" rel="noopener">
              <span class="pick-rank">{index:02d}</span>
              <span class="pick-main">
                <strong>{html.escape(item['course'])}</strong>
                <small>{html.escape(item['teacher'])} · {html.escape(item['college'])}</small>
              </span>
              <span class="pick-gpa">{item['avg_gpa']:.2f}<small>GPA</small></span>
              <span class="pick-meta">{item['sample_label']} 样本<br>{'未知' if item['checkin_ratio'] is None else f"{item['checkin_ratio']:.1f}%"} 点名</span>
            </a>"""
            for index, item in enumerate(items, 1)
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>刷分选课决策表</title>
  <style>
    :root {{
      --paper: #f5f2e9;
      --ink: #17211d;
      --muted: #667069;
      --line: #c9c8bd;
      --green: #166247;
      --green-soft: #dce9df;
      --blue: #245c8a;
      --blue-soft: #dde8f0;
      --amber: #9a5b0b;
      --amber-soft: #f2e5c9;
      --red: #a13b32;
      --red-soft: #f1deda;
      --white: #fffef8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: var(--paper);
      font-family: "Source Han Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      border-bottom: 1px solid var(--ink);
      background: var(--white);
    }}
    .masthead {{
      max-width: 1480px;
      margin: 0 auto;
      padding: 28px 28px 22px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 28px;
      align-items: end;
    }}
    h1 {{
      margin: 0;
      font-family: "Noto Serif CJK SC", "Songti SC", SimSun, serif;
      font-size: clamp(30px, 5vw, 62px);
      line-height: 1;
      font-weight: 800;
      letter-spacing: 0;
    }}
    .subtitle {{
      margin: 12px 0 0;
      max-width: 760px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.7;
    }}
    .stamp {{
      border-left: 4px solid var(--green);
      padding-left: 14px;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .stamp strong {{ display: block; font-size: 20px; }}
    .stamp small {{ color: var(--muted); }}
    main {{ max-width: 1480px; margin: 0 auto; padding: 24px 28px 48px; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border: 1px solid var(--ink);
      background: var(--ink);
      gap: 1px;
    }}
    .metric {{ background: var(--white); padding: 16px; min-width: 0; }}
    .metric strong {{ display: block; font-size: 28px; font-variant-numeric: tabular-nums; }}
    .metric span {{ color: var(--muted); font-size: 12px; }}
    .section-head {{
      margin: 34px 0 12px;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: baseline;
      border-bottom: 1px solid var(--ink);
      padding-bottom: 8px;
    }}
    h2 {{ margin: 0; font-family: "Noto Serif CJK SC", "Songti SC", serif; font-size: 22px; }}
    .section-head p {{ margin: 0; color: var(--muted); font-size: 12px; }}
    .picks {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--line); border: 1px solid var(--line); }}
    .pick {{
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr) 76px 86px;
      align-items: center;
      min-height: 82px;
      padding: 12px;
      gap: 10px;
      background: var(--white);
      color: inherit;
      text-decoration: none;
      transition: background .16s ease, transform .16s ease;
    }}
    .pick:hover {{ background: var(--green-soft); transform: translateX(2px); }}
    .pick-rank {{ color: var(--green); font: 700 20px ui-monospace, monospace; }}
    .pick-main {{ min-width: 0; }}
    .pick-main strong, .pick-main small {{ display: block; overflow-wrap: anywhere; }}
    .pick-main strong {{ font-size: 15px; }}
    .pick-main small {{ margin-top: 6px; color: var(--muted); font-size: 11px; }}
    .pick-gpa {{ color: var(--green); font: 800 24px ui-monospace, monospace; text-align: right; }}
    .pick-gpa small {{ display: block; color: var(--muted); font-size: 9px; }}
    .pick-meta {{ color: var(--muted); font-size: 10px; line-height: 1.5; text-align: right; }}
    .controls {{
      position: sticky;
      top: 0;
      z-index: 5;
      display: grid;
      grid-template-columns: 2fr repeat(5, minmax(120px, 1fr));
      gap: 8px;
      padding: 10px 0;
      background: var(--paper);
      border-bottom: 1px solid var(--ink);
    }}
    input, select {{
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--ink);
      border-radius: 4px;
      background: var(--white);
      color: var(--ink);
      padding: 8px 10px;
      font: inherit;
    }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--ink); border-top: 0; background: var(--white); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 1120px; }}
    th {{
      position: sticky;
      top: 61px;
      z-index: 4;
      padding: 10px 8px;
      background: var(--ink);
      color: var(--white);
      font-size: 11px;
      text-align: left;
      white-space: nowrap;
    }}
    td {{ padding: 11px 8px; border-bottom: 1px solid var(--line); vertical-align: top; font-size: 12px; }}
    tbody tr:hover {{ background: #eef2ea; }}
    .rank {{ font: 700 14px ui-monospace, monospace; color: var(--muted); }}
    .course-name {{ display: block; font-weight: 800; font-size: 14px; color: var(--ink); text-decoration: none; }}
    .course-name:hover {{ color: var(--green); text-decoration: underline; }}
    .teacher-line {{ margin-top: 5px; color: var(--muted); }}
    .num {{ font-family: ui-monospace, "SFMono-Regular", monospace; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .gpa {{ color: var(--green); font-size: 17px; font-weight: 900; }}
    .tag {{
      display: inline-block;
      padding: 3px 6px;
      border-radius: 3px;
      font-size: 10px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .tag-pro {{ color: var(--blue); background: var(--blue-soft); }}
    .tag-cross {{ color: var(--green); background: var(--green-soft); }}
    .tag-risk {{ color: var(--red); background: var(--red-soft); }}
    .tag-warn {{ color: var(--amber); background: var(--amber-soft); }}
    .signal {{ min-width: 120px; line-height: 1.6; }}
    details {{ max-width: 340px; }}
    summary {{ cursor: pointer; color: var(--blue); font-weight: 700; }}
    blockquote {{
      margin: 8px 0;
      padding: 7px 9px;
      border-left: 3px solid var(--line);
      background: var(--paper);
      color: #414a45;
      line-height: 1.5;
      font-size: 11px;
    }}
    .method {{
      margin-top: 28px;
      padding: 18px 0;
      border-top: 1px solid var(--ink);
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 28px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.7;
    }}
    .method strong {{ color: var(--ink); }}
    .empty {{ padding: 32px; text-align: center; color: var(--muted); }}
    @media (max-width: 900px) {{
      .masthead {{ grid-template-columns: 1fr; }}
      .metrics {{ grid-template-columns: 1fr 1fr; }}
      .picks {{ grid-template-columns: 1fr; }}
      .controls {{ grid-template-columns: 1fr 1fr; position: static; }}
      .controls input {{ grid-column: 1 / -1; }}
      th {{ position: static; }}
      .method {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 560px) {{
      main, .masthead {{ padding-left: 14px; padding-right: 14px; }}
      .metrics {{ grid-template-columns: 1fr; }}
      .controls {{ grid-template-columns: 1fr; }}
      .controls input {{ grid-column: auto; }}
      .pick {{ grid-template-columns: 34px minmax(0, 1fr) 62px; }}
      .pick-meta {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="masthead">
      <div>
        <h1>刷分选课决策表</h1>
        <p class="subtitle">面向软件工程大三学生。核心目标是高 GPA，次级目标是少点名、事少、专业相关；跨专业课程会过滤明显的专业实习、临床、毕业设计等高门槛项目。</p>
      </div>
      <div class="stamp"><strong>{generated_at}</strong><small>本地数据分析生成</small></div>
    </div>
  </header>
  <main>
    <section class="metrics" aria-label="数据概览">
      <div class="metric"><strong>{stats['teachers']:,}</strong><span>老师记录</span></div>
      <div class="metric"><strong>{stats['comments']:,}</strong><span>评论正文</span></div>
      <div class="metric"><strong>{stats['courses']:,}</strong><span>课程 GPA 记录</span></div>
      <div class="metric"><strong>{len(rows):,}</strong><span>通过基础筛选的候选</span></div>
    </section>

    <div class="section-head"><h2>软件相关优先队列</h2><p>专业相关性已纳入总分</p></div>
    <section class="picks">{top_rows(top_software)}</section>

    <div class="section-head"><h2>跨专业低风险备选</h2><p>排除明显专业实习与高门槛课程</p></div>
    <section class="picks">{top_rows(top_cross)}</section>

    <div class="section-head"><h2>完整候选表</h2><p id="result-count"></p></div>
    <section class="controls" aria-label="筛选条件">
      <input id="search" type="search" placeholder="搜索课程、老师或学院">
      <select id="category">
        <option value="">全部类别</option>
        <option value="软件相关">软件相关</option>
        <option value="跨专业低风险">跨专业低风险</option>
        <option value="不建议跨选">不建议跨选</option>
      </select>
      <select id="min-gpa">
        <option value="3.8">GPA ≥ 3.8</option>
        <option value="4.0">GPA ≥ 4.0</option>
        <option value="4.2" selected>GPA ≥ 4.2</option>
        <option value="4.4">GPA ≥ 4.4</option>
        <option value="4.6">GPA ≥ 4.6</option>
      </select>
      <select id="min-sample">
        <option value="10">样本 ≥ 10</option>
        <option value="20" selected>样本 ≥ 20</option>
        <option value="50">样本 ≥ 50</option>
        <option value="100">样本 ≥ 100</option>
      </select>
      <select id="max-checkin">
        <option value="101">不限点名率</option>
        <option value="50">点名率 ≤ 50%</option>
        <option value="30" selected>点名率 ≤ 30%</option>
        <option value="15">点名率 ≤ 15%</option>
      </select>
      <select id="sort">
        <option value="score">综合推荐</option>
        <option value="gpa">GPA 最高</option>
        <option value="checkin">点名最少</option>
        <option value="sample">样本最多</option>
        <option value="easy">事少信号</option>
      </select>
    </section>

    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>#</th><th>课程 / 老师</th><th>类别</th><th>综合</th><th>平均 GPA</th>
          <th>样本</th><th>老师评分</th><th>点名率</th><th>评论信号</th><th>证据</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="empty" id="empty" hidden>当前筛选条件下没有候选课程。</div>
    </div>

    <section class="method">
      <div><strong>排序口径</strong><br>GPA 占权重最高，并使用 30 个先验样本向 4.0 收缩；样本量、老师评分、低点名率、评论中的“事少/给分好/无考试”会加分，“作业多/考试难/卡绩/签到”会扣分。</div>
      <div><strong>使用边界</strong><br>GPA 是课程维度；老师评分、点名率和多数评论是老师维度，可能混合多门课程。选课前仍需核对当学期开课老师、考核方式、容量和培养方案限制。</div>
    </section>
  </main>
  <script>
    const DATA = {safe_json};
    const $ = id => document.getElementById(id);
    const controls = ["search", "category", "min-gpa", "min-sample", "max-checkin", "sort"];

    function esc(value) {{
      return String(value ?? "").replace(/[&<>"']/g, char => ({{
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }})[char]);
    }}

    function render() {{
      const query = $("search").value.trim().toLowerCase();
      const category = $("category").value;
      const minGpa = Number($("min-gpa").value);
      const minSample = Number($("min-sample").value);
      const maxCheckin = Number($("max-checkin").value);
      const sort = $("sort").value;

      let rows = DATA.filter(item => {{
        const haystack = `${{item.course}} ${{item.teacher}} ${{item.college}}`.toLowerCase();
        const checkin = item.checkin_ratio == null ? 101 : item.checkin_ratio;
        return (!query || haystack.includes(query))
          && (!category || item.category === category)
          && item.avg_gpa >= minGpa
          && item.sample_count >= minSample
          && checkin <= maxCheckin;
      }});

      rows.sort((a, b) => {{
        if (sort === "gpa") return b.avg_gpa - a.avg_gpa || b.sample_count - a.sample_count;
        if (sort === "checkin") return (a.checkin_ratio ?? 101) - (b.checkin_ratio ?? 101);
        if (sort === "sample") return b.sample_count - a.sample_count;
        if (sort === "easy") return (b.course_easy + b.teacher_easy * .08) - (a.course_easy + a.teacher_easy * .08);
        return b.score - a.score;
      }});

      $("result-count").textContent = `显示 ${{rows.length}} / ${{DATA.length}} 门`;
      $("empty").hidden = rows.length > 0;
      $("rows").innerHTML = rows.map((item, index) => {{
        const categoryClass = item.category === "软件相关" ? "tag-pro" :
          item.category === "跨专业低风险" ? "tag-cross" : "tag-risk";
        const risk = item.risk > 0
          ? `<span class="tag ${{item.risk >= 4 ? "tag-risk" : "tag-warn"}}">风险 ${{item.risk}}</span>`
          : "";
        const snippets = item.snippets.length
          ? item.snippets.map(s => `<blockquote>${{esc(s.content)}}<br><small>${{esc(s.date)}}</small></blockquote>`).join("")
          : "<blockquote>没有命中明确的事少/难度关键词。</blockquote>";
        return `<tr>
          <td class="rank">${{String(index + 1).padStart(2, "0")}}</td>
          <td>
            <a class="course-name" href="${{esc(item.url)}}" target="_blank" rel="noopener">${{esc(item.course)}}</a>
            <div class="teacher-line">${{esc(item.teacher)}} · ${{esc(item.college)}}</div>
          </td>
          <td><span class="tag ${{categoryClass}}">${{esc(item.category)}}</span> ${{risk}}</td>
          <td class="num"><strong>${{item.score.toFixed(1)}}</strong></td>
          <td class="num"><span class="gpa">${{item.avg_gpa.toFixed(2)}}</span><br><small>校正 ${{item.adjusted_gpa.toFixed(2)}}</small></td>
          <td class="num">${{esc(item.sample_label)}}</td>
          <td class="num">${{item.teacher_score == null ? "未知" : item.teacher_score.toFixed(2)}}<br><small>${{item.rating_count}} 人</small></td>
          <td class="num">${{item.checkin_ratio == null ? "未知" : item.checkin_ratio.toFixed(1) + "%"}}</td>
          <td class="signal">利好 ${{item.course_easy.toFixed(1)}} / 风险 ${{item.course_hard.toFixed(1)}}<br><small>${{esc(item.evidence_scope)}}</small></td>
          <td><details><summary>查看评论证据</summary>${{snippets}}</details></td>
        </tr>`;
      }}).join("");
    }}
    controls.forEach(id => $(id).addEventListener(id === "search" ? "input" : "change", render));
    render();
  </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="chalaoshi_public_data.sqlite")
    parser.add_argument("--html", default="course_recommendations.html")
    parser.add_argument("--csv", default="course_recommendations.csv")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        all_rows = build_recommendations(conn)
        software_rows = [
            row for row in all_rows if row["category"] == "软件相关"
        ][:300]
        cross_rows = [
            row for row in all_rows if row["category"] == "跨专业低风险"
        ][:300]
        rows = software_rows + cross_rows
        stats = {
            "teachers": conn.execute("SELECT COUNT(*) FROM teachers").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
            "courses": conn.execute("SELECT COUNT(*) FROM gpa_courses").fetchone()[0],
        }
    finally:
        conn.close()

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    Path(args.html).write_text(
        render_html(rows, stats, generated_at),
        encoding="utf-8",
    )
    export_csv(rows, Path(args.csv))

    software_count = sum(row["category"] == "软件相关" for row in rows)
    cross_count = sum(row["category"] == "跨专业低风险" for row in rows)
    print(f"generated {args.html} with {len(rows)} candidates")
    print(f"software_related={software_count} cross_major_low_risk={cross_count}")


if __name__ == "__main__":
    main()
