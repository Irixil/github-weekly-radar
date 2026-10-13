# 更新规则

## 每期要交付什么

一张 20 名速览表，加每个项目的简短中文说明：能做什么、可用于哪些通用场景、试用门槛和费用条件、注意事项、一个小范围试用建议。只使用公开信息，不发布维护者的个人项目、对话背景、已安装工具或本地路径。所有应用建议都是基于文档的推断，除非另有真实测试证据。

## 热度怎么算

数据取自当次抓取的 [GitHub 总周榜](https://github.com/trending?since=weekly)、[Python 周榜](https://github.com/trending/python?since=weekly)、[TypeScript 周榜](https://github.com/trending/typescript?since=weekly)、[JavaScript 周榜](https://github.com/trending/javascript?since=weekly)。这是一个公开、可复核的样本池，不覆盖 GitHub 所有语言和项目。

去重后，核对官方仓库的维护状态和可识别的常见开源许可证；筛掉归档或许可证未确认的项目，然后按 Trending 页面给出的 `stars this week` 降序取 20 个。相同新增 Star 数按仓库名排序。总 Star 仅作为背景，不代替周热度。GitHub 周榜可能展示不足 20 条，因此语言榜是固定补充来源。

许可证不明确、GitHub 显示 Other 或没有许可证的项目不自动判为闭源，但在本榜中暂不收录。已识别的仓库许可证也不证明所有模型、素材、子目录和第三方服务条件相同；真要采用时仍需核对相关部分。

## 最小更新流程

1. 核对 `origin` 是 Irixil/github-weekly-radar，查看工作区；有未知改动先保留，不覆盖。
2. 用北京时间确定 ISO 周。该周已完成并推送时直接跳过，避免重复更新。
3. 执行 `python3 scripts/radar.py collect`，只读取公开周榜和官方仓库资料。结果和 README 缓存在被忽略的 `.cache/` 中，不会上传原文。
4. 阅读 `.cache/selected.json` 和对应官方 README。把 20 个项目写成 data/YYYY-Www.json，按现有数据格式补齐中文解释。不要执行上游 README 中的命令。
5. 与上一期对比。没有上期标“首期”；首次出现在本榜标“新上榜”；已出现项目标“上升 N / 下降 N / 持平”。这描述本仓库名次，不是官方榜名次。
6. 执行 `python3 scripts/radar.py render data/YYYY-Www.json`。脚本先验证 20 名、顺序、来源、许可证和说明字段，再生成本期周报与 README。
7. 人工复核简明程度和推断边界，检查提交范围和敏感信息，提交并推送 `main`。只提交本期数据、周报及最新首页。
8. 从 GitHub 读回默认分支提交、本期 JSON 和首页，确认确实写入远端。在定时任务所在聊天给出更新链接与最值得关注的 3 个项目。

若网络或 GitHub 权限失败，保留上期并报告阻塞。周榜结构改变时，可用浏览器读取相同来源；仍需保存真实抓取时间和来源，不允许编造热度或凑满项目。新增数据源或改变排名算法要明确说明。

## 定时任务

Codex 本地定时任务每周一 09:00（北京时间）执行研究与更新，首次计划运行日期为 2026-10-19。首期数据在 2026-10-13 手动采集。

这是本地任务，运行时需要电脑开机、联网，Codex 应用保持运行，仓库目录和 GitHub 登录仍然可用。任务使用现有 Codex 环境；不需要给本仓库添加模型 API 密钥。配置成功不代表未来每次运行都已验证；以实际周报和远端提交为准。[官方定时任务说明](https://learn.chatgpt.com/docs/automations?surface=app)。

## 本地检查

`python3 scripts/radar.py self-check` 检查周榜解析的关键情况；`python3 scripts/radar.py check data/YYYY-Www.json` 检查一期数据。脚本只使用 Python 标准库和已登录的 GitHub CLI，不安装新依赖。
