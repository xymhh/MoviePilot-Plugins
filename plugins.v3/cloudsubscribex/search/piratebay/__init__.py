"""海盗湾 (The Pirate Bay) 搜索渠道。"""

from .client import PirateBayClient, PirateBayError
from .service import PirateBaySearchService

__all__ = [
    "PirateBayClient",
    "PirateBayError",
    "PirateBaySearchService",
]
