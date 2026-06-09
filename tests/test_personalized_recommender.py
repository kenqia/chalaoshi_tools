import unittest

import personalized_recommender as recommender


def course_row(**overrides):
    row = {
        "teacher_id": 1,
        "teacher": "张老师",
        "college": "计算机科学与技术学院",
        "course": "软件工程导论",
        "avg_gpa": 4.3,
        "sample_count": 80,
        "sample_label": "80",
        "teacher_score": 8.8,
        "rating_count": 100,
        "checkin_ratio": 20.0,
        "comment_count": 120,
        "relevance": "强相关",
        "risk": 0,
        "risk_reasons": [],
        "course_easy": 3.0,
        "course_hard": 0.0,
        "teacher_easy": 5.0,
        "teacher_hard": 1.0,
        "snippets": [],
        "url": "https://chalaoshi.de/t/1/",
    }
    row.update(overrides)
    return row


class NaturalLanguagePreferenceTest(unittest.TestCase):
    def test_parses_priorities_thresholds_exclusions_and_limit(self):
        preference = recommender.parse_preference(
            "给分高最重要，最好不点名、事少，软件相关，"
            "样本至少50，点名率低于30%，排除体育和实习，前40门课"
        )

        self.assertEqual(preference.mode, "course")
        self.assertEqual(preference.limit, 40)
        self.assertEqual(preference.min_sample, 50)
        self.assertEqual(preference.max_checkin, 30)
        self.assertGreater(preference.weights["gpa"], preference.weights["teacher"])
        self.assertGreater(preference.weights["checkin"], 0)
        self.assertGreater(preference.weights["workload"], 0)
        self.assertGreater(preference.weights["relevance"], 0)
        self.assertEqual(preference.exclusions, ["体育", "实习"])

    def test_negations_disable_unwanted_dimensions(self):
        preference = recommender.parse_preference(
            "按老师排序，不在意点名，不要求专业相关，前30"
        )

        self.assertEqual(preference.mode, "teacher")
        self.assertEqual(preference.limit, 30)
        self.assertEqual(preference.weights["checkin"], 0)
        self.assertEqual(preference.weights["relevance"], 0)


class PersonalizedRankingTest(unittest.TestCase):
    def test_priority_changes_course_order(self):
        high_gpa = course_row(
            course="高绩点但常点名",
            avg_gpa=4.7,
            checkin_ratio=90.0,
        )
        low_checkin = course_row(
            teacher_id=2,
            course="较低绩点但不点名",
            avg_gpa=4.05,
            checkin_ratio=0.0,
        )

        gpa_first = recommender.rank_courses(
            [high_gpa, low_checkin],
            recommender.parse_preference("只看给分高，点名无所谓"),
        )
        checkin_first = recommender.rank_courses(
            [high_gpa, low_checkin],
            recommender.parse_preference("不点名最重要，绩点要求不高"),
        )

        self.assertEqual(gpa_first[0]["course"], "高绩点但常点名")
        self.assertEqual(checkin_first[0]["course"], "较低绩点但不点名")

    def test_explicit_checkin_limit_excludes_unknown_values(self):
        preference = recommender.parse_preference("点名率低于30%，前10门课")

        ranked = recommender.rank_courses(
            [
                course_row(course="点名率已知", checkin_ratio=20.0),
                course_row(
                    teacher_id=2,
                    course="点名率未知",
                    checkin_ratio=None,
                ),
            ],
            preference,
        )

        self.assertEqual([row["course"] for row in ranked], ["点名率已知"])

    def test_teacher_mode_groups_courses_by_teacher(self):
        rows = [
            course_row(course="软件工程导论"),
            course_row(course="数据库", avg_gpa=4.4),
            course_row(teacher_id=2, teacher="李老师", course="算法", avg_gpa=4.2),
        ]
        preference = recommender.parse_preference("按老师排序，给分高，前10")

        teachers = recommender.rank_teachers(rows, preference)

        self.assertEqual(len(teachers), 2)
        self.assertEqual(teachers[0]["teacher"], "张老师")
        self.assertEqual(teachers[0]["course_count"], 2)
        self.assertIn("数据库", teachers[0]["top_courses"])

    def test_html_explains_prompt_and_contains_ranked_result(self):
        preference = recommender.parse_preference("给分高，不点名，前10门课")
        ranked = recommender.rank_courses([course_row()], preference)

        document = recommender.render_personalized_html(
            ranked,
            preference,
            "给分高，不点名，前10门课",
            "2026-06-09 12:00",
        )

        self.assertIn("给分高，不点名，前10门课", document)
        self.assertIn("软件工程导论", document)
        self.assertIn("系统理解", document)
        self.assertIn("<!doctype html>", document.lower())


if __name__ == "__main__":
    unittest.main()
