"""页面与API适配。"""

from .account import AccountApi
from .checkin import CheckinApi
from .config import ConfigApi
from .history import HistoryApi
from .duplicate import DuplicateApi
from .ledger import LedgerApi
from .media_library import MediaLibraryApi
from .organize import OrganizeApi
from .page import PageApi
from .qrcode import QRCodeService
from .registration import MoviePilotRegistration
from .runtime import RuntimeApi
from .search import SearchApi
from .sync import SyncApi
from .tg import TgMonitorApi

__all__ = [
    "AccountApi",
    "CheckinApi",
    "ConfigApi",
    "HistoryApi",
    "DuplicateApi",
    "LedgerApi",
    "MediaLibraryApi",
    "OrganizeApi",
    "MoviePilotRegistration",
    "PageApi",
    "QRCodeService",
    "RuntimeApi",
    "SearchApi",
    "SyncApi",
    "TgMonitorApi",
]
