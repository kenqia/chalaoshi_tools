import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from tqdm import tqdm


BASE = "https://chalaoshi.de"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; educational-research-crawler/1.0; "
        "low-rate; public-pages-only)"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

DETAIL_URL = BASE + "/t/{tid}/"
KEFOU_URL = BASE + "/kefou"
CURRENT_API_COMMENT_URL = "https://api.chalaoshi.de/comments/{tid}"

API_COMMENT_CANDIDATES = [
    BASE + "/teacher/{tid}/comment_list",
    BASE + "/teacher/{tid}/comment_list/",
    BASE + "/t/{tid}/comment_list",
    BASE + "/t/{tid}/comment_list/",
    BASE + "/api/teacher/{tid}/comments",
    BASE + "/api/t/{tid}/comments",
]

NOISE_LINES = {
    "查老师",
    "评分",
    "修改评分",
    "确定",
    "Loading...",
    "Loading",
    "人气评论",
    "最新评论",
    "最后一条评论了喔::>_<::",
    "还没有人点评，快来点击下面的评分吧~",
    "本数据由 课否 提供，仅供参考",
    "本数据由  课否  提供，仅供参考",
    "课否",
    "提供，仅供参考",
    "平均绩点",
    "关注课否公众号，发送老师名称",
    "排序：长度 最新",
    "排序：长度最新",
    "排序： 最新 长度",
    "排序：",
}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def norm_space(s: Optional[str]) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def content_hash(
    teacher_id: int,
    content: str,
    date: str,
    source_comment_id: Optional[str] = None,
) -> str:
    if source_comment_id:
        raw = f"{teacher_id}|source_comment_id|{norm_space(source_comment_id)}"
    else:
        raw = f"{teacher_id}|{norm_space(content)}|{norm_space(date)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def snapshot_hash(data: Dict) -> str:
    raw = "|".join([
        str(data.get("teacher_id") or ""),
        norm_space(data.get("score")),
        str(data.get("rating_count") or ""),
        str(data.get("comment_count") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_comment_date(date: Optional[str]) -> str:
    date = norm_space(date)
    m = re.match(r"^(\d{4})[.-](\d{1,2})[.-](\d{1,2})$", date)
    if not m:
        return date
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_first_int(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"\d+", str(s))
    if not m:
        return None
    return int(m.group(0))


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
):
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def init_db_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS teachers (
        teacher_id INTEGER PRIMARY KEY,
        url TEXT,
        name TEXT,
        school TEXT,
        college TEXT,
        checkin_ratio TEXT,
        score TEXT,
        rating_count INTEGER,
        comment_count INTEGER,
        raw_text TEXT,
        crawled_at TEXT
    )
    """)
    ensure_column(conn, "teachers", "crawled_at", "TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS teacher_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER,
        score TEXT,
        rating_count INTEGER,
        comment_count INTEGER,
        snapshot_hash TEXT UNIQUE,
        crawled_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS gpa_courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER,
        course_name TEXT,
        avg_gpa TEXT,
        sample_count TEXT,
        crawled_at TEXT,
        UNIQUE(teacher_id, course_name, avg_gpa, sample_count)
    )
    """)
    ensure_column(conn, "gpa_courses", "crawled_at", "TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER,
        source_comment_id TEXT,
        content TEXT,
        heat_score TEXT,
        comment_date TEXT,
        source TEXT,
        content_hash TEXT UNIQUE,
        crawled_at TEXT
    )
    """)
    ensure_column(conn, "comments", "source_comment_id", "TEXT")
    ensure_column(conn, "comments", "heat_score", "TEXT")
    ensure_column(conn, "comments", "comment_date", "TEXT")
    ensure_column(conn, "comments", "source", "TEXT")
    ensure_column(conn, "comments", "content_hash", "TEXT")
    ensure_column(conn, "comments", "crawled_at", "TEXT")
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_content_hash
    ON comments(content_hash)
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS crawl_status (
        teacher_id INTEGER PRIMARY KEY,
        teacher_status TEXT,
        comment_status TEXT,
        http_status INTEGER,
        detail_error TEXT,
        comment_error TEXT,
        updated_at TEXT
    )
    """)

    conn.commit()
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    return init_db_connection(conn)


def update_status(
    conn: sqlite3.Connection,
    teacher_id: int,
    teacher_status: Optional[str] = None,
    comment_status: Optional[str] = None,
    http_status: Optional[int] = None,
    detail_error: Optional[str] = None,
    comment_error: Optional[str] = None,
):
    old = conn.execute(
        "SELECT teacher_status, comment_status, http_status, detail_error, comment_error "
        "FROM crawl_status WHERE teacher_id = ?",
        (teacher_id,),
    ).fetchone()

    if old:
        teacher_status = teacher_status if teacher_status is not None else old[0]
        comment_status = comment_status if comment_status is not None else old[1]
        http_status = http_status if http_status is not None else old[2]
        detail_error = detail_error if detail_error is not None else old[3]
        comment_error = comment_error if comment_error is not None else old[4]

    conn.execute("""
    INSERT OR REPLACE INTO crawl_status
    (teacher_id, teacher_status, comment_status, http_status, detail_error, comment_error, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        teacher_id,
        teacher_status,
        comment_status,
        http_status,
        detail_error,
        comment_error,
        now_str(),
    ))
    conn.commit()


def get_existing_teacher(conn: sqlite3.Connection, teacher_id: int) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM teachers WHERE teacher_id = ?",
        (teacher_id,),
    ).fetchone()
    conn.row_factory = None
    return row


def get_comment_db_count(conn: sqlite3.Connection, teacher_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE teacher_id = ?",
        (teacher_id,),
    ).fetchone()
    return int(row[0])


def needs_comment_refresh(
    expected_count: Optional[int],
    existing_count: int,
    force_comments: bool,
) -> bool:
    if force_comments:
        return True
    if expected_count is None or expected_count <= 0:
        return False
    return existing_count < expected_count


def save_teacher(conn: sqlite3.Connection, data: Dict):
    conn.execute("""
    INSERT OR REPLACE INTO teachers
    (teacher_id, url, name, school, college, checkin_ratio, score,
     rating_count, comment_count, raw_text, crawled_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["teacher_id"],
        data["url"],
        data["name"],
        data["school"],
        data["college"],
        data["checkin_ratio"],
        data["score"],
        data["rating_count"],
        data["comment_count"],
        data["raw_text"],
        now_str(),
    ))
    conn.commit()
    save_teacher_snapshot(conn, data)


def save_teacher_snapshot(conn: sqlite3.Connection, data: Dict) -> int:
    h = snapshot_hash(data)
    cur = conn.execute("""
    INSERT OR IGNORE INTO teacher_snapshots
    (teacher_id, score, rating_count, comment_count, snapshot_hash, crawled_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data["teacher_id"],
        data.get("score"),
        data.get("rating_count"),
        data.get("comment_count"),
        h,
        now_str(),
    ))
    conn.commit()
    return cur.rowcount


def save_gpa_courses(conn: sqlite3.Connection, teacher_id: int, courses: List[Dict]):
    for c in courses:
        conn.execute("""
        INSERT OR IGNORE INTO gpa_courses
        (teacher_id, course_name, avg_gpa, sample_count, crawled_at)
        VALUES (?, ?, ?, ?, ?)
        """, (
            teacher_id,
            c["course_name"],
            c["avg_gpa"],
            c["sample_count"],
            now_str(),
        ))
    conn.commit()


def save_comments(
    conn: sqlite3.Connection,
    teacher_id: int,
    comments: List[Dict],
    source: str,
) -> int:
    inserted = 0

    for c in comments:
        content = norm_space(c.get("content"))
        date = norm_space(c.get("date"))
        date = normalize_comment_date(date)
        heat_score = norm_space(c.get("heat_score"))
        source_comment_id = norm_space(c.get("comment_id") or c.get("source_comment_id"))
        if not content and not source_comment_id:
            continue

        h = content_hash(teacher_id, content, date, source_comment_id)

        cur = conn.execute("""
        INSERT OR IGNORE INTO comments
        (teacher_id, source_comment_id, content, heat_score, comment_date, source, content_hash, crawled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            teacher_id,
            source_comment_id,
            content,
            heat_score,
            date,
            source,
            h,
            now_str(),
        ))

        if cur.rowcount > 0:
            inserted += 1

    conn.commit()
    return inserted


def comment_already_seen(
    conn: sqlite3.Connection,
    teacher_id: int,
    comment: Dict,
) -> bool:
    content = norm_space(comment.get("content"))
    date = normalize_comment_date(comment.get("date"))
    source_comment_id = norm_space(comment.get("comment_id") or comment.get("source_comment_id"))
    if not content and not source_comment_id:
        return False

    h = content_hash(teacher_id, content, date, source_comment_id)
    row = conn.execute(
        "SELECT 1 FROM comments WHERE content_hash = ? LIMIT 1",
        (h,),
    ).fetchone()
    return row is not None


def request_get(
    session: requests.Session,
    url: str,
    params: Optional[dict] = None,
    timeout: int = 20,
    retry: int = 3,
    sleep: float = 1.5,
) -> Optional[requests.Response]:
    last_exc = None

    for i in range(retry):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=timeout)
            time.sleep(sleep + random.uniform(0, 0.4))
            return r
        except requests.RequestException as e:
            last_exc = e
            time.sleep((i + 1) * sleep)

    print(f"[WARN] request failed: {url} {last_exc}")
    return None


def parse_teacher_detail(html: str, teacher_id: int, url: str) -> Tuple[Optional[Dict], List[Dict]]:
    soup = BeautifulSoup(html, "lxml")
    raw_text = soup.get_text("\n", strip=True)
    lines = [norm_space(x) for x in raw_text.splitlines() if norm_space(x)]

    name = None
    h3 = soup.find("h3")
    if h3:
        name = norm_space(h3.get_text(" ", strip=True))

    if not name:
        title = soup.find("title")
        if title:
            m = re.search(r"(.+?)老师", title.get_text(" ", strip=True))
            if m:
                name = norm_space(m.group(1))

    score = None
    h2 = soup.find("h2")
    if h2:
        score = norm_space(h2.get_text(" ", strip=True))

    if not score:
        m = re.search(r"\n\s*(\d+(?:\.\d+)?)\s*\n\s*\d+人参与评分", "\n" + raw_text)
        if m:
            score = m.group(1)

    rating_count = None
    m = re.search(r"(\d+)\s*人参与评分", raw_text)
    if m:
        rating_count = int(m.group(1))

    comment_count = None
    m = re.search(r"(\d+)\s*个评论", raw_text)
    if m:
        comment_count = int(m.group(1))

    checkin_ratio = None
    m = re.search(r"([\d.]+)%的人认为该老师会点名", raw_text)
    if m:
        checkin_ratio = m.group(1)

    school = None
    college = None

    for i, line in enumerate(lines):
        if line == "浙江大学":
            school = line
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                if "点名" not in nxt and "评分" not in nxt:
                    college = nxt
            break

    data = {
        "teacher_id": teacher_id,
        "url": url,
        "name": name,
        "school": school,
        "college": college,
        "checkin_ratio": checkin_ratio,
        "score": score,
        "rating_count": rating_count,
        "comment_count": comment_count,
        "raw_text": raw_text[:20000],
    }

    # GPA 解析：平均绩点后，课程名一行 + 绩点/样本量一行
    courses = []
    try:
        start = lines.index("平均绩点")
        i = start + 1
        while i + 1 < len(lines):
            if "本数据由" in lines[i] or "个评论" in lines[i]:
                break

            course_name = lines[i]
            stat = lines[i + 1]

            m = re.match(r"^(\d+(?:\.\d+)?)/(.*)$", stat)
            if m:
                courses.append({
                    "course_name": course_name,
                    "avg_gpa": m.group(1),
                    "sample_count": m.group(2),
                })
                i += 2
            else:
                i += 1
    except ValueError:
        pass

    # 有些无评分页也有老师名；这里以 name 为有效页核心判断
    if not name:
        return None, []

    return data, courses


def fetch_teacher_detail_data(
    session: requests.Session,
    teacher_id: int,
    sleep: float,
) -> Dict:
    url = DETAIL_URL.format(tid=teacher_id)
    r = request_get(session, url, sleep=sleep)

    if r is None:
        return {
            "teacher_id": teacher_id,
            "valid": False,
            "teacher_status": "error",
            "detail_error": "request failed",
        }

    if r.status_code == 404:
        return {
            "teacher_id": teacher_id,
            "valid": False,
            "teacher_status": "404",
            "http_status": 404,
        }

    if r.status_code != 200:
        return {
            "teacher_id": teacher_id,
            "valid": False,
            "teacher_status": "error",
            "http_status": r.status_code,
            "detail_error": f"http {r.status_code}",
        }

    data, courses = parse_teacher_detail(r.text, teacher_id, url)

    if not data:
        return {
            "teacher_id": teacher_id,
            "valid": False,
            "teacher_status": "invalid",
            "http_status": r.status_code,
            "detail_error": "no teacher name parsed",
        }

    return {
        "teacher_id": teacher_id,
        "valid": True,
        "data": data,
        "courses": courses,
        "teacher_status": "ok",
        "http_status": 200,
        "detail_error": "",
    }


def fetch_teacher_detail(
    session: requests.Session,
    conn: sqlite3.Connection,
    teacher_id: int,
    sleep: float,
) -> Tuple[bool, Optional[Dict]]:
    result = fetch_teacher_detail_data(session, teacher_id, sleep)

    if not result["valid"]:
        update_status(
            conn,
            teacher_id,
            teacher_status=result.get("teacher_status"),
            http_status=result.get("http_status"),
            detail_error=result.get("detail_error"),
        )
        return False, None

    data = result["data"]
    courses = result["courses"]

    save_teacher(conn, data)
    save_gpa_courses(conn, teacher_id, courses)
    update_status(conn, teacher_id, teacher_status="ok", http_status=200, detail_error="")

    return True, data


def clean_comment_buffer(buf: List[str]) -> List[str]:
    cleaned = []

    for line in buf:
        line = norm_space(line)
        if not line:
            continue

        if line in NOISE_LINES:
            continue

        if line == "* * *":
            continue

        if re.fullmatch(r"#+", line):
            continue

        if re.search(r"\d+人参与评分", line):
            continue

        if re.search(r"\d+个评论", line):
            continue

        if re.search(r"[\d.]+%的人认为该老师会点名", line):
            continue

        if line == "浙江大学":
            continue

        if re.fullmatch(r"\d+(?:\.\d+)?", line):
            # 可能是教师总评分，也可能是热度分。先保留，后面会作为 heat_score 判断。
            cleaned.append(line)
            continue

        cleaned.append(line)

    return cleaned


def parse_comments_from_text(
    text: str,
    from_full_page: bool = False,
) -> List[Dict]:
    """
    解析形如：
    评论内容
    12
    发布于 2024.06.01 举报
    * * *

    from_full_page=True 时，会尽量跳过教师详情头部。
    """
    text = text.replace("\xa0", " ")
    raw_lines = [norm_space(x) for x in text.splitlines() if norm_space(x)]

    comments = []
    buf = []
    started = not from_full_page

    for line in raw_lines:
        if from_full_page and ("人气评论" in line or "最新评论" in line):
            started = True
            buf = []
            continue

        if not started:
            continue

        # 遇到发布日期，认为一个评论块结束
        m = re.search(r"发布于\s*(\d{4}[.-]\d{1,2}[.-]\d{1,2})", line)
        if m:
            date = normalize_comment_date(m.group(1))
            cleaned = clean_comment_buffer(buf)

            heat_score = None
            content_lines = cleaned

            if cleaned:
                last = cleaned[-1]
                if re.fullmatch(r"[+-]?\d+", last) or re.fullmatch(r"\d+(?:\.\d+)?", last):
                    heat_score = last
                    content_lines = cleaned[:-1]

            content = "\n".join(content_lines).strip()

            # 再做一次明显噪声过滤
            bad_content = (
                not content
                or "平均绩点" in content
                or "参与评分" in content
                or "该老师会点名" in content
                or content.startswith("###")
            )

            if not bad_content:
                comments.append({
                    "content": content,
                    "heat_score": heat_score,
                    "date": date,
                })

            buf = []
            continue

        # 分隔线通常是评论之间的边界；不清空，因为发布日期才是真边界
        buf.append(line)

    # 去重
    seen = set()
    result = []
    for c in comments:
        key = (norm_space(c["content"]), norm_space(c["date"]))
        if key not in seen:
            seen.add(key)
            result.append(c)

    return result


def parse_comments_from_html(
    html: str,
    from_full_page: bool = False,
) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")

    # 先尝试按旧模板/常见块解析
    text = soup.get_text("\n", strip=True)
    return parse_comments_from_text(text, from_full_page=from_full_page)


def parse_api_comments_from_html(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    comments = []

    for block in soup.find_all("div", id="comment-page"):
        left = block.select_one(".left p")
        footer = block.select_one(".comment-footer")

        if not left or not footer:
            continue

        content = norm_space(left.get_text("\n", strip=True))
        footer_text = footer.get_text(" ", strip=True)
        date_match = re.search(r"发布于\s*(\d{4}[.-]\d{1,2}[.-]\d{1,2})", footer_text)

        comment_id = ""
        like_node = block.select_one(".up[id^='like_']")
        if like_node and like_node.get("id"):
            m = re.search(r"like_(\d+)", like_node.get("id", ""))
            if m:
                comment_id = m.group(1)

        if not comment_id:
            report_node = block.select_one(".comment-footer a[onclick*='reportComment']")
            if report_node and report_node.get("onclick"):
                m = re.search(r"reportComment\((\d+)\)", report_node.get("onclick", ""))
                if m:
                    comment_id = m.group(1)

        if not date_match or (not content and not comment_id):
            continue

        heat_score = ""
        score_node = block.select_one(".right p")
        if score_node:
            heat_score = norm_space(score_node.get_text(" ", strip=True)).replace(" ", "")

        comment = {
            "content": content,
            "heat_score": heat_score,
            "date": normalize_comment_date(date_match.group(1)),
        }
        if comment_id:
            comment["comment_id"] = comment_id
        comments.append(comment)

    seen = set()
    result = []
    for comment in comments:
        key = (
            norm_space(comment.get("comment_id")),
            norm_space(comment["content"]),
            norm_space(comment["date"]),
        )
        if key not in seen:
            seen.add(key)
            result.append(comment)

    return result


def parse_kefou_comments_from_html(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    raw_lines = [norm_space(x) for x in text.splitlines() if norm_space(x)]

    lines = []
    for line in raw_lines:
        if line in NOISE_LINES:
            continue
        if re.fullmatch(r"\d+\s*个评论", line):
            continue
        if line.startswith("排序："):
            continue
        lines.append(line)

    comments = parse_comments_from_text("\n".join(lines), from_full_page=False)
    for comment in comments:
        if comment.get("heat_score") is None:
            comment["heat_score"] = ""
    return comments


def save_incremental_comments(
    conn: sqlite3.Connection,
    teacher_id: int,
    comments: List[Dict],
    source: str,
    stop_after_seen: int = 20,
) -> Tuple[int, int, bool]:
    inserted = 0
    seen_in_a_row = 0
    stopped_early = False

    for comment in comments:
        if comment_already_seen(conn, teacher_id, comment):
            seen_in_a_row += 1
            if stop_after_seen > 0 and seen_in_a_row >= stop_after_seen:
                stopped_early = True
                break
            continue

        seen_in_a_row = 0
        inserted += save_comments(conn, teacher_id, [comment], source=source)

    return inserted, seen_in_a_row, stopped_early


def fetch_comments_by_kefou(
    session: requests.Session,
    conn: sqlite3.Connection,
    teacher_id: int,
    teacher_name: str,
    sleep: float,
    stop_after_seen: int = 20,
) -> int:
    if not teacher_name:
        return 0

    params = {"name": teacher_name, "sort": "time"}
    url = f"{KEFOU_URL}?{urlencode(params)}"
    r = request_get(
        session,
        url,
        sleep=sleep,
        timeout=25,
        retry=3,
    )

    if r is None:
        update_status(conn, teacher_id, comment_error="kefou request failed")
        return 0

    if r.status_code != 200:
        update_status(conn, teacher_id, comment_error=f"kefou http {r.status_code}")
        return 0

    comments = parse_kefou_comments_from_html(r.text)
    inserted, _, _ = save_incremental_comments(
        conn,
        teacher_id,
        comments,
        source="kefou:time",
        stop_after_seen=stop_after_seen,
    )
    return inserted


def fetch_current_api_comments_data(
    session: requests.Session,
    teacher_id: int,
    sleep: float,
) -> Tuple[List[Dict], str]:
    current_api = CURRENT_API_COMMENT_URL.format(tid=teacher_id)
    r = request_get(
        session,
        current_api,
        params={"sort": "time"},
        sleep=sleep,
        timeout=25,
        retry=3,
    )

    if r is None:
        return [], "api request failed"
    if r.status_code != 200:
        return [], f"api http {r.status_code}"
    if not r.text.strip():
        return [], ""
    return parse_api_comments_from_html(r.text), ""


def fetch_kefou_comments_data(
    session: requests.Session,
    teacher_id: int,
    teacher_name: str,
    sleep: float,
) -> Tuple[List[Dict], str]:
    if not teacher_name:
        return [], ""

    params = {"name": teacher_name, "sort": "time"}
    url = f"{KEFOU_URL}?{urlencode(params)}"
    r = request_get(
        session,
        url,
        sleep=sleep,
        timeout=25,
        retry=3,
    )

    if r is None:
        return [], "kefou request failed"
    if r.status_code != 200:
        return [], f"kefou http {r.status_code}"
    return parse_kefou_comments_from_html(r.text), ""


def crawl_teacher_remote(
    teacher_id: int,
    existing_comment_count: int,
    force_comments: bool,
    sleep: float,
) -> Dict:
    session = requests.Session()
    result = fetch_teacher_detail_data(session, teacher_id, sleep)

    if not result["valid"]:
        result["comments_by_source"] = []
        result["comment_error"] = ""
        return result

    data = result["data"]
    expected_count = parse_first_int(data.get("comment_count"))
    comments_by_source = []
    comment_errors = []

    if needs_comment_refresh(expected_count, existing_comment_count, force_comments):
        api_comments, api_error = fetch_current_api_comments_data(
            session=session,
            teacher_id=teacher_id,
            sleep=sleep,
        )
        if api_comments:
            comments_by_source.append((
                f"api:{CURRENT_API_COMMENT_URL.format(tid=teacher_id)}",
                api_comments,
            ))
        elif api_error:
            comment_errors.append(api_error)

        projected_count = existing_comment_count + len(api_comments)
        if expected_count and projected_count < expected_count:
            kefou_comments, kefou_error = fetch_kefou_comments_data(
                session=session,
                teacher_id=teacher_id,
                teacher_name=data.get("name") or "",
                sleep=sleep,
            )
            if kefou_comments:
                comments_by_source.append(("kefou:time", kefou_comments))
            elif kefou_error:
                comment_errors.append(kefou_error)

    result["comments_by_source"] = comments_by_source
    result["comment_error"] = "; ".join(comment_errors)
    return result


def fetch_comments_by_api(
    session: requests.Session,
    conn: sqlite3.Connection,
    teacher_id: int,
    expected_count: Optional[int],
    sleep: float,
    max_pages: int = 300,
) -> int:
    total_inserted = 0

    current_api = CURRENT_API_COMMENT_URL.format(tid=teacher_id)
    r = request_get(
        session,
        current_api,
        params={"sort": "time"},
        sleep=sleep,
        timeout=25,
        retry=3,
    )

    if r is not None and r.status_code == 200 and r.text.strip():
        comments = parse_api_comments_from_html(r.text)
        inserted, _, _ = save_incremental_comments(
            conn,
            teacher_id,
            comments,
            source=f"api:{current_api}",
            stop_after_seen=20,
        )
        total_inserted += inserted

        if comments:
            return total_inserted

    for endpoint_template in API_COMMENT_CANDIDATES:
        endpoint = endpoint_template.format(tid=teacher_id)
        found_any_for_endpoint = False

        # 有的站 page 从 0 开始，有的从 1 开始，两个都试
        for first_page in [0, 1]:
            empty_round = 0

            for page_no in range(first_page, first_page + max_pages):
                r = request_get(
                    session,
                    endpoint,
                    params={"page": page_no, "order_by": "time"},
                    sleep=sleep,
                    timeout=20,
                    retry=2,
                )

                if r is None:
                    break

                if r.status_code in (404, 403, 405):
                    break

                if r.status_code != 200:
                    break

                html = r.text.strip()
                if not html:
                    empty_round += 1
                    if empty_round >= 2:
                        break
                    continue

                comments = parse_comments_from_html(html, from_full_page=False)

                if not comments:
                    empty_round += 1
                    if empty_round >= 2:
                        break
                    continue

                found_any_for_endpoint = True
                inserted = save_comments(conn, teacher_id, comments, source=f"api:{endpoint}")
                total_inserted += inserted

                if expected_count and get_comment_db_count(conn, teacher_id) >= expected_count:
                    return total_inserted

            if found_any_for_endpoint:
                return total_inserted

    return total_inserted


class BrowserCommentFetcher:
    def __init__(self, headful: bool = False, slow_mo: int = 0):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=not headful,
            slow_mo=slow_mo,
        )
        self.context = self.browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="zh-CN",
            viewport={"width": 1280, "height": 1600},
        )

    def close(self):
        try:
            self.context.close()
            self.browser.close()
            self.playwright.stop()
        except Exception:
            pass

    def fetch(
        self,
        teacher_id: int,
        expected_count: Optional[int],
        max_scroll_rounds: int = 260,
        debug_dir: str = "debug_comments",
    ) -> List[Dict]:
        url = DETAIL_URL.format(tid=teacher_id)
        page = self.context.new_page()

        captured_comments: List[Dict] = []

        def on_response(response):
            nonlocal captured_comments
            try:
                ct = response.headers.get("content-type", "")
                u = response.url

                if not any(x in u.lower() for x in ["comment", "teacher", "/t/"]):
                    return

                if not any(x in ct for x in ["text", "html", "json", "javascript"]):
                    return

                body = response.text()
                if "发布于" not in body:
                    return

                comments = parse_comments_from_html(body, from_full_page=False)
                if comments:
                    captured_comments.extend(comments)
            except Exception:
                pass

        page.on("response", on_response)

        def route_handler(route):
            rt = route.request.resource_type
            if rt in {"image", "media", "font"}:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", route_handler)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(1500)

            # 尝试点“最新评论”
            for selector in [
                "text=最新评论",
                "a:has-text('最新评论')",
                "button:has-text('最新评论')",
            ]:
                try:
                    loc = page.locator(selector).first
                    if loc.count() > 0:
                        loc.click(timeout=1500)
                        page.wait_for_timeout(1200)
                        break
                except Exception:
                    pass

            # 如果页面里存在旧版 loadMoreComments 函数，主动调用几次
            try:
                page.evaluate(
                    """
                    async ({tid}) => {
                        if (typeof window.loadMoreComments === 'function') {
                            for (let i = 0; i < 5; i++) {
                                try {
                                    window.loadMoreComments(String(tid), 'time');
                                } catch (e) {}
                                await new Promise(r => setTimeout(r, 800));
                            }
                        }
                    }
                    """,
                    {"tid": teacher_id},
                )
            except Exception:
                pass

            all_comments: List[Dict] = []
            stable_rounds = 0
            last_count = 0

            if expected_count:
                dynamic_max_rounds = min(max_scroll_rounds, max(40, expected_count // 8 + 30))
            else:
                dynamic_max_rounds = 80

            for _ in range(dynamic_max_rounds):
                # 解析页面当前可见/已渲染文本
                try:
                    body_text = page.locator("body").inner_text(timeout=5000)
                    page_comments = parse_comments_from_text(body_text, from_full_page=True)
                    all_comments.extend(page_comments)
                except Exception:
                    pass

                # 合并网络响应捕捉到的评论
                all_comments.extend(captured_comments)

                # 去重
                dedup = {}
                for c in all_comments:
                    key = (norm_space(c.get("content")), norm_space(c.get("date")))
                    if key[0] and key not in dedup:
                        dedup[key] = c
                all_comments = list(dedup.values())

                cur_count = len(all_comments)

                if expected_count and cur_count >= expected_count:
                    break

                if cur_count == last_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    last_count = cur_count

                if stable_rounds >= 15:
                    break

                # 滚动触发懒加载
                try:
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(900)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1200)
                except Exception:
                    pass

            if expected_count and len(all_comments) == 0:
                os.makedirs(debug_dir, exist_ok=True)
                try:
                    html = page.content()
                    with open(
                        os.path.join(debug_dir, f"teacher_{teacher_id}.html"),
                        "w",
                        encoding="utf-8",
                    ) as f:
                        f.write(html)

                    body_text = page.locator("body").inner_text(timeout=3000)
                    with open(
                        os.path.join(debug_dir, f"teacher_{teacher_id}.txt"),
                        "w",
                        encoding="utf-8",
                    ) as f:
                        f.write(body_text)
                except Exception:
                    pass

            return all_comments

        finally:
            try:
                page.close()
            except Exception:
                pass


def crawl_comments(
    session: requests.Session,
    conn: sqlite3.Connection,
    browser_fetcher: Optional[BrowserCommentFetcher],
    teacher_id: int,
    teacher_name: str,
    expected_count: Optional[int],
    sleep: float,
    force_comments: bool = False,
) -> int:
    existing = get_comment_db_count(conn, teacher_id)

    if not force_comments and expected_count and existing >= expected_count:
        update_status(conn, teacher_id, comment_status="ok", comment_error="")
        return 0

    inserted_total = 0

    # 1. 当前页面用 api.chalaoshi.de 承载完整的查老师评论。
    try:
        inserted_api = fetch_comments_by_api(
            session=session,
            conn=conn,
            teacher_id=teacher_id,
            expected_count=expected_count,
            sleep=sleep,
        )
        inserted_total += inserted_api
    except Exception as e:
        update_status(conn, teacher_id, comment_error=f"api error: {repr(e)}")

    current_count = get_comment_db_count(conn, teacher_id)

    if expected_count and current_count >= expected_count:
        update_status(conn, teacher_id, comment_status="ok", comment_error="")
    else:
        # 2. /kefou 是站内链接的课否补充评论，和查老师评论计数不是同一批。
        try:
            inserted_kefou = fetch_comments_by_kefou(
                session=session,
                conn=conn,
                teacher_id=teacher_id,
                teacher_name=teacher_name,
                sleep=sleep,
            )
            inserted_total += inserted_kefou
        except Exception as e:
            update_status(conn, teacher_id, comment_error=f"kefou error: {repr(e)}")

    current_count = get_comment_db_count(conn, teacher_id)

    if expected_count and current_count >= expected_count:
        update_status(conn, teacher_id, comment_status="ok", comment_error="")
        return inserted_total

    # 3. API 和 kefou 抓不到或不够，再用浏览器渲染兜底
    if browser_fetcher is not None and (expected_count is None or current_count < expected_count):
        try:
            comments = browser_fetcher.fetch(
                teacher_id=teacher_id,
                expected_count=expected_count,
            )
            inserted_browser = save_comments(
                conn,
                teacher_id,
                comments,
                source="browser_rendered",
            )
            inserted_total += inserted_browser
        except Exception as e:
            update_status(conn, teacher_id, comment_error=f"browser error: {repr(e)}")

    final_count = get_comment_db_count(conn, teacher_id)

    if expected_count is None:
        status = "ok"
    elif final_count >= expected_count:
        status = "ok"
    elif final_count > 0:
        status = "partial"
    else:
        status = "empty"

    update_status(
        conn,
        teacher_id,
        comment_status=status,
        comment_error="" if status in {"ok", "partial"} else "no comments parsed",
    )

    return inserted_total


def save_remote_crawl_result(conn: sqlite3.Connection, result: Dict) -> bool:
    teacher_id = result["teacher_id"]

    if not result.get("valid"):
        update_status(
            conn,
            teacher_id,
            teacher_status=result.get("teacher_status"),
            http_status=result.get("http_status"),
            detail_error=result.get("detail_error"),
        )
        return False

    data = result["data"]
    save_teacher(conn, data)
    save_gpa_courses(conn, teacher_id, result.get("courses", []))

    for source, comments in result.get("comments_by_source", []):
        save_comments(conn, teacher_id, comments, source=source)

    expected_count = parse_first_int(data.get("comment_count"))
    final_count = get_comment_db_count(conn, teacher_id)

    if expected_count is None or expected_count <= 0:
        comment_status = "ok"
    elif final_count >= expected_count:
        comment_status = "ok"
    elif final_count > 0:
        comment_status = "partial"
    else:
        comment_status = "empty"

    update_status(
        conn,
        teacher_id,
        teacher_status="ok",
        comment_status=comment_status,
        http_status=200,
        detail_error="",
        comment_error="" if comment_status in {"ok", "partial"} else result.get("comment_error", ""),
    )
    return True


def crawl_parallel(conn: sqlite3.Connection, args):
    if args.max_id is None:
        raise ValueError("--concurrency requires --max-id")

    ids = list(range(args.start_id, args.max_id + 1))
    processed = 0

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {}
        for tid in ids:
            existing_comment_count = get_comment_db_count(conn, tid)
            future = executor.submit(
                crawl_teacher_remote,
                tid,
                existing_comment_count,
                args.force_comments,
                args.sleep,
            )
            futures[future] = tid

        for future in tqdm(as_completed(futures), total=len(futures), desc="crawl teachers parallel"):
            tid = futures[future]
            try:
                result = future.result()
            except Exception as e:
                update_status(
                    conn,
                    tid,
                    teacher_status="error",
                    detail_error=f"parallel worker error: {repr(e)}",
                )
            else:
                save_remote_crawl_result(conn, result)

            processed += 1
            if processed % args.export_every == 0:
                export_csv(conn, args.out_dir)
                quality_report(conn)


def export_csv(conn: sqlite3.Connection, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)

    for table in ["teachers", "teacher_snapshots", "gpa_courses", "comments", "crawl_status"]:
        df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
        df.to_csv(
            os.path.join(out_dir, f"{table}.csv"),
            index=False,
            encoding="utf-8-sig",
        )


def quality_report(conn: sqlite3.Connection):
    teacher_count = conn.execute("SELECT COUNT(*) FROM teachers").fetchone()[0]
    snapshot_count = conn.execute("SELECT COUNT(*) FROM teacher_snapshots").fetchone()[0]
    comment_count = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    gpa_count = conn.execute("SELECT COUNT(*) FROM gpa_courses").fetchone()[0]

    empty_comment_teachers = conn.execute("""
        SELECT COUNT(*)
        FROM teachers t
        LEFT JOIN comments c ON t.teacher_id = c.teacher_id
        WHERE COALESCE(t.comment_count, 0) > 0
        GROUP BY t.teacher_id
        HAVING COUNT(c.id) = 0
    """).fetchall()

    partial_top = conn.execute("""
        SELECT
            t.teacher_id,
            t.name,
            t.comment_count AS page_count,
            COUNT(c.id) AS db_count,
            COALESCE(t.comment_count, 0) - COUNT(c.id) AS missing
        FROM teachers t
        LEFT JOIN comments c ON t.teacher_id = c.teacher_id
        GROUP BY t.teacher_id
        HAVING missing > 0
        ORDER BY missing DESC
        LIMIT 10
    """).fetchall()

    print("\n========== Crawl Quality Report ==========")
    print(f"teachers       : {teacher_count}")
    print(f"snapshots      : {snapshot_count}")
    print(f"gpa_courses    : {gpa_count}")
    print(f"comments       : {comment_count}")
    print(f"empty-comment teachers with expected comments: {len(empty_comment_teachers)}")

    if partial_top:
        print("\nTop missing comment examples:")
        for row in partial_top:
            print(row)

    print("==========================================\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="chalaoshi_public_data.sqlite")
    parser.add_argument("--out-dir", default="chalaoshi_export")
    parser.add_argument("--start-id", type=int, default=1)
    parser.add_argument("--max-id", type=int, default=None)
    parser.add_argument("--empty-gap", type=int, default=1200)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--export-every", type=int, default=200)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--force-detail", action="store_true")
    parser.add_argument("--reuse-detail", action="store_true")
    parser.add_argument("--force-comments", action="store_true")
    parser.add_argument("--skip-comments", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()

    conn = init_db(args.db)
    session = requests.Session()

    if args.concurrency > 1:
        if args.max_id is None:
            raise SystemExit("--concurrency requires --max-id")
        if args.skip_comments:
            raise SystemExit("--concurrency does not support --skip-comments yet")
        try:
            crawl_parallel(conn, args)
        except KeyboardInterrupt:
            print("\n[INTERRUPTED] exporting partial data...")
        finally:
            export_csv(conn, args.out_dir)
            quality_report(conn)
            conn.close()
        return

    browser_fetcher = None
    if not args.no_browser and not args.skip_comments:
        browser_fetcher = BrowserCommentFetcher(headful=args.headful)

    last_valid_id = args.start_id - 1
    tid = args.start_id
    processed = 0

    try:
        pbar = tqdm(total=None, desc="crawl teachers")

        while True:
            if args.max_id is not None and tid > args.max_id:
                break

            # 如果没有 max-id，用连续空洞停止
            if args.max_id is None and tid - last_valid_id > args.empty_gap:
                print(f"[STOP] no valid teacher for {args.empty_gap} consecutive ids after {last_valid_id}")
                break

            existing = get_existing_teacher(conn, tid)

            data = None
            valid = False

            if existing is not None and args.reuse_detail and not args.force_detail:
                valid = True
                data = dict(existing)
                last_valid_id = tid
            else:
                valid, data = fetch_teacher_detail(
                    session=session,
                    conn=conn,
                    teacher_id=tid,
                    sleep=args.sleep,
                )
                if valid:
                    last_valid_id = tid

            if valid and data:
                expected_count = parse_first_int(data.get("comment_count"))

                if not args.skip_comments and expected_count is not None and expected_count > 0:
                    crawl_comments(
                        session=session,
                        conn=conn,
                        browser_fetcher=browser_fetcher,
                        teacher_id=tid,
                        teacher_name=data.get("name") or "",
                        expected_count=expected_count,
                        sleep=args.sleep,
                        force_comments=args.force_comments,
                    )
                else:
                    update_status(conn, tid, comment_status="skipped" if args.skip_comments else "ok")

            processed += 1

            if processed % args.export_every == 0:
                export_csv(conn, args.out_dir)
                quality_report(conn)

            tid += 1
            pbar.update(1)

    except KeyboardInterrupt:
        print("\n[INTERRUPTED] exporting partial data...")
    finally:
        if browser_fetcher is not None:
            browser_fetcher.close()

        export_csv(conn, args.out_dir)
        quality_report(conn)
        conn.close()


if __name__ == "__main__":
    main()
