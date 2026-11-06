# -*- coding: utf-8 -*-
"""Playwright 浏览器采集器：适合 JS 渲染、有反爬或需登录的平台。

登录状态保存在 auth/<key>.json，先执行:
    python main.py login --platform <key>
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from base import BaseCollector, BookQuery, SearchResult, UA_POOL, log

AUTH_DIR = Path(__file__).resolve().parent.parent / "auth"
DEBUG_DIR = Path(__file__).resolve().parent.parent / "data" / "debug"


class BrowserCollector(BaseCollector):
    _pw = None  # 进程内共享 playwright 实例

    def __init__(self, site, delay=2.5, limit=None, headless=True):
        super().__init__(site, delay=delay, limit=limit, headless=headless)
        self._browser = None
        self._ctx = None

    async def _ensure(self):
        if self._ctx is not None:
            return
        if BrowserCollector._pw is None:
            from playwright.async_api import async_playwright
            BrowserCollector._pw = await async_playwright().start()
        self._browser = await BrowserCollector._pw.chromium.launch(
            headless=self.headless)
        kwargs = {"locale": "zh-CN", "user_agent": random.choice(UA_POOL)}
        state_file = AUTH_DIR / f"{self.site.key}.json"
        if state_file.exists():
            kwargs["storage_state"] = str(state_file)
        self._ctx = await self._browser.new_context(**kwargs)

    async def search(self, query: BookQuery) -> list[SearchResult]:
        await self._ensure()
        page = await self._ctx.new_page()
        try:
            url = self.site.search_url.format(kw=quote_plus(query.keyword))
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(random.uniform(1.0, 2.5))  # 等渲染 + 降频

            sel = self.site.selectors or {}
            item_sel = sel.get("item", "")
            if item_sel:
                try:
                    await page.wait_for_selector(item_sel, timeout=20_000)
                except Exception:  # noqa: BLE001
                    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                    fp = DEBUG_DIR / f"{self.site.key}_{int(time.time())}.png"
                    await page.screenshot(path=str(fp), full_page=True)
                    log.warning("[%s] 等待结果节点超时(%s)，截图: %s",
                                self.site.name, item_sel, fp)
            return await self._extract(page, query)
        finally:
            await page.close()

    async def _extract(self, page, query: BookQuery) -> list[SearchResult]:
        sel = self.site.selectors or {}
        item_sel = sel.get("item", "")
        if not item_sel:
            return []
        loc = page.locator(item_sel)
        n = await loc.count()
        if n == 0:
            log.warning("[%s] 共 0 条结果，请核对 sites.py 的选择器 / 是否出现验证码",
                        self.site.name)
        results: list[SearchResult] = []
        for i in range(min(n, self.limit)):
            el = loc.nth(i)

            async def grab(key: str) -> str:
                s = sel.get(key)
                if not s:
                    return ""
                try:
                    return (await el.locator(s).first
                            .inner_text(timeout=3000)).strip()
                except Exception:  # noqa: BLE001
                    return ""

            title = await grab("title")
            if not title:
                try:
                    title = (await el.inner_text()).strip().splitlines()[0][:100]
                except Exception:  # noqa: BLE001
                    title = ""
            if not title:
                continue

            link = ""
            try:
                ls = sel.get("link", "a")
                if ls:
                    link = await el.locator(ls).first.get_attribute(
                        "href", timeout=3000) or ""
            except Exception:  # noqa: BLE001
                pass

            results.append(SearchResult(
                platform=self.site.key,
                keyword=query.keyword,
                title=title,
                price=await grab("price"),
                url=urljoin(page.url, link),
                seller=await grab("seller"),
            ))
        return results

    async def close(self):
        for closer in (getattr(self, "_ctx", None),
                       getattr(self, "_browser", None)):
            try:
                if closer:
                    await closer.close()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = self._browser = None


async def interactive_login(site, headless: bool = False):
    """打开有头浏览器让用户手动登录，之后保存登录状态供采集复用。"""
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=headless)
    ctx = await browser.new_context(locale="zh-CN",
                                    user_agent=random.choice(UA_POOL))
    page = await ctx.new_page()
    await page.goto(site.login_url or site.search_url.split("{")[0],
                    wait_until="domcontentloaded")
    await asyncio.to_thread(
        input, f"请在打开的浏览器中完成【{site.name}】登录，"
               f"确认登录成功后回到此终端按回车保存...")
    state = await ctx.storage_state()
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    fp = AUTH_DIR / f"{site.key}.json"
    fp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                  encoding="utf-8")
    print(f"登录状态已保存: {fp}")
    await browser.close()
    await pw.stop()
