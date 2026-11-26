"""页面与API适配。"""

from .account import AccountApi
from .channels import ChannelsApi
from .checkin import CheckinApi
from .config import ConfigApi
from .doctor import DoctorApi
from .page import PageApi
from .qrcode import QRCodeService
from .redirect import RedirectApi
from .registration import MoviePilotRegistration
from .runtime import RuntimeApi
from .search import SearchApi
from .sync import SyncApi
from .tg import TgMonitorApi

__all__ = [
    "AccountApi",
    "ChannelsApi",
    "CheckinApi",
    "ConfigApi",
    "DoctorApi",
    "MoviePilotRegistration",
    "PageApi",
    "QRCodeService",
    "RedirectApi",
    "RuntimeApi",
    "SearchApi",
    "SyncApi",
    "TgMonitorApi",
]
