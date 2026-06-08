import unittest

import generate_course_recommendations as report


class RecommendationHelpersTest(unittest.TestCase):
    def test_parse_sample_count_handles_500_plus(self):
        self.assertEqual(report.parse_sample_count("500+"), 500)
        self.assertEqual(report.parse_sample_count("42"), 42)
        self.assertEqual(report.parse_sample_count(""), 0)

    def test_professional_relevance_identifies_software_courses(self):
        score, label = report.professional_relevance(
            "B/S体系软件设计",
            "计算机科学与技术学院",
        )
        self.assertGreaterEqual(score, 3)
        self.assertEqual(label, "强相关")

    def test_unrelated_course_is_not_related_only_because_of_cs_college(self):
        score, label = report.professional_relevance(
            "创业管理",
            "计算机科学与技术学院",
        )
        self.assertEqual(score, 0)
        self.assertEqual(label, "跨专业")

    def test_specialized_practicum_is_high_risk_for_cross_major_student(self):
        risk, reasons = report.course_risk(
            "口腔颌面外科临床实习",
            "医学院",
        )
        self.assertGreaterEqual(risk, 3)
        self.assertTrue(any("实习" in reason for reason in reasons))

    def test_comment_signals_distinguish_easy_and_hard_language(self):
        positive = report.comment_signals(
            "这门课事少，不点名，无考试，给分好，适合摸鱼。"
        )
        negative = report.comment_signals(
            "作业很多，考试很难，卡绩，天天签到。"
        )

        self.assertGreater(positive["easy"], positive["hard"])
        self.assertGreater(negative["hard"], negative["easy"])


if __name__ == "__main__":
    unittest.main()
