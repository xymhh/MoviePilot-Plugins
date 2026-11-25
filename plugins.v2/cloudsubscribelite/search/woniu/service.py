"""蜗牛搜索服务。"""

from ..base import DelegatingSearchService


class WoniuSearchService(DelegatingSearchService):
    """蜗牛搜索门面服务，对接系统统一检索编排。"""

    source_key = "woniu"
