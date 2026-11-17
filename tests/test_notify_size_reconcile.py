"""通知体积校正旧契约已删除（v1.13.1，Phase 2 设计 §3.3）。

此前本文件固定「以历史台账为准」的回填契约（apply_history_size /
history_episode_size：事后从 history 里补体积）。v1.13.1 起 transfer_details
在创建时就带真实体积（从转存结果按集累加），回填不再是契约——6 个回填
单测随之删除；回填代码本身（notify_size.py）留到 v1.13.2 再删。

新契约见 tests/test_transfer_details_size_1_13_1.py：_append 按集对齐
file_names / episode_sizes / size_unknown、_aggregate 逐集校验修正汇总、
卡片在体积未知时不显示大小数字。
"""
