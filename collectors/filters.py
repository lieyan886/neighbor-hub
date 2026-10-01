"""信息采集用的入库前过滤器：去重、黑白名单、空值校验。"""
from __future__ import annotations

from dataclasses import dataclass

from core.models import Item
from core.repository import items


@dataclass
class FilterResult:
    """过滤结果：留下的条目 + 被丢掉的条目及原因，方便 UI 逐条解释。"""

    kept: list[Item]
    dropped: list[tuple[Item, str]]

    @property
    def kept_count(self) -> int:
        return len(self.kept)


class ItemFilter:
    """按配置对采集结果做筛选。

    规则顺序：必填校验 -> 黑名单 -> 白名单 -> 指纹去重。
    """

    def __init__(
        self,
        blacklist: list[str] | None = None,
        whitelist: list[str] | None = None,
        dedupe: bool = True,
    ) -> None:
        self.blacklist = [w.strip() for w in (blacklist or []) if w.strip()]
        self.whitelist = [w.strip() for w in (whitelist or []) if w.strip()]
        self.dedupe = dedupe

    def check(self, item: Item) -> str | None:
        """返回丢弃原因，返回 None 表示通过。"""
        if not item.title.strip():
            return "标题为空"
        haystack = f"{item.title} {item.summary} {item.merchant}"
        for bad in self.blacklist:
            if bad in haystack:
                return f"命中黑名单「{bad}」"
        if self.whitelist:
            if not any(good in haystack for good in self.whitelist):
                return "未命中白名单"
        if self.dedupe and item.source_hash and items.hash_exists(item.source_hash):
            return "库里已有（重复）"
        return None

    def apply(self, candidates: list[Item]) -> FilterResult:
        kept: list[Item] = []
        dropped: list[tuple[Item, str]] = []
        seen: set[str] = set()
        for it in candidates:
            reason = self.check(it)
            if reason is None:
                if self.dedupe and it.source_hash:
                    if it.source_hash in seen:
                        reason = "本批次内重复"
                    else:
                        seen.add(it.source_hash)
            if reason is None:
                kept.append(it)
            else:
                dropped.append((it, reason))
        return FilterResult(kept=kept, dropped=dropped)
