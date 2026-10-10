# -*- coding: utf-8 -*-
"""
books.toscrape.com 全站爬虫（课程作业一）
- 遵守 robots.txt（实测 404，无限制），请求间隔 >= 1 秒
- 列表页：catalogue/page-1.html ... page-50.html（自动翻到最后一页）
- 详情页：提取精确库存数字 In stock (N available)
- 清洗：价格 -> float（去货币符号）、评分 -> int(1-5)、库存 -> int
- 链接补全为绝对 URL
- 产出：books.csv + validation_report.txt
"""
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://books.toscrape.com/"
CATALOGUE = urljoin(BASE, "catalogue/")
OUT_DIR = Path(__file__).resolve().parent
CSV_PATH = OUT_DIR / "books.csv"
REPORT_PATH = OUT_DIR / "validation_report.txt"
CACHE_PATH = OUT_DIR / "stock_cache.json"

UA = "Mozilla/5.0 (compatible; HomeworkScraper/1.0; educational use)"
RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
INTERVAL = 1.0  # 秒：相邻请求最小间隔

session = requests.Session()
session.headers.update({"User-Agent": UA})
_last_request_ts = 0.0


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get(url, retries=3):
    """带重试的 GET，且保证与上一次请求间隔 >= 1 秒。"""
    global _last_request_ts
    last_err = ""
    for attempt in range(1, retries + 1):
        if _last_request_ts:
            gap = time.time() - _last_request_ts
            if gap < INTERVAL:
                time.sleep(INTERVAL - gap)
        try:
            resp = session.get(url, timeout=30)
            _last_request_ts = time.time()
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = repr(exc)
        if attempt < retries:
            backoff = 2 * attempt
            log(f"  ! 请求失败({last_err}) {url}，{backoff}s 后第 {attempt + 1} 次重试")
            time.sleep(backoff)
    raise RuntimeError(f"抓取失败 {url}: {last_err}")


def parse_listing(html):
    """解析列表页，返回 (books, has_next)。"""
    soup = BeautifulSoup(html, "html.parser")
    books = []
    for art in soup.select("article.product_pod"):
        a = art.h3.a
        title = (a.get("title") or "").strip()
        link = urljoin(CATALOGUE, a.get("href", ""))

        price_txt = art.select_one("p.price_color").get_text(strip=True)
        m = re.search(r"[0-9]+(?:\.[0-9]+)?", price_txt)
        price = float(m.group()) if m else None

        cls = art.select_one("p.star-rating").get("class", [])
        rating = next((RATING_MAP[c] for c in cls if c in RATING_MAP), None)

        books.append({
            "书名": title,
            "价格": price,
            "评分": rating,
            "库存": None,  # 稍后从详情页补
            "详情页链接": link,
        })
    has_next = soup.select_one("li.next a") is not None
    return books, has_next


def parse_detail_stock(html):
    """从详情页提取库存数字；无货返回 0，无法判定返回 None。"""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("p.instock.availability")
    if not node:
        return None
    txt = node.get_text(strip=True)
    m = re.search(r"([0-9]+)\s+available", txt)
    if m:
        return int(m.group(1))
    low = txt.lower()
    if "in stock" in low:
        return 0
    if "out of stock" in low:
        return 0
    return None


def phase1_listings():
    """抓取所有列表页。"""
    log("阶段 1：抓取列表页 ...")
    all_books = []
    per_page_counts = []
    page = 1
    while True:
        url = urljoin(CATALOGUE, f"page-{page}.html")
        html = get(url)
        books, has_next = parse_listing(html)
        if not books:
            log(f"  第 {page} 页解析到 0 条，判定到达终点")
            break
        for b in books:
            b["页码"] = page
        all_books.extend(books)
        per_page_counts.append(len(books))
        log(f"  第 {page} 页: {len(books)} 条（累计 {len(all_books)}）next={has_next}")
        if not has_next:
            break
        page += 1
    log(f"列表页完成：共 {len(per_page_counts)} 页，{len(all_books)} 条")
    return all_books, per_page_counts


def load_cache():
    if CACHE_PATH.exists():
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def phase2_stock(books):
    """逐本访问详情页补库存；带缓存，断点可续跑。"""
    log("阶段 2：抓取详情页库存（约 1000 个请求，每请求间隔 1 秒）...")
    cache = load_cache()
    todo = [b for b in books if b["详情页链接"] not in cache]
    log(f"  缓存命中 {len(books) - len(todo)}，待抓 {len(todo)}")
    for i, b in enumerate(books, 1):
        url = b["详情页链接"]
        if url not in cache:
            try:
                cache[url] = parse_detail_stock(get(url))
            except RuntimeError as e:
                log(f"  ! 详情页失败，库存置空: {e}")
                cache[url] = None
            if i % 20 == 0:
                save_cache(cache)
        b["库存"] = cache[url]
        if i % 50 == 0:
            log(f"  详情页进度 {i}/{len(books)}")
    save_cache(cache)
    log("详情页库存抓取完成")


def is_null(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def write_csv(books):
    fields = ["书名", "价格", "评分", "库存", "详情页链接"]
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for b in books:
            row = {}
            for k in fields:
                v = b.get(k)
                if v is None:
                    row[k] = ""
                elif k == "价格":
                    row[k] = f"{float(v):.2f}"  # 金额统一保留两位小数
                else:
                    row[k] = v
            writer.writerow(row)
    log(f"已写出 {CSV_PATH}")


def write_report(books, per_page_counts):
    fields = ["书名", "价格", "评分", "库存", "详情页链接"]
    total = len(books)
    null_counts = {k: sum(1 for b in books if is_null(b.get(k))) for k in fields}

    links = [b["详情页链接"] for b in books]
    dup_links = total - len(set(links))
    bad_links = sum(1 for u in links if not u.startswith("https://books.toscrape.com/"))

    # 漏页检查：页码应连续 1..N，每页 20 条
    pages = sorted({b["页码"] for b in books})
    expected = list(range(1, pages[-1] + 1)) if pages else []
    missing_pages = [p for p in expected if p not in pages]
    short_pages = [
        (i + 1, c) for i, c in enumerate(per_page_counts) if c != 20
    ]

    lines = []
    lines.append("=" * 60)
    lines.append("books.toscrape.com 全站爬取 · 数据校验报告")
    lines.append("=" * 60)
    lines.append(f"生成时间        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"目标站点        : {BASE}")
    lines.append(f"robots.txt      : HTTP 404（站点未提供 robots.txt，无抓取限制）")
    lines.append(f"请求间隔        : >= {INTERVAL:.0f} 秒/次")
    lines.append("")
    lines.append(f"总页数          : {len(per_page_counts)}")
    lines.append(f"总条数          : {total}（目标约 1000）")
    lines.append("")
    lines.append("每页条数:")
    for i, c in enumerate(per_page_counts, 1):
        mark = "" if c == 20 else "  <-- 异常"
        lines.append(f"  第 {i:>2} 页: {c} 条{mark}")
    lines.append("")
    lines.append("空值统计:")
    for k in fields:
        lines.append(f"  {k:<6}: {null_counts[k]} 个空值")
    lines.append("")
    lines.append(f"重复详情页链接数: {dup_links}")
    lines.append(f"非法/不完整链接 : {bad_links}（应以 https://books.toscrape.com/ 开头）")
    lines.append("")
    lines.append("数值字段清洗检查:")
    prices = [b["价格"] for b in books if isinstance(b["价格"], (int, float))]
    ratings = [b["评分"] for b in books if isinstance(b["评分"], int)]
    stocks = [b["库存"] for b in books if isinstance(b["库存"], int)]
    lines.append(f"  价格: 数字 {len(prices)}/{total}，范围 "
                 f"{min(prices):.2f} ~ {max(prices):.2f}")
    lines.append(f"  评分: 数字 {len(ratings)}/{total}，取值 "
                 f"{sorted(set(ratings))} (1-5)")
    lines.append(f"  库存: 数字 {len(stocks)}/{total}，范围 "
                 f"{min(stocks)} ~ {max(stocks)}")
    lines.append("")
    lines.append("漏页检查:")
    lines.append(f"  页码连续性     : page-1 .. page-{pages[-1] if pages else 0}，"
                 f"缺失页 {missing_pages if missing_pages else '无'}")
    lines.append(f"  每页条数异常页 : {short_pages if short_pages else '无（全部 20 条）'}")
    if total < 1000:
        lines.append(f"  ⚠ 总条数 {total} < 1000，可能存在漏页，请检查上方页码连续性！")
    else:
        lines.append(f"  ✓ 总条数 {total}，与站点声明的 1000 本一致，无漏页")
    lines.append("")
    lines.append("前 5 行预览:")
    header = f"  {'序号':<4}{'书名':<42}{'价格':>8}{'评分':>4}{'库存':>6}  详情页链接"
    lines.append(header)
    for i, b in enumerate(books[:5], 1):
        title = b["书名"]
        title = (title[:40] + "..") if len(title) > 42 else title
        lines.append(
            f"  {i:<4}{title:<42}{b['价格']:>8.2f}{b['评分']:>4}{b['库存']:>6}  {b['详情页链接']}"
        )
    lines.append("=" * 60)

    report = "\n".join(lines)
    REPORT_PATH.write_text(report, encoding="utf-8")
    log(f"已写出 {REPORT_PATH}")
    print("\n" + report)


def main():
    start = time.time()
    log(f"开始抓取 {BASE}")
    books, per_page_counts = phase1_listings()
    phase2_stock(books)
    write_csv(books)
    write_report(books, per_page_counts)
    log(f"全部完成，用时 {(time.time() - start) / 60:.1f} 分钟")


if __name__ == "__main__":
    main()
