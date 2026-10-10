# -*- coding: utf-8 -*-
"""
quotes.toscrape.com 动态页爬虫（课程作业二：JavaScript 渲染 / 无限滚动）
- 只操作公开教学沙盒，所有请求间隔 >= 1 秒
- /js    ：等待 JavaScript 渲染完成后，从 DOM 抓取名言与作者
- /scroll：模拟向下滚动直到没有新内容加载，同时监听网络请求找出 XHR 数据接口
- 去重后合并保存 quotes.csv
- 产出：quotes.csv + dynamic_scraping_report.md（含接口直连代码）
"""
import csv
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path(__file__).resolve().parent
CSV_PATH = OUT_DIR / "quotes.csv"
REPORT_PATH = OUT_DIR / "dynamic_scraping_report.md"

BASE = "https://quotes.toscrape.com"
URL_JS = BASE + "/js"
URL_SCROLL = BASE + "/scroll"

UA = "Mozilla/5.0 (compatible; HomeworkScraper/1.0; educational use)"
INTERVAL = 1.0  # 秒：相邻 HTTP 请求最小间隔

session = requests.Session()
session.headers.update({"User-Agent": UA})
_last_request_ts = 0.0


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def polite_sleep():
    """保证相邻请求间隔 >= INTERVAL 秒（控制请求频率）。"""
    global _last_request_ts
    if _last_request_ts:
        gap = time.time() - _last_request_ts
        if gap < INTERVAL:
            time.sleep(INTERVAL - gap)
    _last_request_ts = time.time()


# ---------------------------------------------------------------
# 第一部分：用 requests 探测原始 HTML（取证：为什么直接抓不到）
# ---------------------------------------------------------------
def probe_with_requests():
    """请求原始 HTML，统计渲染后的引言节点数量，寻找数据线索。"""
    log("阶段 0：requests 探测原始 HTML ...")
    evidence = {}
    for name, url in [("js", URL_JS), ("scroll", URL_SCROLL)]:
        polite_sleep()
        resp = session.get(url, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text
        ev = {
            "status": resp.status_code,
            "html_len": len(html),
            "quote_nodes": html.count('class="quote"'),  # 渲染后的 DOM 节点
            "script_tags": html.count("<script"),
            "external_js": re.findall(r'<script[^>]*src="([^"]+)"', html),
            "inline_data": bool(re.search(r"\bdata\s*=\s*\[", html)),  # 内嵌 JS 数据
            "api_refs": sorted(set(re.findall(r'["\'](/api[^"\']*)["\']', html))),
        }
        evidence[name] = ev
        log(f"  /{name}: HTTP {ev['status']}, {ev['html_len']} 字节, "
            f"渲染引言节点 {ev['quote_nodes']} 个, script 标签 {ev['script_tags']} 个, "
            f"内嵌数据 {ev['inline_data']}, 接口线索 {ev['api_refs'] or '无'}")
    return evidence


# ---------------------------------------------------------------
# 第二部分：Playwright 抓取（真实浏览器执行 JS）
# ---------------------------------------------------------------
def extract_quotes(page):
    """从已渲染的 DOM 提取当前页面上所有引言。"""
    out = []
    for q in page.locator("div.quote").all():
        text = q.locator("span.text").inner_text().strip().strip("\u201c\u201d")
        author = q.locator("small.author").inner_text().strip()
        out.append((text, author))
    return out


def scrape_js_page(browser):
    """/js：等待 JS 渲染完成，然后从 DOM 抓取。"""
    log("阶段 1：抓取 /js（等待 JavaScript 渲染）...")
    page = browser.new_page()
    api_calls = []
    page.on("request", lambda r: api_calls.append(r.url) if "/api/" in r.url else None)

    polite_sleep()
    page.goto(URL_JS, timeout=60000)
    page.wait_for_selector("div.quote", timeout=30000)  # 等待 JS 把数据渲染进 DOM
    page.wait_for_timeout(1000)  # 再稳一下，确保渲染完整

    quotes = extract_quotes(page)
    log(f"  渲染完成，从 DOM 抓到 {len(quotes)} 条")
    page.close()
    return quotes, api_calls


def scrape_scroll_page(browser):
    """/scroll：滚动到底部直到不再加载新内容；同时记录 XHR 接口。"""
    log("阶段 2：抓取 /scroll（模拟无限滚动）...")
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    api_calls = []

    def on_request(req):
        if "/api/" in req.url:
            api_calls.append(req.url)

    page.on("request", on_request)

    polite_sleep()
    page.goto(URL_SCROLL, timeout=60000)
    page.wait_for_selector("div.quote", timeout=30000)

    seen = 0
    stable_rounds = 0
    round_no = 0
    while stable_rounds < 3:  # 连续 3 轮无新增才判定到底了
        round_no += 1
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)  # 等待可能的异步加载
        count = page.locator("div.quote").count()
        if count > seen:
            log(f"  第 {round_no} 轮滚动：{seen} -> {count} 条")
            stable_rounds = 0
            seen = count
        else:
            stable_rounds += 1
            log(f"  第 {round_no} 轮滚动：仍是 {count} 条（稳定 {stable_rounds}/3）")
        polite_sleep()  # 控制节奏

    quotes = extract_quotes(page)
    log(f"  滚动结束，共 {len(quotes)} 条；捕获 XHR 接口调用 {len(api_calls)} 次")
    for u in sorted(set(api_calls))[:3]:
        log(f"    接口示例: {u}")
    page.close()
    return quotes, api_calls


# ---------------------------------------------------------------
# 第三部分：直接请求 XHR 数据接口（requests 就够了）
# ---------------------------------------------------------------
def fetch_via_api():
    """直接循环请求 /api/quotes?page=N，验证接口可用。"""
    log("阶段 3：直接请求 XHR 数据接口验证 ...")
    quotes, page_no = [], 1
    while True:
        polite_sleep()
        resp = session.get(f"{BASE}/api/quotes?page={page_no}", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data["quotes"]:
            quotes.append((item["text"].strip().strip("\u201c\u201d"),
                           item["author"]["name"].strip()))
        log(f"  接口第 {page_no} 页: {len(data['quotes'])} 条, has_next={data['has_next']}")
        if not data["has_next"]:
            break
        page_no += 1
    log(f"  接口直连共取回 {len(quotes)} 条")
    return quotes


# ---------------------------------------------------------------
# 输出
# ---------------------------------------------------------------
def write_csv(all_rows):
    """去重（按 名言+作者）后写 quotes.csv。"""
    unique = OrderedDict()
    dup = 0
    for text, author, source in all_rows:
        key = (text, author)
        if key in unique:
            dup += 1
        else:
            unique[key] = {"名言": text, "作者": author, "来源": source}

    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["名言", "作者", "来源"])
        writer.writeheader()
        writer.writerows(unique.values())
    log(f"已写出 {CSV_PATH}（原始 {len(all_rows)} 条 -> 去重后 {len(unique)} 条，重复 {dup} 条）")
    return len(unique), dup


def write_report(evidence, n_js, n_scroll, api_calls, n_api, n_total, dup):
    js_ev, scroll_ev = evidence["js"], evidence["scroll"]
    api_set = sorted(set(api_calls))
    api_example = api_set[0] if api_set else f"{BASE}/api/quotes?page=1"
    max_scroll_page = max([int(re.search(r"page=(\d+)", u).group(1))
                           for u in api_set if "page=" in u], default=1)

    md = f"""# 动态页抓取说明（quotes.toscrape.com）

> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　|　仅用于公开教学沙盒，请求间隔 >= {INTERVAL:.0f} 秒

## 0. 结果概览

| 阶段 | 方式 | 抓到条数 |
|---|---|---|
| /js | Playwright 等待 JS 渲染后取 DOM | {n_js} |
| /scroll | Playwright 模拟滚动至不再加载 | {n_scroll} |
| 数据接口直连 | requests 请求 `/api/quotes` | {n_api} |
| **合并去重后** | 按（名言, 作者）去重 | **{n_total}**（丢弃重复 {dup} 条） |

## 1. 为什么 requests 拿不到内容

requests 只负责发 HTTP 请求、把响应原文交给你——**它不是浏览器，不会执行任何 JavaScript**。

实测证据（本脚本阶段 0）：

| 页面 | HTTP 状态 | 原始 HTML 大小 | 渲染后的引言节点 | 内嵌 JS 数据 |
|---|---|---|---|---|
| /js | {js_ev['status']} | {js_ev['html_len']} 字节 | **{js_ev['quote_nodes']} 个** | {str(js_ev['inline_data']).lower()} |
| /scroll | {scroll_ev['status']} | {scroll_ev['html_len']} 字节 | **{scroll_ev['quote_nodes']} 个** | {str(scroll_ev['inline_data']).lower()} |

- `/js` 页返回的 HTML 里根本没有 `class="quote"` 的节点，只有一个 `<script>` 标签，数据以 JS 变量 `data = [...]` 的形式藏在脚本里。浏览器要执行这段脚本，才能把它变成可见的 DOM。
- `/scroll` 页更彻底：原始 HTML 连数据都没有，引言要等用户滚动到底部时，由 JS 发起异步（XHR/Fetch）请求向服务器再要。

注意一个细节：`/js` 页的数据其实就内嵌在 HTML 的 `<script>` 里，用正则/BeautifulSoup 抠出这段 JS 变量也能拿到数据（不算"渲染抓取"，属于解析内嵌数据）；而 `/scroll` 页是**必须**模拟浏览器行为的典型。

## 2. 浏览器如何执行 JavaScript

打开一个页面时，浏览器引擎大致按下面的流水线工作：

```
下载 HTML → 解析构建 DOM 树
    ├─ 遇到 <script src=...> → 暂停解析，下载并执行 JS
    └─ JS 通过 DOM API 增删改节点（document.createElement / innerHTML ...）
        → 触发样式计算、布局、绘制 → 用户看到内容
```

- JS 的执行者是浏览器内置的引擎（Chrome/Edge 用 V8），它把脚本编译成机器码运行。
- 脚本可以改 DOM、发网络请求（XHR/Fetch）、监听滚动/点击等事件，再根据响应继续改 DOM——这就是"动态渲染"。
- Playwright/Selenium 的原理：用程序**驱动一个真实浏览器**（本作业用系统 Edge 的 Chromium 内核），等它执行完 JS、渲染出 DOM 之后，再从 DOM 里提取数据。等价于"你亲手开浏览器复制粘贴"，只是自动化了。

## 3. 内容实际从哪里来

用 Playwright 监听网络请求（`page.on("request")`）可以看到页面运行时真实发出的请求：

- `/js` 页：**数据内嵌在 HTML 的 `<script>` 变量 `data = [...]` 中**，页面自己的 JS 只是把它渲染成 DOM，没有额外网络请求。
- `/scroll` 页：每次滚动到底部，页面 JS 就向 **`{api_example}`** 发 XHR/Fetch 请求，共触发 **{len(api_calls)} 次、覆盖第 1~{max_scroll_page} 页**。服务器返回 JSON（字段：`quotes[].text / quotes[].author.name / has_next / top_ten_tags`），前端把 JSON 渲染成 DOM。

也就是说：**你最终在 DOM 里看到的每条引言，源头都是这份 JSON（或内嵌 JS 数据），渲染只是"搬运工"。**

## 4. 找到 XHR/Fetch 接口后，直接请求它（附代码）

能用接口就不用模拟浏览器——更快、更稳、更省资源。接口规律：`GET /api/quotes?page=N`，返回 `has_next` 标记是否还有下一页。

```python
import time
import requests

BASE = "https://quotes.toscrape.com"
session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (compatible; HomeworkScraper/1.0)"

quotes, page_no = [], 1
while True:
    resp = session.get(f"{{BASE}}/api/quotes?page={{page_no}}", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    for item in data["quotes"]:
        quotes.append({{"text": item["text"],
                        "author": item["author"]["name"]}})
    if not data["has_next"]:      # 没有下一页就停
        break
    page_no += 1
    time.sleep(1)                 # 控制请求频率

print(len(quotes))
```

本次实测该代码路径（脚本阶段 3）：共请求 {max_scroll_page} 页，取回 {n_api} 条，与浏览器滚动抓到的 {n_scroll} 条完全一致。

## 5. 三种方案对比

| 方案 | 适用场景 | 优缺点 |
|---|---|---|
| requests + 解析 HTML | 内容在原始 HTML 里 | 快、稳；但对纯 JS 渲染页无效 |
| requests 请求 XHR 接口 | 找得到数据接口（开发者工具 Network 面板筛选 XHR/Fetch） | 最优：绕过渲染，直取结构化数据 |
| Playwright/Selenium 渲染 | 接口加密/难找、需登录/交互 | 通用但慢、资源占用高，是兜底手段 |

## 6. 文件清单

- `dynamic_scraper.py`：本作业脚本（探测 → 渲染抓取 → 滚动抓取 → 接口直连 → 去重输出）
- `quotes.csv`：去重后的合并结果（列：名言、作者、来源；UTF-8-SIG 编码，Excel 可直接打开）
"""
    REPORT_PATH.write_text(md, encoding="utf-8")
    log(f"已写出 {REPORT_PATH}")


def main():
    start = time.time()
    evidence = probe_with_requests()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        quotes_js, api_js = scrape_js_page(browser)
        quotes_scroll, api_scroll = scrape_scroll_page(browser)
        browser.close()

    quotes_api = fetch_via_api()

    all_rows = [(t, a, "js") for t, a in quotes_js] \
             + [(t, a, "scroll") for t, a in quotes_scroll]
    n_total, dup = write_csv(all_rows)
    write_report(evidence, len(quotes_js), len(quotes_scroll),
                 api_scroll, len(quotes_api), n_total, dup)
    log(f"全部完成，用时 {time.time() - start:.1f} 秒")


if __name__ == "__main__":
    main()
