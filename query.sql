SELECT teacher_id, name, score, rating_count, comment_count
FROM teachers
ORDER BY RANDOM()
LIMIT 10;


-- 老师总数
SELECT COUNT(*) FROM teachers;

-- 有名字的老师数
SELECT COUNT(*) FROM teachers WHERE name IS NOT NULL AND name != '';

-- 评分为空的老师
SELECT teacher_id, url, name, score
FROM teachers
WHERE score IS NULL OR score = ''
LIMIT 20;

-- 评分超出范围的老师
SELECT teacher_id, name, score
FROM teachers
WHERE CAST(score AS REAL) < 0 OR CAST(score AS REAL) > 10;

-- 重名老师检查，不一定是错误，但要看学院是否不同
SELECT name, COUNT(*) AS cnt
FROM teachers
GROUP BY name
HAVING cnt > 1
ORDER BY cnt DESC
LIMIT 20;

-- 评论重复检查
SELECT teacher_id, content, date, COUNT(*) AS cnt
FROM comments
GROUP BY teacher_id, content, date
HAVING cnt > 1
ORDER BY cnt DESC
LIMIT 20;

-- 有评论数但实际没抓到评论的老师
SELECT t.teacher_id, t.name, t.comment_count, COUNT(c.id) AS real_comments
FROM teachers t
LEFT JOIN comments c ON t.teacher_id = c.teacher_id
WHERE CAST(t.comment_count AS INTEGER) > 0
GROUP BY t.teacher_id
HAVING real_comments = 0
LIMIT 20;

-- 评论数差异大的老师
SELECT t.teacher_id, t.name, t.comment_count, COUNT(c.id) AS real_comments
FROM teachers t
LEFT JOIN comments c ON t.teacher_id = c.teacher_id
GROUP BY t.teacher_id
HAVING ABS(CAST(t.comment_count AS INTEGER) - real_comments) > 5
ORDER BY ABS(CAST(t.comment_count AS INTEGER) - real_comments) DESC
LIMIT 30;

SELECT
    t.teacher_id,
    t.name,
    t.comment_count AS page_count,
    COUNT(c.id) AS db_count,
    CAST(t.comment_count AS INTEGER) - COUNT(c.id) AS missing
FROM teachers t
LEFT JOIN comments c ON t.teacher_id = c.teacher_id
GROUP BY t.teacher_id
ORDER BY missing DESC
LIMIT 50;