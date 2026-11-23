"""预置指标（2026-10-19 口径，用到 2026-11）。与自定义指标同一结构。"""
from .config import PLAY_FROM, FALLBACK_FROM

# 播放侧 from：对齐回收文档 F4XsdE1W 附「指标体系」——剔除 no_results_recommend（兜底推荐不是搜索结果带来的播放）
F3 = ["from", "in", [f for f in PLAY_FROM if f != FALLBACK_FROM]]
NF = ["from", "!=", FALLBACK_FROM]
DETAIL_UV = {"event": "search_detail_expose", "agg": "uv", "filters": []}

PRESETS = [
    ("ctr_ui", {
        "name": "结果页点击渗透率", "kind": "ratio", "group": "北极星",
        "note": "search_card_click UV（排兜底）÷ search_detail_expose UV",
        "numerator": {"event": "search_card_click", "agg": "uv", "filters": [NF]},
        "denominator": DETAIL_UV, "numerator_within_denominator": True}),
    ("expose_conv", {
        "name": "界面曝光 → 卡片曝光 UV 转化", "kind": "ratio", "group": "过程",
        "note": "观测项：排序策略不应明显影响",
        "numerator": {"event": "search_card_expose", "agg": "uv", "filters": [NF]},
        "denominator": DETAIL_UV, "numerator_within_denominator": True}),
    ("click_3s", {
        "name": "点击 → 3s 播放 UV 转化", "kind": "ratio", "group": "过程",
        "note": "点过卡且有 3s 播放（is_3s_valid_play 或 play_duration≥3000）的 UV ÷ 点卡 UV；播放 from ∈ search/search_related/search_related_feed",
        "numerator": {"event": "play_close", "agg": "uv",
                      "filters": [F3, ["any", [["is_3s_valid_play", "=", "true"], ["play_duration", ">=", "3000"]]]]},
        "denominator": {"event": "search_card_click", "agg": "uv", "filters": [NF]},
        "numerator_within_denominator": True}),
    ("vv_per_user", {
        "name": "人均 VV（P99 封顶）", "kind": "ratio", "group": "过程",
        "note": "Σ min(单设备单日 play_start_ready, P99) ÷ search_detail_expose UV；P99 = 开窗前 7 天单设备单日，未播放按 0",
        "numerator": {"event": "play_start_ready", "agg": "pv", "filters": [F3]},
        "denominator": DETAIL_UV, "numerator_within_denominator": True, "cap": "p99_pre7d"}),
    ("noresult_rate", {
        "name": "无结果率", "kind": "ratio", "group": "护栏", "lower_is_better": True,
        "note": "search_no_results UV ÷ search_detail_expose UV（设备级；旧版本前无 from）",
        "numerator": {"event": "search_no_results", "agg": "uv", "filters": []},
        "denominator": DETAIL_UV, "numerator_within_denominator": True}),
    ("completion", {
        "name": "完播率（PV，阈值 90%）", "kind": "ratio", "group": "护栏",
        "note": "play_close 中 play_duration ≥ 0.9×video_duration 的条数 ÷ video_duration>0 的条数",
        "numerator": {"event": "play_close", "agg": "pv",
                      "filters": [F3, ["video_duration", ">", "0"], ["play_duration", ">=", "0.9*video_duration"]]},
        "denominator": {"event": "play_close", "agg": "pv", "filters": [F3, ["video_duration", ">", "0"]]}}),
    ("avg_vdur", {
        "name": "平均 video_duration（ms）", "kind": "ratio", "group": "护栏",
        "note": "配合完播率看：防排序偏向短视频把完播率机械抬高",
        "numerator": {"event": "play_close", "agg": "sum:video_duration", "filters": [F3, ["video_duration", ">", "0"]]},
        "denominator": {"event": "play_close", "agg": "pv", "filters": [F3, ["video_duration", ">", "0"]]}}),
    ("startup_ms", {
        "name": "起播耗时 P50 / P90（ms）", "kind": "quantile", "group": "护栏", "lower_is_better": True,
        "note": "play_start_ready.use_time，剔除 0；只出数值不出 CI",
        "source": {"event": "play_start_ready", "filters": [F3, ["use_time", ">", "0"]]},
        "column": "use_time", "quantiles": [0.5, 0.9]}),
    ("fresh_share", {
        "name": "新内容曝光份额（近 30 天发布，PV）", "kind": "ratio", "group": "过程",
        "note": "时效实验主判据。search_card_expose（排兜底）中视频 publish_time 在曝光当天往前 30 天内的条数 ÷ 卡片曝光条数（PV）；"
                "关联内容表 CONTENT_TABLE（video_id），关联不到或 publish_time 为空计入分母、不计分子；"
                "口径见 README 指标说明",
        "numerator": {"event": "search_card_expose", "agg": "pv", "filters": [NF, ["publish_age_days", "<=", "30"]]},
        "denominator": {"event": "search_card_expose", "agg": "pv", "filters": [NF]}}),
]
