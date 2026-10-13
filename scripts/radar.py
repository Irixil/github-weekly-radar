#!/usr/bin/env python3
"""Read public weekly trends; validate and render an agent-written Chinese issue."""
import argparse
import concurrent.futures
import datetime as dt
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
SOURCES = ["https://github.com/trending" + suffix + "?since=weekly"
           for suffix in ("", "/python", "/typescript", "/javascript")]
OPEN_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "GPL-2.0",
                 "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0", "MPL-2.0",
                 "ISC", "Unlicense", "BSL-1.0", "EPL-2.0", "Zlib"}
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class Trending(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.heading = [], None, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article" and "Box-row" in attrs.get("class", "").split():
            self.row = {"text": [], "repo": None}
        if self.row is not None:
            if tag == "h2":
                self.heading = True
            if tag == "a" and self.heading:
                name = attrs.get("href", "").lstrip("/")
                if REPO_NAME.fullmatch(name):
                    self.row["repo"] = name

    def handle_data(self, text):
        if self.row is not None:
            self.row["text"].append(text)

    def handle_endtag(self, tag):
        if tag == "h2":
            self.heading = False
        if tag == "article" and self.row is not None:
            text = " ".join(self.row["text"])
            gain = re.search(r"([\d,]+)\s+stars this week", text)
            if not self.row["repo"] or not gain:
                raise ValueError("周榜行缺少仓库或本周 Star；停止，不能猜测。")
            self.rows.append({"repo": self.row["repo"], "source_rank": len(self.rows) + 1,
                              "weekly_stars": int(gain.group(1).replace(",", ""))})
            self.row = None


def fetch_source(url):
    request = urllib.request.Request(url, headers={"User-Agent": "GitHub-Weekly-Radar/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        parser = Trending()
        parser.feed(response.read().decode("utf-8"))
    if not parser.rows:
        raise ValueError("周榜为空：" + url)
    return {"url": url, "captured_at": dt.datetime.now(TZ).isoformat(), "rows": parser.rows}


def collect():
    # ponytail: four weekly source pages; add languages only when the user changes coverage.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        sources = list(pool.map(fetch_source, SOURCES))
    candidates = {}
    for source in sources:
        for row in source["rows"]:
            key = row["repo"].lower()
            item = candidates.setdefault(key, {"repo": row["repo"], "weekly_stars": row["weekly_stars"], "sources": []})
            if item["weekly_stars"] != row["weekly_stars"]:
                raise ValueError("同一仓库的周热度不一致；重新核对后再发榜。")
            item["sources"].append({"url": source["url"], "rank": row["source_rank"], "captured_at": source["captured_at"]})
    query = []
    for index, item in enumerate(candidates.values()):
        owner, name = item["repo"].split("/")
        query.append('r%d: repository(owner:%s,name:%s) { nameWithOwner url stargazerCount forkCount isArchived pushedAt licenseInfo { spdxId } primaryLanguage { name } defaultBranchRef { target { oid } } readme: object(expression:"HEAD:README.md") { ... on Blob { text } } readmeLower: object(expression:"HEAD:readme.md") { ... on Blob { text } } }' % (index, json.dumps(owner), json.dumps(name)))
    result = subprocess.run(["gh", "api", "graphql", "-f", "query=query { " + " ".join(query) + " }"], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("GitHub 资料读取失败：" + result.stderr[:500])
    payload = json.loads(result.stdout)
    if payload.get("errors"):
        raise RuntimeError("GitHub 资料不完整；停止本次更新。")
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    selected = []
    for metadata in payload["data"].values():
        if not metadata:
            raise ValueError("候选仓库已不可访问；需要重新核对。")
        item = candidates[metadata["nameWithOwner"].lower()]
        upper, lower = metadata.pop("readme"), metadata.pop("readmeLower")
        readme = upper or lower
        item["metadata"] = metadata
        if readme:
            filename = metadata["nameWithOwner"].replace("/", "--") + ".md"
            (cache / filename).write_text(readme["text"], encoding="utf-8")
            item["readme_cache"] = filename
            item["readme_path"] = "README.md" if upper else "readme.md"
        license_id = (metadata.get("licenseInfo") or {}).get("spdxId")
        if license_id in OPEN_LICENSES and not metadata["isArchived"]:
            selected.append(item)
    selected.sort(key=lambda item: (-item["weekly_stars"], item["repo"].lower()))
    if len(selected) < 20:
        raise ValueError("可确认开源的候选不足 20 个；不生成榜单。")
    output = {"issue_date": dt.datetime.now(TZ).date().isoformat(), "source_urls": SOURCES,
              "candidate_count": len(candidates), "eligible_count": len(selected), "projects": selected[:20]}
    (cache / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    (cache / "selected.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"candidates": len(candidates), "eligible": len(selected), "selected": 20,
                      "review": str(cache / "selected.json")}, ensure_ascii=False))


def week_id(date):
    year, week, _ = dt.date.fromisoformat(date).isocalendar()
    return "%d-W%02d" % (year, week)


def validate(issue):
    projects = issue["projects"]
    if len(projects) != 20 or len({p["repo"].lower() for p in projects}) != 20:
        raise ValueError("必须正好 20 个不同项目。")
    if issue["source_urls"] != SOURCES:
        raise ValueError("来源范围改变，需要明确说明。")
    gains = []
    for rank, project in enumerate(projects, 1):
        if project["rank"] != rank or not REPO_NAME.fullmatch(project["repo"]):
            raise ValueError("名次或仓库名不正确。")
        if project["license"] not in OPEN_LICENSES:
            raise ValueError("许可证需要核对：" + project["repo"])
        if type(project["weekly_stars"]) is not int or project["weekly_stars"] < 0:
            raise ValueError("周热度必须是非负整数。")
        if type(project["total_stars"]) is not int or project["total_stars"] < project["weekly_stars"]:
            raise ValueError("总 Star 与周热度不一致。")
        if project["url"] != "https://github.com/" + project["repo"]:
            raise ValueError("仓库链接不正确。")
        if not re.fullmatch(r"[0-9a-f]{40}", project["source_revision"]):
            raise ValueError("缺少源版本。")
        if not project["evidence_url"].startswith(project["url"] + "/blob/" + project["source_revision"] + "/"):
            raise ValueError("官方文档链接没有绑定源版本。")
        for field in ("summary", "fit_short", "project_fit", "cost", "caution", "first_step", "change"):
            if not isinstance(project.get(field), str) or not project[field].strip():
                raise ValueError("缺少说明字段：" + field)
        if project["difficulty"] not in {"低", "中", "高"}:
            raise ValueError("门槛等级不正确。")
        if not project["sources"] or project.get("verified_runtime") is not False:
            raise ValueError("缺少来源，或把文档研究冒充运行验证。")
        for source in project["sources"]:
            captured = dt.datetime.fromisoformat(source["captured_at"].replace("Z", "+00:00"))
            if source["url"] not in SOURCES or type(source["rank"]) is not int or source["rank"] < 1:
                raise ValueError("来源或原榜名次不正确。")
            if captured.tzinfo is None or abs((captured.astimezone(TZ).date() - dt.date.fromisoformat(issue["issue_date"])).days) > 1:
                raise ValueError("抓取时间与刊发日期不匹配。")
        gains.append((-project["weekly_stars"], project["repo"].lower()))
    if gains != sorted(gains):
        raise ValueError("榜单没有按周热度排序。")
    return week_id(issue["issue_date"])


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(projects):
    lines = ["| 名次 | 项目 | 本周新增 Star | 一句话用途 | 可用场景 |", "| --- | --- | ---: | --- | --- |"]
    for p in projects:
        lines.append("| %d | [%s](%s) | +%s | %s | %s |" % (p["rank"], p["repo"], p["url"], format(p["weekly_stars"], ","), cell(p["summary"]), cell(p["fit_short"])))
    return "\n".join(lines)


def render(issue):
    issue_id = validate(issue)
    report = ["# GitHub 开源周热榜 Top 20 · " + issue_id, "", "更新日期：" + issue["issue_date"] + "，北京时间。", "",
              "热度来自当次实时周 Trending；结合总榜和三种语言周榜去重、核对许可证后排序。这是本仓库的综合样本榜。用途按官方文档整理，通用应用建议是推断；本期没有实际安装或运行这些项目。", "", table(issue["projects"]), "",
              "## 本周优先关注", "", issue["editor_note"], "", "## 20 个项目详细说明", ""]
    for p in issue["projects"]:
        report += ["### %d. %s" % (p["rank"], p["repo"]), "",
                   "**能做什么**　" + p["summary"], "", "**可能用在哪**　" + p["project_fit"], "",
                   "**试用门槛**　%s。%s" % (p["difficulty"], p["cost"]), "",
                   "**注意事项**　" + p["caution"], "", "**小范围第一步**　" + p["first_step"], "",
                   "周新增 Star **+%s** · 总 Star **%s** · %s · %s · %s" % (format(p["weekly_stars"], ","), format(p["total_stars"], ","), p["language"], p["license"], p["change"]), "",
                   "[官方文档](%s) · [仓库](%s)" % (p["evidence_url"], p["url"]), ""]
    report += ["## 数据来源与边界", "", "候选 %d 个，许可证可确认且未归档的候选 %d 个，入榜 20 个。抓取时间与各项目的来源榜名次保存在 [本期数据](../data/%s.json)。" % (issue["candidate_count"], issue["eligible_count"], issue_id), ""]
    report += ["- [周榜来源 %d](%s)" % (i, url) for i, url in enumerate(SOURCES, 1)]
    report += ["", "[排名和更新规则](../UPDATE_GUIDE.md) · [通用应用场景](../USE_CASES.md)", "",
               "本周 Star 是 GitHub 页面在抓取时显示的滚动周数据，不是本仓库独立计数，也不是增长速度、产品质量或效果保证。名次变化以本仓库上一期为准。", ""]
    (ROOT / "weekly").mkdir(exist_ok=True)
    (ROOT / "weekly" / (issue_id + ".md")).write_text("\n".join(report), encoding="utf-8")
    archives = sorted((ROOT / "weekly").glob("*.md"), reverse=True)
    readme = ["# GitHub 开源周热榜", "", "每周 20 个最近热门的开源项目，用中文说清楚：它能做什么、可用在哪些场景、试用门槛是什么。只使用公开资料与通用应用建议。", "",
              "**最新一期：[" + issue_id + "](weekly/" + issue_id + ".md)** · 更新于 " + issue["issue_date"] + " · 每周一 09:00（北京时间）", "",
              "## 先看这几个", "", issue["editor_note"], "", "## 本周 20 名速览", "", table(issue["projects"]), "",
              "点击 [本期详细说明](weekly/" + issue_id + ".md) 查看通用应用建议、门槛、费用条件和注意事项。应用建议属于文档研究后的推断，尚未通过实际接入验证。", "",
              "## 历史周报", ""]
    readme += ["- [" + p.stem + "](weekly/" + p.name + ")" for p in archives]
    readme += ["", "## 如何读这个榜", "",
               "热度取自 GitHub 总周榜及 Python、TypeScript、JavaScript 周榜，去重和许可证核对后按本周新增 Star 排序。范围是这四个榜的样本池；本榜不等同于 GitHub 官方总榜，也不按历史总 Star 排名。", "",
               "[更新与排名规则](UPDATE_GUIDE.md) · [通用应用场景](USE_CASES.md) · [每期原始指标](data/)", "",
               "定时更新由 Codex 本地任务执行，需要电脑开机、联网并保持 Codex 应用运行。开源项目可能依赖收费服务；具体采用建议以每期说明为准。", ""]
    (ROOT / "README.md").write_text("\n".join(readme), encoding="utf-8")
    print("已验证并生成 " + issue_id + "，20 个项目。")


def self_check():
    parser = Trending()
    parser.feed('<article class="Box-row"><h2><a href="/a/one">a/one</a></h2><span>1,234 stars this week</span></article><article class="Box-row"><h2><a href="/b/two">b/two</a></h2><span>0 stars this week</span></article>')
    assert [(r["repo"], r["weekly_stars"]) for r in parser.rows] == [("a/one", 1234), ("b/two", 0)]
    try:
        Trending().feed('<article class="Box-row"><h2><a href="/a/one">one</a></h2></article>')
    except ValueError:
        pass
    else:
        raise AssertionError("缺失周热度时必须停止")
    assert week_id("2026-10-13") == "2026-W42"
    assert week_id("2027-01-01") == "2026-W53"
    print("解析与跨年周编号检查通过。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "check", "render", "self-check"])
    parser.add_argument("file", nargs="?")
    args = parser.parse_args()
    if args.command == "collect":
        collect()
    elif args.command == "self-check":
        self_check()
    else:
        if not args.file:
            parser.error("check / render 需要一期 JSON 文件")
        issue = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if args.command == "render":
            render(issue)
        else:
            print(validate(issue) + " 数据检查通过，20 个项目。")
