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
import unicodedata
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
SOURCES = ["https://github.com/trending" + suffix + "?since=weekly"
           for suffix in ("", "/python", "/typescript", "/javascript", "/go", "/rust", "/swift", "/dart", "/java")]
OPEN_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "GPL-2.0",
                 "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0", "MPL-2.0",
                 "ISC", "Unlicense", "BSL-1.0", "EPL-2.0", "Zlib"}
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def history(exclude=None):
    known = {}
    for path in (ROOT / "data").glob("*.json"):
        if exclude and path.resolve() == exclude.resolve():
            continue
        for project in json.loads(path.read_text(encoding="utf-8")).get("projects", []):
            name = project["repo"].lower()
            known[name] = project.get("repo_id") or known.get(name)
    cached = ROOT / ".cache" / "history_ids.json"
    if cached.exists():
        resolved = json.loads(cached.read_text(encoding="utf-8"))
        known = {name: value or resolved.get(name) for name, value in known.items()}
    return known


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
    # ponytail: fixed weekly source pages keep popularity metrics comparable.
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
    candidate_names = list(candidates)
    previous = history()
    unresolved = [name for name, repo_id in previous.items() if not repo_id]
    for index, item in enumerate(candidates.values()):
        owner, name = item["repo"].split("/")
        query.append('r%d: repository(owner:%s,name:%s) { id nameWithOwner url stargazerCount forkCount isArchived pushedAt licenseInfo { spdxId } primaryLanguage { name } defaultBranchRef { target { oid } } readme: object(expression:"HEAD:README.md") { ... on Blob { text } } readmeLower: object(expression:"HEAD:readme.md") { ... on Blob { text } } }' % (index, json.dumps(owner), json.dumps(name)))
    for index, name in enumerate(unresolved):
        owner, repo = name.split("/")
        query.append('h%d: repository(owner:%s,name:%s) { id nameWithOwner }' % (index, json.dumps(owner), json.dumps(repo)))
    payload = {"data": {}}
    # Read README files in small batches to avoid oversized GitHub responses.
    for start in range(0, len(query), 15):
        result = subprocess.run(["gh", "api", "graphql", "-f", "query=query { " + " ".join(query[start:start + 15]) + " }"], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("GitHub 资料读取失败：" + result.stderr[:500])
        batch = json.loads(result.stdout)
        if batch.get("errors"):
            raise RuntimeError("GitHub 资料不完整；停止本次更新。")
        payload["data"].update(batch["data"])
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    for index, name in enumerate(unresolved):
        metadata = payload["data"]["h%d" % index]
        if metadata:
            previous[name] = metadata["id"]
            previous[metadata["nameWithOwner"].lower()] = metadata["id"]
    (cache / "history_ids.json").write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
    selected = []
    for key, metadata in payload["data"].items():
        if not key.startswith("r"):
            continue
        if not metadata:
            raise ValueError("候选仓库已不可访问；需要重新核对。")
        item = candidates[candidate_names[int(key[1:])]]
        item["repo"] = metadata["nameWithOwner"]
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
    previous_ids = {value for value in previous.values() if value}
    fresh = [item for item in selected if item["repo"].lower() not in previous and item["metadata"]["id"] not in previous_ids]
    if len(fresh) < 5:
        raise ValueError("未推荐过且可确认开源的候选不足 5 个；不重复凑数。")
    output = {"issue_date": dt.datetime.now(TZ).date().isoformat(), "source_urls": SOURCES,
              "candidate_count": len(candidates), "eligible_count": len(selected), "history_count": len(previous),
              "unseen_count": len(fresh), "projects": fresh[:5]}
    (cache / "candidates.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
    (cache / "new_candidates.json").write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
    (cache / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
    (cache / "selected.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"candidates": len(candidates), "eligible": len(selected), "already_recommended": len(previous), "unseen": len(fresh), "selected": 5,
                      "review": str(cache / "selected.json")}, ensure_ascii=False))


def week_id(date):
    year, week, _ = dt.date.fromisoformat(date).isocalendar()
    return "%d-W%02d" % (year, week)


def validate(issue):
    projects = issue["projects"]
    count = 5 if issue.get("schema_version", 1) >= 3 else 20
    if len(projects) != count or len({p["repo"].lower() for p in projects}) != count:
        raise ValueError("本格式必须正好 %d 个不同项目。" % count)
    if not issue["source_urls"] or any(url not in SOURCES for url in issue["source_urls"]):
        raise ValueError("来源不在已确认的 GitHub 周榜范围。")
    previous = history(ROOT / "data" / (issue["issue_date"] + ".json")) if count == 5 else {}
    previous_ids = {value for value in previous.values() if value}
    current_ids = set()
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
        if count == 5:
            repo_id = project.get("repo_id")
            if not isinstance(repo_id, str) or not repo_id:
                raise ValueError("缺少 GitHub 稳定仓库标识。")
            if project["repo"].lower() in previous or repo_id in previous_ids or repo_id in current_ids:
                raise ValueError("已推荐过或重复的项目：" + project["repo"])
            current_ids.add(repo_id)
        if not re.fullmatch(r"[0-9a-f]{40}", project["source_revision"]):
            raise ValueError("缺少源版本。")
        if not project["evidence_url"].startswith(project["url"] + "/blob/" + project["source_revision"] + "/"):
            raise ValueError("官方文档链接没有绑定源版本。")
        for field in ("summary", "fit_short", "project_fit", "cost", "caution", "first_step", "change"):
            if not isinstance(project.get(field), str) or not project[field].strip():
                raise ValueError("缺少说明字段：" + field)
        if project["difficulty"] not in {"低", "中", "高"}:
            raise ValueError("门槛等级不正确。")
        if issue.get("schema_version", 1) >= 2:
            if not all(isinstance(project.get(key), str) and project[key].strip()
                       for key in ["example", "diagram_title"]):
                raise ValueError("新资料必须有具体例子与图解标题。")
            steps = project.get("diagram_steps")
            if not isinstance(steps, list) or len(steps) != 3 or not all(isinstance(s, str) and s.strip() for s in steps):
                raise ValueError("每个项目必须有输入、处理、结果三步图解。")
            if any(len(wrap_text(step, 11)) > 3 for step in steps):
                raise ValueError("图解文字过长，需要改写为简短白话。")
        if not project["sources"] or project.get("verified_runtime") is not False:
            raise ValueError("缺少来源，或把文档研究冒充运行验证。")
        for source in project["sources"]:
            captured = dt.datetime.fromisoformat(source["captured_at"].replace("Z", "+00:00"))
            if source["url"] not in issue["source_urls"] or type(source["rank"]) is not int or source["rank"] < 1:
                raise ValueError("来源或原榜名次不正确。")
            if captured.tzinfo is None or abs((captured.astimezone(TZ).date() - dt.date.fromisoformat(issue["issue_date"])).days) > 1:
                raise ValueError("抓取时间与刊发日期不匹配。")
        gains.append((-project["weekly_stars"], project["repo"].lower()))
    if gains != sorted(gains):
        raise ValueError("榜单没有按周热度排序。")
    return issue["issue_date"] if issue.get("schema_version", 1) >= 2 else week_id(issue["issue_date"])


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(projects, report_path=None):
    lines = ["| 名次 | 项目 | 本周新增 Star | 一句话用途 | 可用场景 |", "| --- | --- | ---: | --- | --- |"]
    for p in projects:
        link = p["url"] if report_path is None else report_path + "#project-%02d" % p["rank"]
        name = p["repo"].split("/")[1]
        lines.append("| %d | [%s](%s) | +%s | %s | %s |" % (p["rank"], name, link, format(p["weekly_stars"], ","), cell(p["summary"]), cell(p["fit_short"])))
    return "\n".join(lines)


def wrap_text(text, width):
    lines, current, units = [], "", 0
    for char in text:
        size = 1 if unicodedata.east_asian_width(char) in "WF" else 0.6
        if current and units + size > width:
            lines.append(current)
            current, units = "", 0
        current += char
        units += size
    return lines + ([current] if current else [])


def diagram(project):
    title = escape(project["diagram_title"])
    pieces = ['<svg xmlns="http://www.w3.org/2000/svg" width="960" height="320" viewBox="0 0 960 320" role="img" aria-labelledby="title desc">',
              '<title id="title">' + title + '</title>',
              '<desc id="desc">' + escape(' → '.join(project["diagram_steps"])) + '</desc>',
              '<style>text{font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;fill:#193446}.body{font-size:22px}.label{font-size:17px;font-weight:600}</style>',
              '<rect width="960" height="320" rx="16" fill="#f5f7f8"/>',
              '<text x="30" y="44" font-size="28" font-weight="650">' + title + '</text>',
              '<text x="30" y="75" font-size="15" fill="#536674">' + escape(project["repo"]) + ' · 工作方式示意</text>',
              '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0 0 L6 3 L0 6" fill="none" stroke="#52788d" stroke-width="1.4"/></marker></defs>']
    for index, (label, step) in enumerate(zip(["给它什么", "它怎么处理", "得到什么"], project["diagram_steps"])):
        x = 30 + 310 * index
        fill = ["#ffffff", "#e8eff3", "#e4f0ec"][index]
        pieces += ['<rect x="%d" y="105" width="280" height="150" rx="12" fill="%s" stroke="#c5d2da"/>' % (x, fill),
                   '<text x="%d" y="137" text-anchor="middle" class="label">%s</text>' % (x + 140, label)]
        lines = wrap_text(step, 11)
        y = 196 - (len(lines) - 1) * 14
        pieces += ['<text x="%d" y="%d" text-anchor="middle" class="body">%s</text>' % (x + 140, y + 28 * i, escape(line)) for i, line in enumerate(lines)]
        if index < 2:
            pieces += ['<path d="M%d 180 H%d" fill="none" stroke="#52788d" stroke-width="2.4" marker-end="url(#arrow)"/>' % (x + 286, x + 304)]
    pieces += ['<text x="30" y="292" font-size="14" fill="#536674">三步看懂用途 · 通用说明图，不是软件运行截图</text>', '</svg>']
    return "\n".join(pieces) + "\n"


def render(issue):
    issue_id = validate(issue)
    if issue.get("schema_version", 1) < 3:
        raise ValueError("旧版 20 项资料保留归档；新资料使用每日 5 项格式。")
    report_path = "reports/" + issue_id + ".md"
    assets = ROOT / "assets" / issue_id
    assets.mkdir(parents=True, exist_ok=True)
    report = ["# " + issue_id + " · 每日开源项目推荐 5 个", "",
              "每天在同一个仓库新增一份资料，每份 5 个项目，避开全部历史推荐。先看速览，再点项目查看图解和例子。", "",
              "热度参考抓取时的 GitHub 滚动周榜，**不是一天内的新增 Star**。用途按公开文档整理，图解和例子用于解释用法；本期未实际安装或运行这些项目。", "",
              "**先认两个词**　Agent 是能分步办事的 AI 助手；Skill 是写给 AI 助手的工作说明。Star 是关注热度，不代表质量。", "",
              "## 本期速览", "", table(issue["projects"], ""), "",
              "## 可以先看", "", issue["editor_note"], ""]
    for p in issue["projects"]:
        filename = p["repo"].replace("/", "--") + ".svg"
        (assets / filename).write_text(diagram(p), encoding="utf-8")
        report += ['<a id="project-%02d"></a>' % p["rank"], "",
                   "## %d. [%s](%s)" % (p["rank"], p["repo"].split("/")[1], p["url"]), "",
                   "**一句话用途**　" + p["summary"], "",
                   "![%s](../assets/%s/%s)" % (p["diagram_title"], issue_id, filename), "",
                   "**举个例子**　" + p["example"], "",
                   "**适合什么情况**　" + p["project_fit"], "",
                   "**门槛与费用**　门槛%s。%s" % (p["difficulty"], p["cost"]), "",
                   "**注意事项**　" + p["caution"], "", "**小范围第一步**　" + p["first_step"], "",
                   "周新增 Star **+%s** · 总 Star **%s** · %s · %s · %s" % (format(p["weekly_stars"], ","), format(p["total_stars"], ","), p["language"], p["license"], p["change"]), "",
                   "[官方文档](%s) · [仓库](%s)" % (p["evidence_url"], p["url"]), ""]
    report += ["## 来源与选取方法", "", "候选池 %d 个，可识别开源许可证且未归档的候选 %d 个。已核对 %d 个历史推荐，剩余未介绍过的候选 %d 个；再核对用途与许可，选出本期 5 个。抓取时间与原榜名次见 [本期数据](../data/%s.json)。" % (issue["candidate_count"], issue["eligible_count"], issue["history_count"], issue["unseen_count"], issue_id), ""]
    for excluded in issue.get("exclusions", []):
        report += ["- [%s](%s) 暂未收录：%s" % (excluded["repo"], excluded["source_url"], excluded["reason"])]
    report += ["- [周榜来源 %d](%s)" % (i, url) for i, url in enumerate(issue["source_urls"], 1)]
    report += ["", "[排名和更新规则](../UPDATE_GUIDE.md) · [通用应用场景](../USE_CASES.md)", "",
               "序号是本期推荐顺序，不是 GitHub 官方总榜排名。图解和应用例子仅作说明。", "",
               "[返回资料首页](../README.md)", ""]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / report_path).write_text("\n".join(report), encoding="utf-8")
    archives = sorted((ROOT / "reports").glob("*.md"), key=lambda p: (not p.stem.endswith("-top20"), p.stem), reverse=True)
    readme = ["# 每日开源项目推荐", "",
              "**同一个仓库，每天新增一份按日期命名的资料，每份推荐 5 个项目，跨全部历史去重。** 每个项目配一张用途图和一个具体例子，用简短中文说明能做什么、何时值得用。", "",
              "只使用公开资料和通用应用建议；旧文档保留，首页持续列出新旧资料。", "",
              "**最新资料：[" + issue_id + "](" + report_path + ")** · 每天 09:00（北京时间）", "",
              "## 先看这份资料", "", issue["editor_note"], "",
              "**阅读方式**　速览选项目 → 点名称 → 看三步图解 → 看具体例子和使用门槛。", "",
              "## 今日 5 个项目", "", table(issue["projects"], report_path), "",
              "## 所有资料", ""]
    readme += ["- [" + p.stem + (" · 早期图文格式示例（20 项，留档）" if p.stem.endswith("-top20") else " · 每日 5 项图解＋例子") + "](reports/" + p.name + ")" for p in archives]
    readme += ["- [" + p.stem + " · 早期文字周榜](weekly/" + p.name + ")" for p in sorted((ROOT / "weekly").glob("*.md"), reverse=True)]
    readme += ["", "## 数据怎么选", "",
               "每天读取总周榜和 Python、TypeScript、JavaScript、Go、Rust、Swift、Dart、Java 周榜，核对历史推荐、许可证与用途，选取 5 个未介绍过的项目。热度指标是滚动一周，不是单日新增量；这是这些榜的样本池。项目改名时，结合 GitHub 仓库标识去重。", "",
               "[更新与排名规则](UPDATE_GUIDE.md) · [通用应用场景](USE_CASES.md) · [每期原始指标](data/)", "",
               "定时更新由 Codex 本地任务执行，需要电脑开机、联网并保持 Codex 应用运行。开源项目可能依赖收费服务；具体采用建议以每期说明为准。", ""]
    (ROOT / "README.md").write_text("\n".join(readme), encoding="utf-8")
    print("已验证并生成 " + issue_id + "，5 个未重复项目。")


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
    assert wrap_text("检查页面结果", 11) == ["检查页面结果"]
    assert len(wrap_text("注册并登录后查看个人主页", 6)) == 2
    sample = diagram({"repo": "a/test", "diagram_title": "看懂用途", "diagram_steps": ["材料", "处理", "结果 & <证据>"]})
    assert "结果 &amp; &lt;证据&gt;" in sample
    print("解析、日期与图解文字检查通过。")


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
            print(validate(issue) + " 数据检查通过，%d 个项目。" % len(issue["projects"]))
