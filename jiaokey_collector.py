# -*- coding: utf-8 -*-
"""教科网(jiaokey.com)图书采集器。

验证码弹窗实际结构（2026-10 勘察）：
  标题「访问验证」+ 提示「请输入图片中的字符以继续。」
  验证码图片 + 输入框 + [验证] [取消] 按钮
  检测策略：按文本内容定位弹窗，不依赖 class 名。

连续弹窗处理：
  一次进入 _handle_captcha 会循环处理所有连续弹窗：
    - 检测到弹窗 → 自动识别 / 人工兜底
    - 处理成功后等 2 秒，看是否有下一个
    - 连续 6 秒无新弹窗 → 返回成功
    - 失败或连续弹窗超 10 个 → 返回失败

采集前必须登录：
  python jiaokey_collector.py --login

依赖：pip install ddddocr

过滤规则：仅按"书名完全一致"入库。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import async_playwright

try:
    import ddddocr
    _HAS_DDDDOCR = True
except ImportError:
    _HAS_DDDDOCR = False

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
SHOTS = DATA / "jiaokey_shots"
DB = DATA / "jiaokey.db"
AUTH = BASE / "auth" / "jiaokey.json"
PLATFORM = "教科网"

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

SEARCH_URL = "https://www.jiaokey.com/book/search/?q={kw}&page={page}"
NO_RESULT_MARKS = ("没有找到", "暂无", "无结果")

# 单个弹窗的自动识别重试次数（失败后刷新验证码再试）
AUTO_CAPTCHA_RETRY = 8
# 一次进入连续弹窗最多处理多少个（防止死循环）
MAX_POPUP_PER_SESSION = 10
# 处理完一个弹窗后，安静多久没有新弹窗就认为结束
POPUP_QUIET_SECONDS = 6.0
# 累计多少次会话失败就整体暂停采集
CAPTCHA_FAIL_THRESHOLD = 3

log = logging.getLogger("jiaokey")


# ================================================================
# ddddocr
# ================================================================
_ocr_instance = None


def _get_ocr():
    global _ocr_instance
    if not _HAS_DDDDOCR:
        return None
    if _ocr_instance is None:
        _ocr_instance = ddddocr.DdddOcr(show_ad=False)
        log.info("  ddddocr 模型已加载")
    return _ocr_instance


# ================================================================
# 验证码检测（按文本定位，打 data-jk-* 标记）
# ================================================================
_MARK_CAPTCHA_JS = r"""
() => {
  // 清除旧标记
  for (const attr of ['data-jk-captcha', 'data-jk-captcha-img',
                      'data-jk-captcha-input', 'data-jk-captcha-submit']) {
    document.querySelectorAll('[' + attr + ']').forEach(e => {
      e.removeAttribute(attr);
    });
  }

  const TEXT_RE = /访问验证|请输入图片中|请输入.{0,10}字符|请输入.{0,6}图片/;

  const candidates = [];
  const all = document.querySelectorAll(
    'div, section, form, dialog, aside, article, main');
  for (const el of all) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden'
        || style.opacity === '0') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 150 || rect.height < 100) continue;
    const text = el.textContent || '';
    if (!TEXT_RE.test(text)) continue;

    const inputs = el.querySelectorAll(
      'input:not([type="hidden"]):not([type="checkbox"])' +
      ':not([type="radio"]):not([type="submit"])' +
      ':not([type="button"]):not([type="file"])');
    let visInput = null;
    for (const inp of inputs) {
      if (inp.offsetWidth > 0 && inp.offsetHeight > 0) {
        visInput = inp; break;
      }
    }
    if (!visInput) continue;
    candidates.push({ el, visInput, area: rect.width * rect.height });
  }

  if (candidates.length === 0) {
    const inputs = document.querySelectorAll(
      'input[placeholder*="验证码"], input[placeholder*="字符"], ' +
      'input[name*="captcha"], input[id*="captcha"], ' +
      'input[name*="verify"], input[placeholder*="图片"]');
    for (const inp of inputs) {
      if (inp.offsetWidth <= 0 || inp.offsetHeight <= 0) continue;
      candidates.push({
        el: inp.parentElement || inp, visInput: inp, area: 999999
      });
      break;
    }
  }

  if (candidates.length === 0) return { found: false };
  candidates.sort((a, b) => a.area - b.area);
  const { el: container, visInput } = candidates[0];

  container.setAttribute('data-jk-captcha', '1');
  visInput.setAttribute('data-jk-captcha-input', '1');

  const imgs = Array.from(container.querySelectorAll('img'))
    .filter(i => i.offsetWidth >= 40 && i.offsetHeight >= 15);
  if (imgs.length > 0) {
    imgs.sort((a, b) =>
      (a.offsetWidth * a.offsetHeight) - (b.offsetWidth * b.offsetHeight));
    imgs[0].setAttribute('data-jk-captcha-img', '1');
  }

  const btns = Array.from(container.querySelectorAll(
    'button, [role="button"], input[type="submit"], input[type="button"], ' +
    'a.btn, a.button, [class*="btn"], [class*="button"]'))
    .filter(b => b.offsetWidth > 0 && b.offsetHeight > 0);

  let submitBtn = null;
  for (const b of btns) {
    const t = (b.textContent || b.value || '').trim();
    if (/^(验证|确定|提交|确认|继续|OK)$/i.test(t)) {
      submitBtn = b; break;
    }
  }
  if (!submitBtn) {
    for (const b of btns) {
      const t = (b.textContent || b.value || '').trim();
      if (/验证|确定|提交|确认/.test(t) && !/取消|关闭|返回|重置/.test(t)) {
        submitBtn = b; break;
      }
    }
  }
  if (submitBtn) submitBtn.setAttribute('data-jk-captcha-submit', '1');

  return {
    found: true,
    hasImg: imgs.length > 0,
    hasInput: true,
    hasSubmit: !!submitBtn,
    buttonTexts: btns.map(b => (b.textContent || b.value || '').trim())
                    .filter(Boolean)
  };
}
"""

# 检查页面上是否出现验证码错误的提示
_CHECK_ERROR_JS = r"""
() => {
  const t = document.body.textContent || '';
  return /验证码错误|验证失败|请重新输入|输入有误|验证码不正确/.test(t);
}
"""


async def _detect_captcha(page_obj) -> dict:
    try:
        return await page_obj.evaluate(_MARK_CAPTCHA_JS)
    except Exception as e:  # noqa: BLE001
        log.debug("  验证码检测异常: %s", e)
        return {"found": False}


# ================================================================
# 单次自动识别
# ================================================================
async def _solve_captcha_auto(page_obj, args) -> bool:
    """单次：截图 → 识别 → 填 → 提交。返回 True=通过。"""
    ocr = _get_ocr()
    if ocr is None:
        return False

    info = await _detect_captcha(page_obj)
    if not info.get("found"):
        return True
    if not info.get("hasImg"):
        log.warning("  弹窗已找到但未定位到验证码图片，按钮: %s",
                    info.get("buttonTexts"))
        return False

    try:
        img_el = await page_obj.query_selector("[data-jk-captcha-img]")
        input_el = await page_obj.query_selector("[data-jk-captcha-input]")
        submit_el = await page_obj.query_selector("[data-jk-captcha-submit]")
    except Exception as e:  # noqa: BLE001
        log.warning("  定位元素失败: %s", e)
        return False

    if not img_el or not input_el:
        log.warning("  图片/输入框句柄为空")
        return False

    try:
        img_bytes = await img_el.screenshot()
    except Exception as e:  # noqa: BLE001
        log.warning("  验证码截图失败: %s", e)
        return False

    try:
        result = ocr.classification(img_bytes)
    except Exception as e:  # noqa: BLE001
        log.warning("  ddddocr 识别异常: %s", e)
        return False

    if not result or not result.strip():
        log.warning("  识别结果为空")
        return False

    result = result.strip()
    log.info("  🔍 识别: %s", result)

    # 填输入框
    try:
        await input_el.click()
        await asyncio.sleep(0.2)
        await input_el.fill("")
        await asyncio.sleep(0.1)
        await input_el.type(result, delay=random.randint(40, 90))
    except Exception:
        try:
            await input_el.fill(result)
        except Exception as e:  # noqa: BLE001
            log.warning("  填入失败: %s", e)
            return False

    await asyncio.sleep(0.5)

    # 提交
    clicked = False
    if submit_el:
        try:
            await submit_el.click()
            clicked = True
        except Exception as e:  # noqa: BLE001
            log.warning("  点击验证按钮失败: %s", e)
    if not clicked:
        try:
            await input_el.press("Enter")
            clicked = True
        except Exception:  # noqa: BLE001
            pass
    if not clicked:
        log.warning("  提交失败")
        return False

    # 等结果（识别错时尽快返回，让外层刷新重试）
    await asyncio.sleep(2)
    for i in range(5):
        if not (await _detect_captcha(page_obj)).get("found"):
            log.info("  ✓ 验证码通过")
            return True
        try:
            err = await page_obj.evaluate(_CHECK_ERROR_JS)
        except Exception:  # noqa: BLE001
            err = False
        if err:
            log.info("  页面提示验证码错误")
            return False
        await asyncio.sleep(1.5)
    return False


async def _refresh_captcha(page_obj):
    """点击验证码图片刷新。"""
    try:
        img_el = await page_obj.query_selector("[data-jk-captcha-img]")
        if img_el:
            await img_el.click()
            await asyncio.sleep(1.2)
    except Exception:  # noqa: BLE001
        pass


# ================================================================
# 连续弹窗处理
# ================================================================
async def _handle_captcha(page_obj, args) -> bool:
    """循环处理所有连续弹窗，直到安静 POPUP_QUIET_SECONDS 秒。

    关键：一次进入会处理多个连续弹窗，处理完一个后等 2 秒看是否还有下一个。
    """
    handled = 0
    last_popup = time.time()

    while handled < MAX_POPUP_PER_SESSION:
        detected = (await _detect_captcha(page_obj)).get("found")

        if not detected:
            # 没有弹窗，检查已经安静多久
            if time.time() - last_popup > POPUP_QUIET_SECONDS:
                if handled:
                    log.info("  ✓ 本次共处理 %d 个验证码弹窗", handled)
                return True
            await asyncio.sleep(1.0)
            continue

        # 有新弹窗
        handled += 1
        last_popup = time.time()
        log.warning("=" * 60)
        log.warning("⚠ 检测到第 %d 个验证码弹窗", handled)

        solved = await _solve_one_popup(page_obj, args)
        if not solved:
            log.warning("  ✗ 第 %d 个弹窗未能解决", handled)
            return False

        # 成功后等 2 秒看是否有下一个
        await asyncio.sleep(2.0)
        last_popup = time.time()

    log.warning("  连续弹窗数超过 %d，暂停本次处理", MAX_POPUP_PER_SESSION)
    return False


async def _solve_one_popup(page_obj, args) -> bool:
    """处理单个弹窗：自动识别（含刷新重试） → 人工兜底。"""
    # ---- 阶段1：自动识别 ----
    if args.auto_captcha and _HAS_DDDDOCR:
        for attempt in range(1, AUTO_CAPTCHA_RETRY + 1):
            log.info("  自动识别 %d/%d", attempt, AUTO_CAPTCHA_RETRY)
            if await _solve_captcha_auto(page_obj, args):
                return True
            # 弹窗可能已被异步处理掉
            if not (await _detect_captcha(page_obj)).get("found"):
                return True
            if attempt < AUTO_CAPTCHA_RETRY:
                await _refresh_captcha(page_obj)
                await asyncio.sleep(1.5)
        log.warning("  自动识别 %d 次全部失败", AUTO_CAPTCHA_RETRY)

    # ---- 阶段2：人工兜底 ----
    if not args.headed:
        log.warning("  headless 模式无法人工输入，请加 --headed")
        return False

    log.warning("👉 请人工输入验证码")
    loop = asyncio.get_running_loop()
    ans = await loop.run_in_executor(
        None, lambda: input("完成后回车继续，放弃请输入 n: "))
    if ans.strip().lower() == "n":
        return False
    await asyncio.sleep(3)
    for _ in range(4):
        if not (await _detect_captcha(page_obj)).get("found"):
            log.info("  ✓ 人工验证通过")
            return True
        await asyncio.sleep(2)
    log.warning("  ✗ 人工验证未通过")
    return False


# ================================================================
# 书单加载
# ================================================================
def load_booklist(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        sys.exit(f"书单文件不存在: {path}")
    queries: list[dict] = []
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header, cols = None, {}
        for row in rows:
            vals = [str(c).strip() if c is not None else "" for c in row]
            if any(vals):
                header = vals
                break
        if header:
            for idx, name in enumerate(header):
                for key, aliases in (("keyword", ("关键词", "关键字")),
                                     ("title", ("书名", "标题", "名称")),
                                     ("author", ("作者",)),
                                     ("publisher", ("出版社", "出版者")),
                                     ("isbn", ("isbn", "条码", "书号"))):
                    if any(a in name.lower() for a in aliases):
                        cols.setdefault(key, idx)
        for row in rows:
            vals = [str(c).strip() if c is not None else "" for c in row]

            def get(k):
                return vals[cols[k]] if k in cols and cols[k] < len(vals) else ""

            kw = get("keyword") or get("title") or get("isbn")
            if kw:
                queries.append({"keyword": kw, "title": get("title"),
                                "author": get("author"),
                                "publisher": get("publisher")})
        wb.close()
    else:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append({"keyword": line, "title": "",
                                "author": "", "publisher": ""})
    seen, uniq = set(), []
    for q in queries:
        if q["keyword"] not in seen:
            seen.add(q["keyword"])
            uniq.append(q)
    return uniq


# ================================================================
# 匹配辅助
# ================================================================
def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:30] or "unnamed"


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


MAX_PER_BOOK = 10


def _price_num(price: str):
    m = re.search(r"(\d+(?:\.\d+)?)", (price or "").replace(",", ""))
    return float(m.group(1)) if m else None


# ================================================================
# 存储
# ================================================================
class JiaokeyStore:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(DB))
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT, shop_name TEXT, shop_entity TEXT, title TEXT,
            price TEXT, price_num REAL, shop_url TEXT, item_url TEXT,
            item_shot TEXT, entity_shot TEXT, scraped_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_kw ON results(keyword);
        CREATE TABLE IF NOT EXISTS progress(
            keyword TEXT PRIMARY KEY, pages_done INTEGER DEFAULT 0,
            status TEXT, updated_at TEXT);
        """)
        self.conn.commit()

    def pages_done(self, keyword: str) -> int:
        row = self.conn.execute(
            "SELECT pages_done FROM progress WHERE keyword=?",
            (keyword,)).fetchone()
        return row[0] if row else 0

    def set_pages_done(self, keyword: str, page: int):
        self.conn.execute(
            "INSERT INTO progress(keyword,pages_done,status,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(keyword) DO UPDATE SET "
            "pages_done=excluded.pages_done, updated_at=excluded.updated_at",
            (keyword, page, "collecting",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()

    def save_items(self, keyword: str, items: list[dict],
                   expect: str = "") -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        added = 0
        expect_n = _norm(expect) or _norm(keyword)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM results WHERE keyword=?",
            (keyword,)).fetchone()[0]
        for it in items:
            if count >= MAX_PER_BOOK:
                break
            title_n = _norm(it.get("title", ""))
            if not title_n or title_n != expect_n:
                continue
            m = re.search(r"[¥￥]\s*\d+(?:\.\d+)?", it.get("price", ""))
            price_clean = m.group().replace(" ", "") if m else it.get("price", "")
            dup = self.conn.execute(
                "SELECT 1 FROM results WHERE keyword=? AND item_url=?",
                (keyword, it["url"])).fetchone()
            if dup:
                continue
            self.conn.execute(
                "INSERT INTO results(keyword,shop_name,shop_entity,title,"
                "price,price_num,shop_url,item_url,scraped_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (keyword, it.get("shop", ""), it.get("entity", ""),
                 it.get("title", ""), price_clean, _price_num(price_clean),
                 it.get("shop_url", ""), it["url"], now))
            count += 1
            added += 1
        self.conn.commit()
        return added

    def pending_shots(self) -> dict:
        rows = self.conn.execute(
            "SELECT id, keyword, title, item_url, shop_url, shop_entity "
            "FROM results WHERE item_url != '' AND item_shot IS NULL "
            "ORDER BY keyword, id").fetchall()
        grouped: dict[str, list] = {}
        for r in rows:
            grouped.setdefault(r[1], []).append(r)
        return grouped

    def update_shot(self, row_id: int, item_shot: str, entity: str,
                    entity_shot: str):
        self.conn.execute(
            "UPDATE results SET item_shot=?, shop_entity=?, entity_shot=? "
            "WHERE id=?", (item_shot, entity, entity_shot, row_id))
        self.conn.commit()

    def export_excel(self, out: Path) -> Path:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "采集结果"
        ws.append(["平台名称", "店铺名称", "店铺主体", "商品名称", "售价",
                   "店铺链接", "商品链接", "商品截图", "主体截图"])
        rows = self.conn.execute(
            "SELECT shop_name, shop_entity, title, price, shop_url, "
            "item_url, COALESCE(item_shot,''), COALESCE(entity_shot,'') "
            "FROM results ORDER BY keyword, id").fetchall()
        for r in rows:
            ws.append([PLATFORM] + list(r))
        widths = [14, 24, 26, 36, 10, 28, 30, 36, 36]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
        out.parent.mkdir(parents=True, exist_ok=True)
        wb.save(out)
        return out

    def stats(self):
        total = self.conn.execute(
            "SELECT COUNT(*) FROM results").fetchone()[0]
        shot = self.conn.execute(
            "SELECT COUNT(item_shot) FROM results").fetchone()[0]
        kws, pages = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pages_done),0) "
            "FROM progress").fetchone()
        return total, shot, kws or 0, pages or 0


# ================================================================
# 搜索
# ================================================================
async def search_one_page(ctx, keyword: str, page: int,
                          args) -> list[dict] | None:
    page_obj = await ctx.new_page()
    try:
        url = SEARCH_URL.format(kw=quote_plus(str(keyword)), page=page)
        await page_obj.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # 首次检测：处理所有连续弹窗
        if (await _detect_captcha(page_obj)).get("found"):
            ok = await _handle_captcha(page_obj, args)
            if not ok:
                return None
            # 处理完毕，重新加载搜索页（可能弹窗通过后需要重载才能进入正常页）
            try:
                await page_obj.goto(url, wait_until="domcontentloaded",
                                    timeout=60_000)
                await asyncio.sleep(2)
            except Exception:  # noqa: BLE001
                pass
            # 重载后可能又出现弹窗，再处理一轮
            if (await _detect_captcha(page_obj)).get("found"):
                ok = await _handle_captcha(page_obj, args)
                if not ok:
                    return None
                # 通过后重载
                try:
                    await page_obj.goto(url, wait_until="domcontentloaded",
                                        timeout=60_000)
                    await asyncio.sleep(2)
                except Exception:  # noqa: BLE001
                    pass

        # 等待正常结果渲染
        deadline = asyncio.get_running_loop().time() + 25
        items: list[dict] = []
        while True:
            await asyncio.sleep(2.0)
            try:
                body_text = await page_obj.inner_text("body")
            except Exception:  # noqa: BLE001
                continue

            # 途中再次检测弹窗（可能又连续弹了）
            if (await _detect_captcha(page_obj)).get("found"):
                ok = await _handle_captcha(page_obj, args)
                if not ok:
                    return None
                continue

            items = await _extract_items(page_obj)
            if items:
                break

            if any(mark in body_text for mark in NO_RESULT_MARKS):
                log.info("  确认无结果")
                return []
            if asyncio.get_running_loop().time() > deadline:
                break

        if not items:
            log.warning("  未提取到商品，keyword=%s page=%d", keyword, page)
        return items
    finally:
        await page_obj.close()


async def _extract_items(page_obj) -> list[dict]:
    EXTRACT_JS = r"""
    () => {
      const links = Array.from(document.querySelectorAll(
        'a[href*="/book/detail/"]'));
      const items = [];
      const seen = new Set();
      for (const a of links) {
        const m = a.href.match(/\/book\/detail\/(\d+)\.html/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        let title = (a.textContent || '').replace(/\s+/g, ' ').trim();
        if (!title || title.length < 2 || /^(查看|详情|购买|更多)/.test(title)) {
          let el = a;
          for (let i = 0; i < 6 && el; i++) {
            el = el.parentElement;
            if (!el) break;
            const inner = el.querySelectorAll('a[href*="/book/detail/"]');
            const ids = new Set();
            for (const l of inner) {
              const mm = l.href.match(/\/book\/detail\/(\d+)\.html/);
              if (mm) ids.add(mm[1]);
            }
            if (ids.size === 1) {
              const h = el.querySelector(
                'h1,h2,h3,h4,h5,.title,.name,.book-name,.bookname,strong,b');
              if (h) {
                const t = (h.textContent || '').replace(/\s+/g, ' ').trim();
                if (t && t.length >= 2 && !/^(查看|详情|购买|更多)/.test(t)) {
                  title = t;
                }
              }
              const pt = (el.textContent || '');
              const pm = pt.match(/[¥￥]\s*\d+(?:\.\d+)?/);
              const price = pm ? pm[0].replace(/\s/g, '') : '';
              if (title && title.length >= 2) {
                items.push({ title, url: a.href, price });
              }
              break;
            }
          }
        } else {
          let el = a;
          let price = '';
          for (let i = 0; i < 5 && el; i++) {
            el = el.parentElement;
            if (!el) break;
            const pt = (el.textContent || '');
            const pm = pt.match(/[¥￥]\s*\d+(?:\.\d+)?/);
            if (pm) { price = pm[0].replace(/\s/g, ''); break; }
          }
          items.push({ title, url: a.href, price });
        }
      }
      return items;
    }
    """
    try:
        raw = await page_obj.evaluate(EXTRACT_JS)
    except Exception as e:  # noqa: BLE001
        log.warning("  JS 提取失败: %s", e)
        return []

    items: list[dict] = []
    for r in raw or []:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        if not title or len(title) < 2 or not url:
            continue
        price = (r.get("price") or "").strip()
        items.append({
            "title": title, "url": url, "price": price,
            "shop": "教科网自营", "shop_url": "",
            "entity": "教科网自营",
        })
    return items


# ================================================================
# 登录
# ================================================================
async def login_flow():
    AUTH.parent.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context(locale="zh-CN", user_agent=UA_POOL[0])
    page = await ctx.new_page()

    await page.goto("https://www.jiaokey.com/", wait_until="domcontentloaded")
    print("=" * 60)
    print("浏览器已打开。请完成登录（账号密码 / 手机验证码均可）。")
    print("脚本每 3 秒自动检测一次，最长等 5 分钟。")
    print("=" * 60)

    ok = False
    last_tip = 0
    for i in range(100):
        await asyncio.sleep(3)
        try:
            html = await page.content()
        except Exception:  # noqa: BLE001
            continue
        if any(k in html for k in ("退出登录", "退出", "我的订单",
                                   "个人中心", "会员中心", "我的账户")):
            ok = True
            break
        if i - last_tip >= 10:
            print(f"  仍在等待登录… ({i * 3} 秒)")
            last_tip = i

    if not ok:
        loop = asyncio.get_running_loop()
        ans = await loop.run_in_executor(
            None, lambda: input("未自动检测到登录成功。"
                                "若已登录请按回车确认，放弃请输入 n："))
        ok = ans.strip().lower() != "n"

    if not ok:
        print("✗ 已放弃登录")
        await browser.close()
        await pw.stop()
        sys.exit(1)

    await asyncio.sleep(2)
    await ctx.storage_state(path=str(AUTH))
    await browser.close()
    await pw.stop()
    print("✓ 登录态已保存到", AUTH)


# ================================================================
# 截图
# ================================================================
async def _goto_wait(page_obj, url: str, selector: str, timeout: int):
    try:
        await page_obj.goto(url, timeout=timeout,
                            wait_until="domcontentloaded")
    except Exception:  # noqa: BLE001
        pass
    try:
        await page_obj.wait_for_selector(selector, timeout=10_000)
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(random.uniform(1.0, 2.0))


async def shot_item(ctx, row) -> str:
    page_obj = await ctx.new_page()
    try:
        await _goto_wait(page_obj, row[3],
                         "h1, .book-title, .detail-title, body", 25_000)
        fp = SHOTS / _safe_name(row[1]) / f"{row[0]:05d}_商品.png"
        fp.parent.mkdir(parents=True, exist_ok=True)
        await page_obj.screenshot(path=str(fp), full_page=True,
                                  timeout=60_000)
        return str(fp)
    except Exception as e:  # noqa: BLE001
        log.warning("  商品页截图失败 id=%s: %s", row[0], e)
        return ""
    finally:
        await page_obj.close()


# ================================================================
# 主流程
# ================================================================
async def run(args):
    if args.clean:
        if DB.exists():
            DB.unlink()
        log.info("已清空历史数据")

    if not AUTH.exists():
        log.error("=" * 60)
        log.error("未找到登录态 %s", AUTH)
        log.error("请先运行: python jiaokey_collector.py --login")
        log.error("=" * 60)
        return

    store = JiaokeyStore()
    queries = load_booklist(args.input)
    if args.limit:
        queries = queries[: args.limit]

    if args.auto_captcha and _HAS_DDDDOCR:
        captcha_mode = "自动识别（ddddocr）"
        _get_ocr()
    elif args.auto_captcha and not _HAS_DDDDOCR:
        captcha_mode = "ddddocr 未安装，降级人工"
        log.warning("未安装 ddddocr，请运行: pip install ddddocr")
    else:
        captcha_mode = "人工输入"

    log.info("书单: %d 本书 | 平台: 教科网（已登录，仅按书名匹配入库）",
             len(queries))
    log.info("验证码处理: %s | 连续弹窗安静阈值: %.1fs",
             captcha_mode, POPUP_QUIET_SECONDS)

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=not args.headed)
    ctx = await browser.new_context(
        locale="zh-CN", user_agent=random.choice(UA_POOL),
        viewport={"width": 1440, "height": 900},
        storage_state=str(AUTH))

    try:
        if not args.shots_only and not args.export_only:
            consec_fail = 0
            captcha_fails = 0
            for q in queries:
                done = 0 if args.redo else store.pages_done(q["keyword"])
                if done >= args.pages and not args.redo:
                    continue
                log.info("搜索: %s (第%d~%d页)", q["keyword"], done + 1,
                         args.pages)
                for pno in range(done + 1, args.pages + 1):
                    try:
                        items = await search_one_page(ctx, q["keyword"], pno,
                                                      args)
                    except Exception as e:  # noqa: BLE001
                        consec_fail += 1
                        log.error("  page=%d 采集异常(%d连败): %s",
                                  pno, consec_fail, e)
                        items = []

                    if items is None:
                        captcha_fails += 1
                        log.warning("  验证码处理失败（累计 %d 次）",
                                    captcha_fails)
                        if captcha_fails >= CAPTCHA_FAIL_THRESHOLD:
                            log.error("=" * 60)
                            log.error("验证码连续 %d 次无法通过，暂停采集。",
                                      captcha_fails)
                            log.error("请重新运行: "
                                      "python jiaokey_collector.py --login")
                            log.error("=" * 60)
                            return
                        await asyncio.sleep(args.delay * 2)
                        break

                    captcha_fails = 0
                    added = store.save_items(
                        q["keyword"], items,
                        expect=q.get("title") or q["keyword"])
                    store.set_pages_done(q["keyword"], pno)
                    consec_fail = 0
                    log.info("  page=%d -> %d 条(书名匹配新增%d)",
                             pno, len(items), added)
                    if not items:
                        break

                    if consec_fail >= 3:
                        log.warning("  连续失败 3 次，立即停止")
                        return
                    await asyncio.sleep(
                        args.delay * (0.8 + random.random() * 0.8))

        if not args.no_shots and not args.export_only:
            pending = store.pending_shots()
            shot_fail = 0
            for kw, rows in pending.items():
                todo = rows[: args.shot_limit]
                if not todo:
                    continue
                log.info("截图: %s (%d 条)", kw, len(todo))
                for row in todo:
                    res = await shot_item(ctx, row)
                    store.update_shot(row[0], res, row[5], "")
                    log.info("  id=%d %s | 商品图:%s",
                             row[0], (row[2] or "")[:24],
                             "√" if res else "×")
                    if not res:
                        shot_fail += 1
                    else:
                        shot_fail = 0
                    if shot_fail >= 3:
                        log.warning("  截图连续失败 3 次，立即停止")
                        return
                    await asyncio.sleep(
                        args.delay * (0.6 + random.random() * 0.6))

        total, shot, kws, pages = store.stats()
        log.info("数据库统计: %d 个关键词 / %d 页 / %d 条结果 / %d 条已截图",
                 kws, pages, total, shot)
        if not args.no_export:
            out = DATA / ("教科网采集结果_%s.xlsx"
                          % datetime.now().strftime("%Y%m%d_%H%M%S"))
            path = store.export_excel(out)
            log.info("已导出 Excel: %s", path)
    finally:
        await browser.close()
        await pw.stop()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="教科网图书采集器")
    ap.add_argument("--input", default="book.xlsx")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shot-limit", type=int, default=5)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no-shots", action="store_true")
    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("--shots-only", action="store_true")
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--no-auto-captcha", dest="auto_captcha",
                    action="store_false",
                    help="禁用验证码自动识别（默认启用）")
    args = ap.parse_args()
    try:
        if args.login:
            asyncio.run(login_flow())
            return
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中断。进度已保存，重新运行同一命令即可续采。")


if __name__ == "__main__":
    main()