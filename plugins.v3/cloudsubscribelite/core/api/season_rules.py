"""季号展示规则（纯函数，便于单测）。

问题背景：TMDB 只有第 1 季的剧集，媒体库快照里却残留第 2-5 季（早期按绝对集号
拆分），旧实现把两者并集展示，页面就凭空多出几季和几百集“缺失”。
"""



def _select_season_numbers(
        official_counts: dict[int, int], library_episodes: dict[int, set]
) -> list[int]:
    """决定要展示的季号：官方（TMDB 等）有季信息时以官方为准。

    媒体库残留的季（早期按绝对集号拆出来的第 2-5 季）官方没有定义时不展示，
    否则会凭空多出几季和几百集“缺失”。官方完全无季信息时才退回媒体库。
    """
    numbers = set(official_counts) if official_counts else set(library_episodes)
    return sorted(numbers)


def _season_total_episodes(official: int, library_episodes) -> int:
    """单季总集数：官方集数优先，官方没有时退回媒体库里最大集号。"""
    official = int(official or 0)
    if official > 0:
        return official
    return max(library_episodes or set(), default=0)
