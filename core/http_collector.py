# -*- coding: utf-8 -*-
"""HTTP 直连采集器：适合服务端渲染、无强反爬的平台。"""
from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from base import BaseCollector, BookQuery, SearchResult, UA_POOL, log

DEBUG_DIR = Path(__file__).resolve().parent.parent / "data" / "debug"


class HttpCollector(BaseCollector):
    async def search(self, query: BookQuery) -> list[SearchResult]:
        url = self.site.search_url.format(kw=quote_plus(query.keyword))
        html = await self._fetch(url)
        return self._parse(url, html, query)

    async def _fetch(self, url: str) -> str:
        headers = {
            "User-Agent": random.choice(UA_POOL),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            **self.site.headers,
        }
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(follow_redirects=True,
                                             timeout=30) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    if self.site.encoding:
                        return resp.content.decode(self.site.encoding,
                                                   errors="ignore")
                    return resp.text
            except Exception as e:  # noqa: BLE001
                last_err = e
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"HTTP 请求失败: {url} ({last_err})")

    def _parse(self, page_url: str, html: str,
               query: BookQuery) -> list[SearchResult]:
        soup = BeautifulSoup(html, "lxml")
        sel = self.site.selectors or {}
        item_sel = sel.get("item", "")
        items = soup.select(item_sel) if item_sel else []

        if not items:
            # 保存页面快照，便于校准选择器
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            fp = DEBUG_DIR / f"{self.site.key}_{int(time.time())}.html"
            fp.write_text(html, encoding="utf-8", errors="ignore")
            log.warning("[%s] 未解析到结果节点(%s)，页面快照: %s",
                        self.site.name, item_sel, fp)

        parts = urlsplit(page_url)
        base = f"{parts.scheme}://{parts.netloc}"
        results: list[SearchResult] = []
        for it in items[: self.limit]:

            def grab(key: str) -> str:
                s = sel.get(key)
                if not s:
                    return ""
                node = it.select_one(s)
                return node.get_text(" ", strip=True) if node else ""

            link = ""
            ls = sel.get("link", "a")
            lnode = it.select_one(ls) if ls else None
            if lnode and lnode.get("href"):
                link = urljoin(base + "/", lnode["href"])

            title = grab("title") or it.get_text(" ", strip=True)[:100]
            if not title:
                continue
            results.append(SearchResult(
                platform=self.site.key,
                keyword=query.keyword,
                title=title,
                price=grab("price"),
                url=link,
                seller=grab("seller"),
            ))
        return results
