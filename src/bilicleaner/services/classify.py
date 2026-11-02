"""关注对象的分类：离线规则分类（21 类）+ 可选的 LLM 增强分类。

规则分类免费、离线、毫秒级，靠中文关键词、官方认证字段和用户分组三个信号
打分；LLM 分类是可选项（默认关闭），调用用户自备的 OpenAI 兼容接口
``POST {base_url}/chat/completions``，每批最多 20 条。LLM 失败时调用方应
退回规则分类，不允许因为模型不可用而让整个任务失败。

分类结果在 B 站接口里没有对应字段，因此由 :class:`CategoryCache` 落盘保存
（按账号隔离，原子替换写盘），只读接口返回关注列表时再合并回去。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Iterable

from ..const import CATEGORIES
from ..errors import BiliCleanerError

__all__ = [
    "CATEGORIES",
    "FALLBACK_CATEGORY",
    "CategoryCache",
    "LlmClassifier",
    "classify_item",
    "classify_rule",
]

FALLBACK_CATEGORY = "其他·难判断"

# 关键词表：类别 -> [(关键词, 权重)]。权重越大越"专有"，避免泛词误伤。
_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "游戏": [
        ("游戏", 2.0), ("电竞", 2.5), ("手游", 2.0), ("端游", 2.0), ("单机", 2.0),
        ("主机", 1.5), ("攻略", 1.2), ("解说", 1.0), ("实况", 1.5), ("速通", 2.0),
        ("steam", 2.0), ("switch", 1.5), ("ps5", 2.0), ("xbox", 1.5), ("fps", 1.2),
        ("王者荣耀", 3.0), ("英雄联盟", 3.0), ("lol", 2.5), ("原神", 3.0),
        ("崩坏", 3.0), ("星穹铁道", 3.0), ("明日方舟", 3.0), ("我的世界", 3.0),
        ("minecraft", 3.0), ("炉石", 2.5), ("csgo", 2.5), ("dota", 2.5),
        ("无畏契约", 2.5), ("valorant", 2.5), ("第五人格", 2.5), ("永劫无间", 2.5),
        ("光遇", 2.5), ("泰拉瑞亚", 2.5), ("游戏区", 2.0), ("玩家", 1.2),
    ],
    "动画·二次元": [
        ("动画", 2.0), ("动漫", 2.5), ("番剧", 2.5), ("二次元", 2.5), ("漫画", 2.0),
        ("手书", 2.5), ("mad", 2.0), ("新番", 2.5), ("追番", 2.5), ("声优", 2.0),
        ("galgame", 2.5), ("视觉小说", 2.0), ("轻小说", 2.0), ("同人", 1.8),
        ("cosplay", 2.0), ("coser", 2.0), ("手办", 2.0), ("模玩", 1.8),
        ("痛车", 2.5), ("动画区", 2.0), ("番", 1.2),
    ],
    "影视": [
        ("影视", 2.5), ("电影", 2.5), ("影评", 2.5), ("电视剧", 2.5), ("美剧", 2.5),
        ("日剧", 2.5), ("韩剧", 2.5), ("预告片", 2.0), ("混剪", 1.8), ("剪辑", 1.2),
        ("综艺", 2.0), ("剧情", 1.2), ("影视区", 2.5), ("解说电影", 3.0), ("剧集", 2.0),
    ],
    "科技数码": [
        ("科技", 2.5), ("数码", 2.5), ("手机", 2.0), ("华为", 2.0), ("小米", 2.0),
        ("苹果", 1.5), ("iphone", 2.5), ("安卓", 2.5), ("android", 2.5),
        ("笔记本", 2.0), ("装机", 2.5), ("cpu", 2.0), ("显卡", 2.5), ("gpu", 2.0),
        ("芯片", 2.0), ("半导体", 2.5), ("测评", 1.5), ("评测", 1.5), ("开箱", 1.5),
        ("硬件", 2.0), ("外设", 2.0), ("智能家居", 2.5), ("无人机", 2.0),
        ("科技区", 2.5), ("极客", 2.0), ("数码区", 2.5), ("新机", 1.8),
    ],
    "编程·AI": [
        ("编程", 3.0), ("代码", 2.0), ("python", 2.5), ("java", 2.0), ("c++", 2.5),
        ("前端", 2.5), ("后端", 2.5), ("算法", 2.0), ("数据结构", 2.5),
        ("人工智能", 3.0), ("ai", 1.5), ("机器学习", 3.0), ("深度学习", 3.0),
        ("大模型", 2.5), ("llm", 2.5), ("chatgpt", 2.5), ("github", 2.5),
        ("程序员", 2.5), ("开发者", 2.0), ("linux", 2.0), ("服务器", 1.8),
        ("运维", 2.0), ("数据库", 2.0), ("架构", 1.5), ("编程语言", 3.0),
        ("代码人生", 2.5), ("技术宅", 2.0),
    ],
    "科普·人文": [
        ("科普", 3.0), ("人文", 2.5), ("历史", 2.0), ("哲学", 2.5), ("文学", 2.0),
        ("社会学", 2.5), ("心理学", 2.5), ("博物", 2.5), ("自然", 1.5), ("宇宙", 2.0),
        ("天文", 2.5), ("地理", 2.0), ("物理", 2.0), ("化学", 2.0), ("生物", 1.8),
        ("考古", 2.5), ("文化", 1.8), ("读书", 1.8), ("社科", 2.5), ("科学", 2.0),
        ("未解之谜", 2.5), ("冷知识", 2.2), ("知识区", 1.5), ("纪录片", 2.0),
    ],
    "学习·教育": [
        ("学习", 2.5), ("教育", 2.5), ("考研", 3.0), ("高考", 3.0), ("四六级", 3.0),
        ("英语", 2.0), ("日语", 2.0), ("教学", 2.0), ("课程", 1.8), ("网课", 2.5),
        ("考试", 2.0), ("刷题", 2.0), ("笔记", 1.5), ("大学", 1.8), ("中学", 2.0),
        ("名师", 2.0), ("学习方法", 2.5), ("专升本", 2.5), ("考公", 2.5),
        ("法考", 2.5), ("教师", 1.5), ("讲解", 1.2), ("知识分享", 2.0),
    ],
    "音乐": [
        ("音乐", 2.5), ("歌曲", 2.0), ("翻唱", 2.5), ("原创", 1.5), ("乐队", 2.5),
        ("吉他", 2.0), ("钢琴", 2.0), ("小提琴", 2.5), ("演奏", 2.0), ("编曲", 2.5),
        ("混音", 2.0), ("说唱", 2.5), ("rap", 2.0), ("嘻哈", 2.5), ("电音", 2.5),
        ("dj", 2.0), ("音乐区", 2.5), ("歌手", 2.0), ("mv", 1.5), ("唱见", 2.5),
        ("v家", 2.5), ("vocaloid", 2.5), ("初音", 2.5), ("古风", 1.8), ("ost", 2.0),
    ],
    "舞蹈": [
        ("舞蹈", 3.0), ("跳舞", 2.5), ("街舞", 2.5), ("宅舞", 2.5), ("中国舞", 2.5),
        ("芭蕾", 2.5), ("爵士舞", 2.5), ("拉丁", 2.0), ("舞见", 2.5), ("编舞", 2.5),
        ("舞蹈区", 3.0), ("翻跳", 2.5),
    ],
    "美食": [
        ("美食", 3.0), ("吃播", 3.0), ("做饭", 2.5), ("菜谱", 2.5), ("厨房", 2.0),
        ("烹饪", 2.5), ("探店", 2.0), ("小吃", 2.0), ("家常菜", 2.5), ("烘焙", 2.5),
        ("甜品", 2.0), ("火锅", 2.0), ("拉面", 2.0), ("厨艺", 2.5), ("美食区", 3.0),
        ("料理", 2.0), ("下厨", 2.0),
    ],
    "生活·Vlog": [
        ("生活", 2.0), ("vlog", 2.5), ("日常", 1.8), ("记录", 1.2), ("旅行", 2.0),
        ("旅游", 2.0), ("校园", 1.8), ("宿舍", 2.0), ("租房", 2.0), ("装修", 2.0),
        ("亲子", 1.8), ("情感", 1.5), ("生活区", 2.5), ("独居", 2.0), ("打工", 1.5),
        ("上班", 1.5), ("通勤", 2.0), ("搬家", 2.0), ("城市", 1.2), ("分享", 1.0),
    ],
    "搞笑·娱乐": [
        ("搞笑", 3.0), ("沙雕", 3.0), ("鬼畜", 3.0), ("整活", 2.5), ("段子", 2.5),
        ("相声", 2.5), ("小品", 2.0), ("娱乐", 2.0), ("鬼畜区", 3.0), ("抽象", 2.0),
        ("挑战", 1.5), ("名场面", 2.0), ("meme", 2.0), ("梗", 1.8), ("笑", 1.2),
    ],
    "体育·健身": [
        ("体育", 2.5), ("健身", 3.0), ("篮球", 2.5), ("足球", 2.5), ("nba", 2.5),
        ("cba", 2.5), ("英超", 2.5), ("中超", 2.5), ("跑步", 2.0), ("马拉松", 2.5),
        ("瑜伽", 2.5), ("增肌", 2.5), ("减脂", 2.5), ("训练", 1.2), ("羽毛球", 2.5),
        ("乒乓球", 2.5), ("网球", 2.0), ("游泳", 2.0), ("骑行", 2.0), ("体育区", 2.5),
        ("运动", 1.5), ("球鞋", 1.8),
    ],
    "汽车·机械": [
        ("汽车", 3.0), ("车评", 3.0), ("试驾", 2.5), ("新能源", 2.0), ("燃油车", 2.0),
        ("摩托", 2.5), ("机车", 2.5), ("改装", 2.0), ("机械", 2.5), ("工程", 1.5),
        ("制造", 1.8), ("机床", 2.5), ("挖掘机", 2.5), ("汽车区", 3.0),
        ("特斯拉", 2.5), ("比亚迪", 2.5), ("发动机", 2.5), ("车", 1.0),
    ],
    "财经·职场": [
        ("财经", 3.0), ("股票", 2.5), ("基金", 2.5), ("投资", 2.5), ("理财", 2.5),
        ("经济", 2.0), ("金融", 2.5), ("商业", 2.0), ("创业", 2.0), ("职场", 2.5),
        ("求职", 2.5), ("面试", 2.0), ("简历", 2.0), ("副业", 2.5), ("赚钱", 2.0),
        ("保险", 1.8), ("房产", 2.0), ("商业模式", 2.5), ("财经区", 3.0), ("打工人", 2.0),
    ],
    "时事·社会": [
        ("时事", 3.0), ("新闻", 2.5), ("社会", 2.0), ("政治", 2.5), ("国际", 1.8),
        ("军事", 2.5), ("热点", 2.0), ("评论", 1.2), ("观察", 1.2), ("案件", 2.0),
        ("法制", 2.5), ("社会新闻", 3.0), ("俄乌", 2.5), ("军事区", 3.0), ("时评", 2.5),
    ],
    "时尚·美妆": [
        ("时尚", 2.5), ("美妆", 3.0), ("化妆", 2.5), ("护肤", 2.5), ("穿搭", 2.5),
        ("口红", 2.5), ("香水", 2.0), ("发型", 2.0), ("变美", 2.5), ("颜值", 1.5),
        ("时尚区", 2.5), ("美妆区", 3.0), ("服饰", 2.0), ("潮流", 2.0), ("美甲", 2.5),
    ],
    "宠物·动物": [
        ("宠物", 3.0), ("猫咪", 2.5), ("狗狗", 2.5), ("萌宠", 3.0), ("训犬", 2.5),
        ("仓鼠", 2.5), ("爬宠", 2.5), ("宠物区", 3.0), ("动物", 1.8), ("猫", 1.2),
        ("狗", 1.2), ("熊猫", 2.5), ("野生动物", 2.5),
    ],
    "手工·绘画·摄影": [
        ("手工", 3.0), ("绘画", 3.0), ("画画", 2.5), ("插画", 2.5), ("美术", 2.5),
        ("素描", 2.5), ("水彩", 2.5), ("油画", 2.5), ("板绘", 2.5), ("手绘", 2.5),
        ("摄影", 3.0), ("拍照", 2.0), ("相机", 2.2), ("修图", 2.0), ("粘土", 2.5),
        ("模型", 1.8), ("木工", 2.5), ("编织", 2.5), ("手作", 2.5), ("摄影区", 3.0),
        ("绘画区", 3.0), ("设计", 1.5),
    ],
    "虚拟UP主·VTuber": [
        ("vtuber", 4.0), ("虚拟主播", 4.0), ("虚拟up主", 4.0), ("虚拟偶像", 3.5),
        ("皮套", 3.0), ("中之人", 3.0), ("vup", 3.5), ("虚拟区", 3.5),
        ("hololive", 3.5), ("nijisanji", 3.5), ("绊爱", 3.5), ("星街", 3.0),
        ("嘉然", 3.5), ("阿梓", 3.0), ("七海", 2.5), ("虚拟主播区", 4.0),
    ],
}

# 用户分组名提示（分组是用户自己起的，信号较弱但很直接）
_GROUP_HINTS: list[tuple[str, str]] = [
    ("虚拟", "虚拟UP主·VTuber"),
    ("vtuber", "虚拟UP主·VTuber"),
    ("vup", "虚拟UP主·VTuber"),
    ("学习", "学习·教育"),
    ("考研", "学习·教育"),
    ("英语", "学习·教育"),
    ("教育", "学习·教育"),
    ("游戏", "游戏"),
    ("电竞", "游戏"),
    ("动漫", "动画·二次元"),
    ("二次元", "动画·二次元"),
    ("番剧", "动画·二次元"),
    ("影视", "影视"),
    ("电影", "影视"),
    ("追剧", "影视"),
    ("科技", "科技数码"),
    ("数码", "科技数码"),
    ("编程", "编程·AI"),
    ("技术", "编程·AI"),
    ("开发", "编程·AI"),
    ("ai", "编程·AI"),
    ("音乐", "音乐"),
    ("舞蹈", "舞蹈"),
    ("美食", "美食"),
    ("做饭", "美食"),
    ("生活", "生活·Vlog"),
    ("vlog", "生活·Vlog"),
    ("日常", "生活·Vlog"),
    ("搞笑", "搞笑·娱乐"),
    ("娱乐", "搞笑·娱乐"),
    ("体育", "体育·健身"),
    ("健身", "体育·健身"),
    ("汽车", "汽车·机械"),
    ("机械", "汽车·机械"),
    ("财经", "财经·职场"),
    ("职场", "财经·职场"),
    ("投资", "财经·职场"),
    ("时事", "时事·社会"),
    ("新闻", "时事·社会"),
    ("社会", "时事·社会"),
    ("美妆", "时尚·美妆"),
    ("时尚", "时尚·美妆"),
    ("宠物", "宠物·动物"),
    ("动物", "宠物·动物"),
    ("手工", "手工·绘画·摄影"),
    ("绘画", "手工·绘画·摄影"),
    ("摄影", "手工·绘画·摄影"),
    ("科普", "科普·人文"),
    ("人文", "科普·人文"),
    ("历史", "科普·人文"),
    ("读书", "科普·人文"),
]

# 官方认证文案里的额外提示（认证是强信号）
_OFFICIAL_HINTS: list[tuple[str, str]] = [
    ("歌手", "音乐"),
    ("演奏", "音乐"),
    ("演员", "影视"),
    ("导演", "影视"),
    ("编剧", "影视"),
    ("漫画家", "动画·二次元"),
    ("声优", "动画·二次元"),
    ("作家", "科普·人文"),
    ("学者", "科普·人文"),
    ("教授", "学习·教育"),
    ("老师", "学习·教育"),
    ("大学", "学习·教育"),
    ("运动员", "体育·健身"),
    ("奥运冠军", "体育·健身"),
    ("教练", "体育·健身"),
    ("主持人", "时事·社会"),
    ("记者", "时事·社会"),
    ("媒体", "时事·社会"),
    ("游戏主播", "游戏"),
    ("游戏UP主", "游戏"),
    ("科技UP主", "科技数码"),
    ("数码UP主", "科技数码"),
    ("动漫UP主", "动画·二次元"),
    ("动画UP主", "动画·二次元"),
    ("音乐UP主", "音乐"),
    ("舞蹈UP主", "舞蹈"),
    ("美食UP主", "美食"),
    ("生活UP主", "生活·Vlog"),
    ("搞笑UP主", "搞笑·娱乐"),
    ("娱乐UP主", "搞笑·娱乐"),
    ("体育UP主", "体育·健身"),
    ("健身UP主", "体育·健身"),
    ("汽车UP主", "汽车·机械"),
    ("财经UP主", "财经·职场"),
    ("科普UP主", "科普·人文"),
    ("知识UP主", "科普·人文"),
    ("影视UP主", "影视"),
    ("时尚UP主", "时尚·美妆"),
    ("美妆UP主", "时尚·美妆"),
    ("宠物UP主", "宠物·动物"),
    ("手工UP主", "手工·绘画·摄影"),
    ("摄影UP主", "手工·绘画·摄影"),
    ("虚拟UP主", "虚拟UP主·VTuber"),
    ("虚拟主播", "虚拟UP主·VTuber"),
    ("vup", "虚拟UP主·VTuber"),
    ("vtuber", "虚拟UP主·VTuber"),
    ("编程UP主", "编程·AI"),
    ("技术UP主", "编程·AI"),
]

_CATEGORY_INDEX = {name: index for index, name in enumerate(CATEGORIES)}

_TEXT_WEIGHT = 1.6  # 昵称 + 签名
_OFFICIAL_WEIGHT = 3.2  # 官方认证
_GROUP_WEIGHT = 2.6  # 用户分组


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _add_hits(scores: dict[str, float], text: str, weight: float, table: dict[str, list[tuple[str, float]]]) -> None:
    if not text:
        return
    for category, keywords in table.items():
        for keyword, base in keywords:
            if keyword in text:
                scores[category] = scores.get(category, 0.0) + base * weight


def classify_rule(item: dict) -> tuple[str, float]:
    """规则分类：返回 ``(类别, 置信度)``，类别一定在 :data:`CATEGORIES` 里。"""
    item = item if isinstance(item, dict) else {}
    scores: dict[str, float] = {}

    text = f"{_norm(item.get('name'))} {_norm(item.get('sign'))}"
    _add_hits(scores, text, _TEXT_WEIGHT, _KEYWORDS)

    official = _norm(item.get("official"))
    _add_hits(scores, official, _OFFICIAL_WEIGHT, _KEYWORDS)
    for keyword, category in _OFFICIAL_HINTS:
        if keyword in official:
            scores[category] = scores.get(category, 0.0) + 2.5

    groups = item.get("groups") or []
    if isinstance(groups, (list, tuple)):
        groups_text = " ".join(_norm(g) for g in groups)
        for keyword, category in _GROUP_HINTS:
            if keyword in groups_text:
                scores[category] = scores.get(category, 0.0) + _GROUP_WEIGHT

    if not scores:
        return FALLBACK_CATEGORY, 0.2

    # 先比分数，再按 CATEGORIES 的固定顺序打破平局，保证结果稳定
    best = max(scores, key=lambda cat: (scores[cat], -_CATEGORY_INDEX.get(cat, 999)))
    total = sum(scores.values()) or 1.0
    share = scores[best] / total
    confidence = min(0.95, 0.35 + 0.45 * share + 0.01 * min(scores[best], 12.0))
    return best, round(confidence, 2)


def classify_item(item: dict) -> dict:
    """分类单条 FollowItem，返回 ``{"category", "confidence", "category_source"}``。"""
    category, confidence = classify_rule(item)
    return {"category": category, "confidence": confidence, "category_source": "rule"}


# ---------------------------------------------------------------- LLM 分类


def _extract_json_array(content: str) -> list:
    """从模型输出里抠出 JSON 数组（容忍 ```json 包裹和前后废话）。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("模型输出里没有 JSON 数组")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("模型输出不是数组")
    return data


class LlmClassifier:
    """可选的 LLM 分类器（OpenAI 兼容 ``/chat/completions``）。"""

    def __init__(self, cfg: dict):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.api_key = str(cfg.get("api_key") or "")
        self.model = str(cfg.get("model") or "").strip()
        self.batch_size = max(1, min(int(cfg.get("batch_size") or 20), 20))
        self.timeout = float(cfg.get("timeout") or 60)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def classify(self, items: Iterable[dict]) -> dict[int, str]:
        """批量分类，返回 ``{mid: category}``；失败抛 :class:`BiliCleanerError`。

        调用方应捕获异常并退回 :func:`classify_rule`。
        """
        if not self.configured:
            raise BiliCleanerError("LLM 分类未配置（缺少 base_url 或 model）", code="LLM_NOT_CONFIGURED")
        item_list = [it for it in items if isinstance(it, dict)]
        result: dict[int, str] = {}
        for start in range(0, len(item_list), self.batch_size):
            chunk = item_list[start : start + self.batch_size]
            result.update(self._classify_batch(chunk))
        return result

    # ------------------------------------------------------------ 内部

    def _classify_batch(self, batch: list[dict]) -> dict[int, str]:
        payload = []
        for item in batch:
            try:
                mid = int(item.get("mid"))
            except (TypeError, ValueError):
                continue
            payload.append(
                {
                    "mid": mid,
                    "name": str(item.get("name") or "")[:40],
                    "sign": str(item.get("sign") or "")[:120],
                    "official": str(item.get("official") or "")[:60],
                    "groups": [str(g) for g in (item.get("groups") or [])][:6],
                }
            )
        if not payload:
            return {}

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 B 站关注列表分类器。把每个 UP 主归入下面固定类别之一，"
                    "只输出 JSON 数组，不要解释。类别列表："
                    + "、".join(CATEGORIES)
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            import requests

            url = f"{self.base_url}/chat/completions"
            resp = requests.post(url, json=body, headers=headers, timeout=self.timeout)
            if resp.status_code >= 400:
                raise BiliCleanerError(
                    f"LLM 接口返回 HTTP {resp.status_code}", code="LLM_FAILED"
                )
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            rows = _extract_json_array(content)
        except BiliCleanerError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/解析错误统一转成 LLM_FAILED
            raise BiliCleanerError(f"LLM 分类请求失败：{exc}", code="LLM_FAILED") from exc

        out: dict[int, str] = {}
        wanted = {row["mid"] for row in payload}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                mid = int(row.get("mid"))
            except (TypeError, ValueError):
                continue
            category = str(row.get("category") or "").strip()
            if mid in wanted and category in _CATEGORY_INDEX:
                out[mid] = category
        return out


# ---------------------------------------------------------------- 分类缓存


class CategoryCache:
    """关注对象分类结果的本地缓存（按账号隔离，原子替换）。"""

    _lock = threading.Lock()

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------ 路径

    def _account(self) -> str | None:
        """当前登录账号 mid；未登录返回 None。"""
        try:
            session = self.store.load_session() or {}
        except OSError:
            return None
        user = session.get("user") or {}
        mid = user.get("mid")
        return str(mid) if mid else None

    def path(self) -> str:
        account = self._account() or "anonymous"
        return os.path.join(self.store.data_dir, f"categories_{account}.json")

    # ------------------------------------------------------------ 读写

    def load(self) -> dict[str, dict]:
        with self._lock:
            data = self.store._read_json(self.path(), {})
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, dict) else {}

    def _save(self, items: dict[str, dict]) -> None:
        payload = {"mid": self._account(), "items": items}
        with self._lock:
            self.store._write_json(self.path(), payload)

    def get(self, mid: int | str) -> dict | None:
        return self.load().get(str(mid))

    def update(self, mapping: dict[int | str, str | tuple[str, str]], default_source: str = "rule") -> int:
        """批量写入分类结果，返回实际写入条数。"""
        if not mapping:
            return 0
        with self._lock:
            data = self.store._read_json(self.path(), {})
            items = data.get("items") if isinstance(data, dict) else {}
            if not isinstance(items, dict):
                items = {}
            written = 0
            for mid, value in mapping.items():
                if isinstance(value, tuple):
                    category, source = value[0], value[1]
                else:
                    category, source = value, default_source
                category = str(category or "").strip()
                if category not in _CATEGORY_INDEX:
                    continue
                items[str(mid)] = {"category": category, "source": str(source)}
                written += 1
            payload = {"mid": self._account(), "items": items}
            self.store._write_json(self.path(), payload)
        return written

    def clear(self) -> None:
        with self._lock:
            self.store._write_json(self.path(), {"mid": self._account(), "items": {}})
