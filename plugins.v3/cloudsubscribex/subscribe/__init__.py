"""自动订阅渠道集合。"""

# 导入子包即触发 provider 模块的 @register 注册
from . import anilist, bangumi, douban, maoyan, mikan, netflix, tmdb  # noqa: F401
