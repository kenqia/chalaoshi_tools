import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import crawl_chalaoshi_all as crawler


class KefouCommentParsingTest(unittest.TestCase):
    def test_parse_kefou_comments_from_html_extracts_content_and_date(self):
        html = """
        <html><body>
        <p>老师讲课认真，作业量合理。</p>
        <p>发布于&nbsp;2024-06-01</p>
        <hr>
        <p>给分很好，也会耐心答疑。</p>
        <p>发布于 2023-12-15</p>
        </body></html>
        """

        comments = crawler.parse_kefou_comments_from_html(html)

        self.assertEqual(
            comments,
            [
                {
                    "content": "老师讲课认真，作业量合理。",
                    "heat_score": "",
                    "date": "2024-06-01",
                },
                {
                    "content": "给分很好，也会耐心答疑。",
                    "heat_score": "",
                    "date": "2023-12-15",
                },
            ],
        )

    def test_parse_kefou_comments_from_html_removes_duplicates(self):
        html = """
        <html><body>
        老师很好
        发布于 2024-01-01
        * * *
        老师很好
        发布于 2024-01-01
        </body></html>
        """

        comments = crawler.parse_kefou_comments_from_html(html)

        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["content"], "老师很好")


class ApiCommentParsingTest(unittest.TestCase):
    def test_parse_api_comments_from_html_extracts_vote_count_and_date(self):
        html = """
        <div class="hidden" id="comment-page">
            <div class="row">
                <div class="left">
                    <p>老师讲课特别认真</p>
                </div>
                <div class="right">
                    <a class="up iconfont" id="like_123">&#xe601;</a>
                    <p class="123-count">&nbsp;-11&nbsp;</p>
                    <a class="down iconfont" id="dislike_123">&#xe601;</a>
                </div>
            </div>
            <p class="comment-footer">发布于&nbsp;2025.05.26&nbsp;&nbsp;<a>投诉</a></p>
            <hr>
        </div>
        """

        comments = crawler.parse_api_comments_from_html(html)

        self.assertEqual(
            comments,
            [
                {
                    "comment_id": "123",
                    "content": "老师讲课特别认真",
                    "heat_score": "-11",
                    "date": "2025-05-26",
                }
            ],
        )

    def test_parse_api_comments_from_html_preserves_blank_comment_with_id(self):
        html = """
        <div class="hidden" id="comment-page">
            <div class="row">
                <div class="left"><p> </p></div>
                <div class="right">
                    <a class="up iconfont" id="like_69054">&#xe601;</a>
                    <p class="69054-count">&nbsp;-1&nbsp;</p>
                </div>
            </div>
            <p class="comment-footer">发布于&nbsp;2018.01.24&nbsp;&nbsp;<a>投诉</a></p>
        </div>
        """

        comments = crawler.parse_api_comments_from_html(html)

        self.assertEqual(
            comments,
            [
                {
                    "comment_id": "69054",
                    "content": "",
                    "heat_score": "-1",
                    "date": "2018-01-24",
                }
            ],
        )


class IncrementalCommentTest(unittest.TestCase):
    def test_needs_comment_refresh_respects_existing_counts(self):
        self.assertFalse(crawler.needs_comment_refresh(10, 10, False))
        self.assertFalse(crawler.needs_comment_refresh(10, 12, False))
        self.assertTrue(crawler.needs_comment_refresh(10, 9, False))
        self.assertTrue(crawler.needs_comment_refresh(10, 10, True))
        self.assertFalse(crawler.needs_comment_refresh(0, 0, False))

    def test_comments_already_seen_detects_existing_hash(self):
        conn = sqlite3.connect(":memory:")
        crawler.init_db_connection(conn)
        try:
            crawler.save_comments(
                conn,
                teacher_id=1,
                comments=[
                    {
                        "content": "已经抓过的评论",
                        "heat_score": "",
                        "date": "2024-01-01",
                    }
                ],
                source="test",
            )

            self.assertTrue(
                crawler.comment_already_seen(
                    conn,
                    teacher_id=1,
                    comment={
                        "content": "已经抓过的评论",
                        "heat_score": "",
                        "date": "2024-01-01",
                    },
                )
            )
            self.assertFalse(
                crawler.comment_already_seen(
                    conn,
                    teacher_id=1,
                    comment={
                        "content": "新的评论",
                        "heat_score": "",
                        "date": "2024-01-02",
                    },
                )
            )
        finally:
            conn.close()

    def test_comments_with_different_api_ids_are_not_collapsed(self):
        conn = sqlite3.connect(":memory:")
        crawler.init_db_connection(conn)
        try:
            inserted = crawler.save_comments(
                conn,
                teacher_id=1,
                comments=[
                    {
                        "comment_id": "100",
                        "content": "同一句评论",
                        "heat_score": "1",
                        "date": "2024-01-01",
                    },
                    {
                        "comment_id": "101",
                        "content": "同一句评论",
                        "heat_score": "2",
                        "date": "2024-01-01",
                    },
                ],
                source="api:test",
            )

            self.assertEqual(inserted, 2)
            count = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
            self.assertEqual(count, 2)
        finally:
            conn.close()

    def test_short_comments_are_preserved(self):
        conn = sqlite3.connect(":memory:")
        crawler.init_db_connection(conn)
        try:
            inserted = crawler.save_comments(
                conn,
                teacher_id=1,
                comments=[
                    {
                        "comment_id": "102",
                        "content": "好",
                        "heat_score": "0",
                        "date": "2024-01-03",
                    }
                ],
                source="api:test",
            )

            self.assertEqual(inserted, 1)
            content = conn.execute("SELECT content FROM comments").fetchone()[0]
            self.assertEqual(content, "好")
        finally:
            conn.close()

    def test_blank_api_comment_with_id_is_preserved(self):
        conn = sqlite3.connect(":memory:")
        crawler.init_db_connection(conn)
        try:
            inserted = crawler.save_comments(
                conn,
                teacher_id=1,
                comments=[
                    {
                        "comment_id": "69054",
                        "content": "",
                        "heat_score": "-1",
                        "date": "2018-01-24",
                    }
                ],
                source="api:test",
            )

            self.assertEqual(inserted, 1)
            row = conn.execute(
                "SELECT source_comment_id, content FROM comments"
            ).fetchone()
            self.assertEqual(row, ("69054", ""))
        finally:
            conn.close()


class ScoreSnapshotTest(unittest.TestCase):
    def test_save_teacher_snapshot_only_adds_new_state(self):
        conn = sqlite3.connect(":memory:")
        crawler.init_db_connection(conn)
        try:
            data = {
                "teacher_id": 1,
                "score": "9.39",
                "rating_count": 417,
                "comment_count": 350,
            }

            self.assertEqual(crawler.save_teacher_snapshot(conn, data), 1)
            self.assertEqual(crawler.save_teacher_snapshot(conn, dict(data)), 0)

            changed = dict(data)
            changed["comment_count"] = 351
            self.assertEqual(crawler.save_teacher_snapshot(conn, changed), 1)

            count = conn.execute("SELECT COUNT(*) FROM teacher_snapshots").fetchone()[0]
            self.assertEqual(count, 2)
        finally:
            conn.close()


class LegacySchemaMigrationTest(unittest.TestCase):
    def test_init_db_connection_upgrades_legacy_tables(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE teachers (
                teacher_id INTEGER PRIMARY KEY,
                url TEXT,
                name TEXT,
                school TEXT,
                college TEXT,
                score TEXT,
                rating_count TEXT,
                checkin_ratio TEXT,
                comment_count TEXT,
                raw_text TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id INTEGER,
                order_by TEXT,
                page INTEGER,
                content TEXT,
                score TEXT,
                date TEXT
            )
        """)
        try:
            crawler.init_db_connection(conn)
            teacher_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(teachers)")
            }
            comment_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(comments)")
            }

            self.assertIn("crawled_at", teacher_columns)
            self.assertIn("content_hash", comment_columns)
            self.assertIn("comment_date", comment_columns)

            crawler.save_teacher(
                conn,
                {
                    "teacher_id": 1,
                    "url": "https://chalaoshi.de/t/1/",
                    "name": "毕惟红",
                    "school": "浙江大学",
                    "college": "数学科学学院",
                    "checkin_ratio": "30.9",
                    "score": "9.39",
                    "rating_count": 417,
                    "comment_count": 350,
                    "raw_text": "raw",
                },
            )
            inserted = crawler.save_comments(
                conn,
                teacher_id=1,
                comments=[
                    {
                        "comment_id": "1",
                        "content": "好",
                        "heat_score": "0",
                        "date": "2024-01-01",
                    }
                ],
                source="api:test",
            )

            self.assertEqual(inserted, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
