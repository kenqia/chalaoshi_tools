# chalaoshi

查老师公开数据的本地增量爬虫，以及支持中文自然语言偏好的课程/老师推荐工具。

## 数据

- `chalaoshi_public_data.sqlite`：老师、课程 GPA、评论正文、抓取状态和历史快照。
- `course_recommendations.html`：可搜索、筛选和排序的课程推荐表。
- `course_recommendations.csv`：推荐结果的结构化导出。
- `personalized_example.html`：软件工程学生偏好的个性化推荐示例。

数据库使用 Git LFS 管理。克隆仓库后需要安装 Git LFS，并执行：

```bash
git lfs pull
```

## 环境

```bash
conda activate chalaoshi
```

主要依赖包括 `requests`、`beautifulsoup4`、`lxml`、`pandas`、`tqdm` 和
`playwright`。

## 增量抓取

默认刷新老师详情和评分快照，只插入尚未保存的评论：

```bash
python crawl_chalaoshi_all.py \
  --db chalaoshi_public_data.sqlite \
  --out-dir chalaoshi_export \
  --start-id 1 \
  --max-id 7000 \
  --no-browser \
  --sleep 0.05 \
  --concurrency 8
```

## 生成推荐报告

```bash
python generate_course_recommendations.py
```

推荐算法以课程平均 GPA 为核心，并综合样本量、老师评分、点名率、专业相关性、
跨专业门槛，以及评论中的事少、给分、考试和作业信号。

## 自然语言个性化推荐

`personalized_recommender.py` 会在本地解析中文偏好，不需要 API Key，也不会把输入
发送给第三方。它支持课程排序和老师排序，并在 HTML 顶部展示系统实际理解出的
权重、门槛和排除条件。

生成课程推荐：

```bash
python personalized_recommender.py \
  --prompt "给分高最重要，最好不点名、事少，软件相关，样本至少30，排除体育和实习，前50门课" \
  --output my_courses.html
```

生成老师推荐：

```bash
python personalized_recommender.py \
  --prompt "按老师排序，给分高、点名少、评论样本多，不要求专业相关，前30" \
  --output my_teachers.html
```

可以识别的常见表达包括：

- 优先目标：`给分高`、`不点名`、`事少`、`老师评分高`、`专业相关`、`样本多`。
- 否定目标：`点名无所谓`、`不要求专业相关`、`绩点要求不高`、`难度无所谓`。
- 硬条件：`GPA不低于4.2`、`样本至少50`、`点名率低于30%`、`前40门课`。
- 排除条件：`排除体育和实习`、`不选医学、竞赛`。
- 排序对象：默认按课程；写入 `按老师排序` 可聚合同一老师的候选课程。

这是可解释的规则解析，不是大语言模型。建议先查看页面中的“系统理解”区域；
若理解不符合预期，换一种更明确的表达即可。点名率、老师评分和部分评论属于老师
维度，可能混合多门课程，最终选课前仍需核对当学期开课信息和培养方案。

## 测试

```bash
python -m unittest discover -s tests -v
```
