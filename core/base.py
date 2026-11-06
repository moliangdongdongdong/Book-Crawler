# -*- coding: utf-8 -*-
"""数据模型与采集器基类：统一的搜索入口 + 重试 + 同平台串行限速。"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("collector")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]


@dataclass
class BookQuery:
    """一次搜索请求：keyword 为实际搜索词。"""
    keyword: str
    title: str = ""
    author: str = ""
    isbn: str = ""


@dataclass
class SearchResult:
    """一条采集结果。"""
    platform: str
    keyword: str
    title: str
    price: str = ""
    url: str = ""
    seller: str = ""
    extra: str = ""
    scraped_at: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def price_num(self):
        m = re.search(r"\d+(?:\.\d+)?", (self.price or "").replace(",", ""))
        return float(m.group()) if m else None


class BaseCollector:
    """所有平台采集器基类。"""

    def __init__(self, site, delay: float = 2.5, limit: int | None = None,
                 headless: bool = True):
        self.site = site
        self.delay = delay
        self.limit = limit or site.item_limit
        self.headless = headless
        self.lock = asyncio.Lock()  # 同一平台串行执行，避免触发反爬

    async def search(self, query: BookQuery) -> list[SearchResult]:
        raise NotImplementedError

    async def search_with_retry(self, query: BookQuery,
                                retries: int = 3) -> list[SearchResult]:
        for attempt in range(1, retries + 1):
            try:
                return await self.search(query)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 第%d次搜索失败 %s: %s",
                            self.site.name, attempt, query.keyword, e)
                if attempt == retries:
                    raise
                await asyncio.sleep(2 * attempt + random.random() * 2)

    @staticmethod
    def clean_price(text: str) -> str:
        return (text or "").strip()
