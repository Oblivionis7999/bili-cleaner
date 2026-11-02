"""全局常量：分类、任务类型、默认设置、B 站错误码。"""

APP_NAME = "BiliCleaner"
VERSION = "0.1.0"

# 规则分类用的固定类别（顺序即界面展示顺序）
CATEGORIES = [
    "游戏",
    "动画·二次元",
    "影视",
    "科技数码",
    "编程·AI",
    "科普·人文",
    "学习·教育",
    "音乐",
    "舞蹈",
    "美食",
    "生活·Vlog",
    "搞笑·娱乐",
    "体育·健身",
    "汽车·机械",
    "财经·职场",
    "时事·社会",
    "时尚·美妆",
    "宠物·动物",
    "手工·绘画·摄影",
    "虚拟UP主·VTuber",
    "其他·难判断",
]

TASK_TYPES = [
    "unfollow",
    "dynamic_delete",
    "favorite_clear",
    "favorite_clean_invalid",
    "toview_clear",
    "classify_llm",
]

TASK_STATUS = ["queued", "running", "paused", "cooldown", "stopped", "done", "failed"]

# 只有这些任务需要用户手打“确认词”
DESTRUCTIVE_CLEAR_TASKS = {"favorite_clear", "favorite_clean_invalid", "toview_clear"}

DYNAMIC_TYPE_LABELS = {
    "DYNAMIC_TYPE_FORWARD": "转发",
    "DYNAMIC_TYPE_DRAW": "图文",
    "DYNAMIC_TYPE_WORD": "文字",
    "DYNAMIC_TYPE_AV": "视频",
    "DYNAMIC_TYPE_ARTICLE": "专栏",
    "DYNAMIC_TYPE_LIVE_RCMD": "直播",
    "DYNAMIC_TYPE_LIVE": "直播",
    "DYNAMIC_TYPE_COMMON_SQUARE": "装扮",
    "DYNAMIC_TYPE_COMMON_VERTICAL": "竖屏",
    "DYNAMIC_TYPE_MUSIC": "音乐",
    "DYNAMIC_TYPE_PGC": "番剧",
    "DYNAMIC_TYPE_COURSES": "课程",
    "DYNAMIC_TYPE_COURSES_SEASON": "课程",
}

RISK_CODES = {-352, -412, -509, -799}
AUTH_CODES = {-101, -111}

DEFAULT_SETTINGS = {
    "delay_seconds": 2.5,
    "jitter_seconds": 0.8,
    "batch_size": 20,
    "batch_pause_seconds": 3.0,
    "cooldown_seconds": 600,
    "max_cooldowns": 2,
    "protect_mutual": True,
    "confirm_phrase": "清空",
    "backup_keep": 0,
    "llm": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "model": "",
        "batch_size": 20,
    },
}

DEFAULT_PORT = 8770


def merge_settings(base: dict, patch: dict) -> dict:
    """深合并设置（llm 是唯一嵌套对象）。"""
    out = dict(base)
    for k, v in (patch or {}).items():
        if k == "llm" and isinstance(v, dict):
            llm = dict(base.get("llm") or {})
            llm.update(v)
            out["llm"] = llm
        else:
            out[k] = v
    return out
