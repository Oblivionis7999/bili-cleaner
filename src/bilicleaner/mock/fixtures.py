"""确定性 Mock 数据：模拟一个真实用了一段时间的 B 站账号。

数据规模和字段严格对齐 ``docs/API-CONTRACT.md``：

- 2000 条关注，其中 40 个互关；分成 6 个分组；21 个分类都有覆盖
- 300 条动态：转发 / 图文 / 视频 / 纯文字 / 专栏 混合
- 3 个收藏夹：默认收藏夹 500 条 + 2 个自建收藏夹各 150 条，含少量失效条目
- 120 条稍后再看

实现约定：

- 只依赖 ``seed``。同一个 seed 生成的数据完全一致（便于快照测试和 UI 演示）。
- 所有时间戳都是 Unix 秒；纯内存生成，不读写文件、不联网。
- 数据是"看起来像真的"的假数据，昵称、签名等均为合成内容。
"""

from __future__ import annotations

import random
from typing import Any

from ..const import CATEGORIES

DEFAULT_SEED = 42

# 数据规模（测试与文档直接引用这些常量，避免散落魔法数字）
FOLLOW_COUNT = 2000
MUTUAL_COUNT = 40
DYNAMIC_COUNT = 300
TOVIEW_COUNT = 120
DEFAULT_FAV_COUNT = 500
SELF_FAV_COUNTS = (150, 150)
GROUP_NAMES = ["学习", "工作", "游戏", "生活", "音乐", "影视"]

# 固定的时间边界（Unix 秒）
_TS_2019 = 1546300800  # 2019-01-01
_TS_2024 = 1704067200  # 2024-01-01
_TS_2025_06 = 1748736000  # 2025-06-01

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_HEX = "0123456789abcdef"

# ---------------------------------------------------------------------------
# 昵称素材：前缀 + 核心 + 后缀 组合，保证 2000 个不重样且像真实 UP 主
# ---------------------------------------------------------------------------

_NAME_PREFIXES = [
    "一只", "爱做饭的", "爱看番的", "摸鱼的", "认真的", "隔壁老", "深夜", "来自北方的",
    "不务正业的", "会飞的", "加班的", "每天都在", "想睡觉的", "退休的", "野生的",
    "半路出家的", "佛系的", "硬核的", "暴躁的", "温柔的", "在逃的", "迷路的",
    "爱吃甜的", "不吃香菜的", "戴眼镜的", "会画画的", "会弹琴的", "打游戏的",
    "看星星的", "养猫的", "养狗的", "种花的", "修电脑的", "写代码的", "做视频的",
    "搬砖的", "读研的", "考公的", "上班摸鱼的", "不加班的小", "凌晨四点的",
]

_NAME_CORES = [
    "老王", "老周", "老陈", "老李", "老张", "老刘", "老杨", "老赵", "老孙", "老吴",
    "小王", "小李", "小张", "小陈", "小周", "小刘", "小杨", "小赵", "小孙", "小吴",
    "小林", "小徐", "小马", "小朱", "小胡", "小郭", "小何", "小高", "小罗", "小郑",
    "小梁", "小谢", "小宋", "小唐", "小许", "小韩", "小冯", "小邓", "小曹", "小彭",
    "小曾", "小田", "小董", "小袁", "小潘", "小蒋", "小蔡", "小余", "小杜", "小程",
    "小苏", "小魏", "小吕", "小丁", "小任", "小沈", "小姚", "小卢", "小姜", "小崔",
    "阿强", "阿杰", "阿泽", "阿岚", "阿木", "阿哲", "阿吉", "阿凯", "阿豪", "阿宁",
    "阿信", "阿卷", "阿柴", "阿橘", "阿狸", "阿兔", "阿熊", "阿猫", "阿鱼", "阿鸟",
    "柚子", "年糕", "团子", "土豆", "西瓜", "布丁", "白粥", "青柠", "西柚", "橘子",
    "海盐", "麦芽", "芝麻", "汤圆", "豆包", "小鹿", "大熊", "柴犬", "橘猫", "狸花",
    "企鹅", "海豹", "熊猫", "树懒", "水獭", "狐狸", "刺猬", "考拉", "羊驼", "河马",
    "章鱼", "水母", "鲸鱼", "鲨鱼", "老鹰", "猫头鹰", "小满", "小寒", "大暑", "白露",
    "秋分", "冬至", "立春", "谷雨", "芒种", "北岛", "南风", "西窗", "东坡", "青川",
    "望舒", "朝颜", "夕雾", "云雀", "山雀", "雨燕", "雪鸮", "沙鸥", "野鹤", "白鹭",
]

_NAME_SUFFIXES = [
    "", "", "", "", "", "的日常", "日记", "Vlog", "频道", "研究所", "实验室", "工作室",
    "影评", "说电影", "聊数码", "测评", "游戏厅", "小课堂", "音乐站", "食堂", "观察",
    "实测", "TV", "Official", "君", "酱", "频道", "放映厅", "杂货铺", "便利店", "电台",
    "手帐", "画室", "琴房", "书房", "厨房", "旅店", "照相馆", "小卖部", "修理铺",
]

_EN_PREFIXES = [
    "Pixel", "Neon", "Retro", "Turbo", "Nano", "Cyber", "Lunar", "Solar", "Echo",
    "Byte", "Flux", "Orbit", "Lumen", "Momo", "Kira", "Yuki", "Hana", "Sora", "Riku",
    "Mikan",
]
_EN_CORES = [
    "Lab", "Cat", "Fork", "Wave", "Byte", "Pilot", "Neko", "Studio", "Works", "Diner",
    "Notes", "Daily", "Craft", "Quest", "Tales", "Club",
]
_EN_SUFFIXES = ["", "", "Official", "TV", "_", "君"]

# 少量"像百大 UP"的合成名字，放在列表最前面，让数据更像真的
_CURATED_NAMES = [
    "硅基食堂", "折叠宇宙", "数字游民老陈", "赛博茶馆", "慢速快门", "一帧电影",
    "白噪音研究所", "碳水教父", "三分钟实验室", "冷知识贩卖机", "虚拟播报员零",
    "像素画师Momo", "你好我是阿卷", "凌晨四点的城市", "声音采集者", "显卡炼丹师",
    "赛博朋克小卖部", "AI观察员", "代码诗人", "折纸飞机的人", "山与海之间",
    "不正经的物理老师", "半糖主义", "晚风收集计划", "机械知了", "量子咖啡",
]

_SIGNS = [
    "这个人很懒，什么都没写", "一个普通的B站用户", "喜欢折腾各种奇怪的东西",
    "业余时间做点小视频，随缘更新", "希望我的视频能帮到你", "在读研究生，方向是计算机视觉",
    "白天上班，晚上剪视频", "喜欢数码，也喜欢做饭", "不接广告，恰饭勿扰",
    "成分复杂，什么都看一点", "主要更新游戏实况，偶尔直播", "游戏区的萌新UP，求关注",
    "记录生活，分享快乐", "愿你我都能被世界温柔以待", "一个人也要好好吃饭",
    "代码写得比人好", "摄影爱好者，器材党", "每天进步一点点", "评论区比视频好看",
    "更新时间随缘，取关随意", "谢谢你的喜欢", "想把喜欢的东西分享给你",
    "社恐UP，回复慢见谅", "最近在学日语", "毕业之后才发现学习最快乐",
    "正在努力变成更好的自己", "生活不止眼前的苟且", "喜欢看番，也喜欢写番评",
    "音乐是生活的解药", "被生活按在地上摩擦", "普通本科在读，热爱历史",
    "厨房是我的快乐老家", "猫奴一枚", "立志做一只有趣的咸鱼", "努力做视频中",
    "从今天开始记录", "随缘更新，感谢陪伴", "喜欢所有可爱的东西", "一个不太正经的UP主",
    "认真生活，偶尔整活", "拍点照片，赚点快乐", "没事就爱看视频", "今年也要好好生活",
    "不想上班的第八百天", "在B站记录我的成长", "谢谢每一个点进来的人",
    "热爱可抵岁月漫长", "摸鱼学十级学者", "菜就多练", "把日子过成诗",
]

_OFFICIAL_TITLES = [
    "bilibili 知名科技UP主", "bilibili 知名游戏UP主", "bilibili 知名影视UP主",
    "bilibili 知名音乐UP主", "bilibili 知名美食UP主", "bilibili 2023百大UP主",
    "bilibili 知名科普UP主", "bilibili 知名动画UP主", "bilibili 知名舞蹈UP主",
    "bilibili 知名体育UP主", "bilibili 知名时尚UP主", "bilibili 知名汽车UP主",
    "bilibili 知名财经UP主", "bilibili 知名资讯UP主", "bilibili 知名动物UP主",
    "bilibili 知名手工艺UP主", "bilibili 虚拟UP主", "bilibili 知名学习UP主",
]

# 21 类分类的权重（大致贴近一个普通用户的关注构成）
_CATEGORY_WEIGHTS = {
    "游戏": 220,
    "动画·二次元": 200,
    "影视": 140,
    "科技数码": 130,
    "编程·AI": 90,
    "科普·人文": 100,
    "学习·教育": 120,
    "音乐": 130,
    "舞蹈": 40,
    "美食": 100,
    "生活·Vlog": 180,
    "搞笑·娱乐": 120,
    "体育·健身": 60,
    "汽车·机械": 50,
    "财经·职场": 60,
    "时事·社会": 50,
    "时尚·美妆": 60,
    "宠物·动物": 70,
    "手工·绘画·摄影": 60,
    "虚拟UP主·VTuber": 50,
    "其他·难判断": 30,
}

# ---------------------------------------------------------------------------
# 动态素材
# ---------------------------------------------------------------------------

_DYNAMIC_PLAN = [
    ("DYNAMIC_TYPE_FORWARD", 120),
    ("DYNAMIC_TYPE_DRAW", 99),
    ("DYNAMIC_TYPE_AV", 60),
    ("DYNAMIC_TYPE_WORD", 15),
    ("DYNAMIC_TYPE_ARTICLE", 6),
]

_FORWARD_TEXTS = [
    "转发一下，说得好", "确实是这样", "哈哈哈哈哈哈哈", "先收藏了，回头慢慢看",
    "太真实了", "有被治愈到", "转发这条动态，好运+1", "经典回顾", "前排围观",
    "愣住.jpg", "好家伙，还有这种操作", "看完感觉收获满满", "这个必须转",
    "啊啊啊啊啊太喜欢了", "笑死，根本停不下来",
]

_DRAW_TEXTS = [
    "今天出门拍了拍，天气真好", "第一次尝试做这种风格，轻喷", "凌晨三点的街景",
    "周末去爬山了，顺便拍了点照片", "新到的镜头试拍", "今天也是认真生活的一天",
    "路边的小猫，太可爱了", "给大家看看我新画的图", "调色练习，欢迎指点",
    "今天的天空值得记录", "随手一拍，意外地好看", "换了新壁纸，心情都变好了",
]

_WORD_TEXTS = [
    "今天有点累，早点睡", "有没有人和我一样……", "深夜emo一下，明天就好了",
    "突然想明白一件事", "今天的进度：0%", "在听一首老歌", "下雨了，心情意外地好",
]

_ARTICLE_TEXTS = [
    "写了一篇长文，聊聊我最近的想法", "整理了一份学习路线，希望能帮到大家",
    "这期专栏内容比较长，建议收藏", "关于这个问题，我有一些不同的看法",
]

_VIDEO_TITLES = [
    "【4K】这可能是今年最好看的风景混剪", "我用一周时间做了个自动化脚本",
    "从入门到入土：Python 爬虫实战", "十分钟带你看完这部冷门佳作",
    "详细测评：新旗舰到底值不值得买", "这个技巧能让你的电脑快 30%",
    "手把手教你做糖醋排骨", "这可能是B站最详细的线性代数讲解",
    "开了十年车才明白的道理", "用代码画一棵树", "拆解一台十年前的笔记本",
    "挑战一天只吃一顿饭", "普通人如何开始健身", "这可能是B站最全的装机指南",
    "当我试着复刻童年零食", "用 Excel 做了一个游戏",
]

# ---------------------------------------------------------------------------
# 收藏夹素材
# ---------------------------------------------------------------------------

_LEARNING_TITLES = [
    "MIT 6.824 分布式系统课程（中文字幕）", "高等数学 同济版 全集", "线性代数 麻省理工公开课",
    "CS231n 计算机视觉 课程讲解", "数据结构与算法 手把手教学", "考研英语阅读真题精讲",
    "概率论与数理统计 零基础入门", "机器学习从零开始", "深度学习框架 PyTorch 教程",
    "如何写好一篇论文", "英语听力训练合集", "Python 数据分析实战课程",
    "计算机网络 自顶向下 精讲", "操作系统原理 全 40 讲", "设计模式 23 种详解",
    "从零实现一个操作系统", "Git 使用教程（完整版）", "SQL 从入门到精通",
    "算法竞赛入门经典", "大学物理 课后习题讲解",
]

_MUSIC_TITLES = [
    "【Hi-Res】灵魂女声合集", "钢琴曲：雨天的午后", "2024 年度歌单分享",
    "古典音乐入门：十分钟听懂一首曲子", "吉他指弹《晴天》", "爵士乐现场合集",
    "电音精选：深夜驾驶 BGM", "国风音乐合集｜持续更新", "Live 现场：万人合唱的瞬间",
    "老歌重听：千禧年华语金曲", "纯音乐：适合学习/工作听的轻音乐", "摇滚现场修复合集",
    "翻唱：那些年被唱哭的歌", "音乐制作教程：从零开始编曲", "Lo-Fi 学习 BGM 一小时",
    "戏曲选段经典合集", "二胡/古筝 民乐精选", "和声分析：这首歌为什么好听",
    "音乐史：从巴洛克到现代", "练耳训练：每天十分钟",
]

_DEFAULT_TITLES = _VIDEO_TITLES + [
    "纪录片：一座城市的十二时辰", "我用废纸板做了一个机械臂", "测评：十款便宜好用的键盘",
    "毕业前一定要做的十件事", "那些年我们一起追过的番", "厨房小白的第一道硬菜",
    "手柄 vs 键盘：谁更适合动作游戏", "在县城开一家书店是什么体验",
    "把我家的猫拍成了电影", "三分钟看完一部悬疑片", "程序员的一天到底在干嘛",
    "这个免费软件，我装了十年", "跟着教程做了一次分子料理",
]

_TOVIEW_TITLES = _VIDEO_TITLES + _LEARNING_TITLES[:8] + _MUSIC_TITLES[:6] + [
    "已失效：这个视频不见了", "【直播回放】周五晚八点闲聊",
]

# ---------------------------------------------------------------------------


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(_HEX) for _ in range(n))


def _bvid(rng: random.Random) -> str:
    return "BV1" + "".join(rng.choice(_BASE58) for _ in range(9))


def _ts(rng: random.Random, lo: int, hi: int) -> int:
    return rng.randrange(lo, hi)


def _make_names(rng: random.Random, n: int) -> list[str]:
    """生成 n 个不重复、像真实 UP 主的昵称。"""
    names: list[str] = []
    seen: set[str] = set()

    for name in _CURATED_NAMES:
        if len(names) >= n:
            return names
        seen.add(name)
        names.append(name)

    attempts = 0
    while len(names) < n:
        attempts += 1
        roll = rng.random()
        if roll < 0.42:
            name = rng.choice(_NAME_PREFIXES) + rng.choice(_NAME_CORES)
        elif roll < 0.9:
            name = rng.choice(_NAME_CORES) + rng.choice(_NAME_SUFFIXES)
        else:
            name = (
                rng.choice(_EN_PREFIXES)
                + rng.choice(_EN_CORES)
                + rng.choice(_EN_SUFFIXES)
            )
        if name in seen:
            # 组合空间很大，正常情况几乎不会走到这里；兜底避免死循环
            if attempts > n * 50:
                name = f"{name}_{len(names)}"
            else:
                continue
        seen.add(name)
        names.append(name)
    return names


def _assign_groups(rng: random.Random, n: int, group_ids: list[int]) -> list[list[int]]:
    """给每个关注分配 0~2 个分组（返回 tagid 列表）。"""
    out: list[list[int]] = []
    for _ in range(n):
        roll = rng.random()
        if roll < 0.45:
            out.append([])
        elif roll < 0.8:
            out.append([rng.choice(group_ids)])
        else:
            picked = rng.sample(group_ids, 2)
            out.append(sorted(picked))
    return out


def make_follows(rng: random.Random, names: list[str]) -> list[dict]:
    """生成 2000 条关注，严格符合契约里的 FollowItem。"""
    group_ids = [74311568 + i * 137 for i in range(len(GROUP_NAMES))]
    group_name_by_id = dict(zip(group_ids, GROUP_NAMES))
    memberships = _assign_groups(rng, len(names), group_ids)
    mutual_idx = set(rng.sample(range(len(names)), MUTUAL_COUNT))

    categories = rng.choices(
        list(_CATEGORY_WEIGHTS.keys()),
        weights=list(_CATEGORY_WEIGHTS.values()),
        k=len(names),
    )
    # 保证 21 类全部出现（否则界面的分类筛选会有空项）
    seen_cats = set(categories)
    missing = [c for c in CATEGORIES if c not in seen_cats]
    for offset, cat in enumerate(missing):
        categories[(offset * 37 + 11) % len(categories)] = cat

    follows: list[dict] = []
    for i, name in enumerate(names):
        gids = memberships[i]
        # 约 22% 的关注还没分类；已分类里 rule 为主，少量 llm / user
        if rng.random() < 0.22:
            category: str | None = None
            source: str | None = None
        else:
            category = categories[i]
            source = rng.choices(
                ["rule", "llm", "user"], weights=[7, 2, 1], k=1
            )[0]
        mutual = i in mutual_idx
        follows.append(
            {
                "mid": 30000000 + i * 7 + 13,
                "name": name,
                "sign": rng.choice(_SIGNS),
                "face": f"https://i0.hdslb.com/bfs/face/{_hex(rng, 32)}.jpg",
                "official": rng.choice(_OFFICIAL_TITLES) if rng.random() < 0.12 else "",
                "attribute": 6 if mutual else 2,
                "mutual": mutual,
                "groups": [group_name_by_id[g] for g in gids],
                "group_ids": gids,
                "order": i + 1,
                "mtime": _ts(rng, _TS_2019, _TS_2025_06),
                "category": category,
                "category_source": source,
            }
        )
    return follows


def make_groups(follows: list[dict]) -> list[dict]:
    """按关注列表里的 group_ids 实时统计分组数量。"""
    counts: dict[int, int] = {}
    for item in follows:
        for gid in item.get("group_ids") or []:
            counts[gid] = counts.get(gid, 0) + 1
    groups = []
    for i, name in enumerate(GROUP_NAMES):
        tagid = 74311568 + i * 137
        groups.append({"tagid": tagid, "name": name, "count": counts.get(tagid, 0)})
    return groups


def make_dynamics(rng: random.Random, follows: list[dict]) -> list[dict]:
    """生成 300 条动态：转发 / 图文 / 视频 / 纯文字 / 专栏 混合。"""
    names = [f["name"] for f in follows]
    types = [t for t, weight in _DYNAMIC_PLAN for _ in range(weight)]
    rng.shuffle(types)

    dyn_ids: set[str] = set()
    items: list[dict] = []

    for dtype in types:
        while True:
            dyn_id = str(rng.randrange(10**18, 10**19 - 1))
            if dyn_id not in dyn_ids:
                dyn_ids.add(dyn_id)
                break

        has_image = False
        video: dict | None = None
        orig_author: str | None = None

        if dtype == "DYNAMIC_TYPE_FORWARD":
            text = rng.choice(_FORWARD_TEXTS)
            has_image = rng.random() < 0.55
            orig_author = rng.choice(names)
            if rng.random() < 0.35:
                video = {
                    "bvid": _bvid(rng),
                    "title": rng.choice(_VIDEO_TITLES),
                }
        elif dtype == "DYNAMIC_TYPE_DRAW":
            text = rng.choice(_DRAW_TEXTS)
            has_image = True
        elif dtype == "DYNAMIC_TYPE_AV":
            text = rng.choice(_VIDEO_TITLES)
            video = {"bvid": _bvid(rng), "title": text}
        elif dtype == "DYNAMIC_TYPE_WORD":
            text = rng.choice(_WORD_TEXTS)
        else:  # ARTICLE
            text = rng.choice(_ARTICLE_TEXTS)
            if rng.random() < 0.5:
                has_image = True

        items.append(
            {
                "id": dyn_id,
                "type": dtype,
                "type_label": _type_label(dtype),
                "pub_ts": _ts(rng, _TS_2024, _TS_2025_06),
                "text": text,
                "has_image": has_image,
                "video": video,
                "orig_author": orig_author,
            }
        )

    items.sort(key=lambda it: it["pub_ts"], reverse=True)
    return items


def _type_label(dtype: str) -> str:
    from ..const import DYNAMIC_TYPE_LABELS

    return DYNAMIC_TYPE_LABELS.get(dtype, "动态")


def _fav_items(
    rng: random.Random,
    titles: list[str],
    upper_names: list[str],
    count: int,
) -> list[dict]:
    """生成一个收藏夹里的条目，约 3.5% 是已失效视频。"""
    items: list[dict] = []
    for idx in range(count):
        invalid = rng.random() < 0.035
        if invalid:
            title = "已失效视频"
            upper = ""
            bvid = ""
            cover = ""
        else:
            title = rng.choice(titles)
            if rng.random() < 0.25:
                title = f"{title} P{idx % 24 + 1}"
            upper = rng.choice(upper_names)
            bvid = _bvid(rng)
            cover = f"https://i0.hdslb.com/bfs/archive/{_hex(rng, 32)}.jpg"
        items.append(
            {
                "id": str(rng.randrange(10**14, 10**15 - 1)),
                "type": 2,
                "title": title,
                "upper": upper,
                "bvid": bvid,
                "cover": cover,
                "fav_time": _ts(rng, _TS_2024, _TS_2025_06),
                "invalid": invalid,
            }
        )
    return items


def make_favorites(rng: random.Random, follows: list[dict]) -> tuple[list[dict], dict[int, list[dict]]]:
    """3 个收藏夹：默认 500 条 + 两个自建各 150 条。"""
    upper_names = [f["name"] for f in follows]
    default_media_id = 916673043
    specs = [
        (default_media_id, "默认收藏夹", DEFAULT_FAV_COUNT, True, 0, _DEFAULT_TITLES),
        (916673044, "学习资料", SELF_FAV_COUNTS[0], False, 0, _LEARNING_TITLES),
        (916673045, "音乐现场", SELF_FAV_COUNTS[1], False, 0, _MUSIC_TITLES),
    ]
    folders: list[dict] = []
    favorites: dict[int, list[dict]] = {}
    for media_id, title, count, is_default, privacy, titles in specs:
        items = _fav_items(rng, titles, upper_names, count)
        favorites[media_id] = items
        folders.append(
            {
                "media_id": media_id,
                "title": title,
                "count": len(items),
                "is_default": is_default,
                "privacy": privacy,
            }
        )
    return folders, favorites


def make_toview(rng: random.Random, follows: list[dict]) -> list[dict]:
    """120 条稍后再看，进度有 0 有中途。"""
    upper_names = [f["name"] for f in follows]
    items: list[dict] = []
    for _ in range(TOVIEW_COUNT):
        duration = rng.choice([42, 95, 128, 260, 375, 600, 900, 1240, 1800, 2700, 3600, 5400])
        progress = 0 if rng.random() < 0.6 else rng.randrange(1, duration)
        items.append(
            {
                "aid": rng.randrange(10**8, 2 * 10**9),
                "title": rng.choice(_TOVIEW_TITLES),
                "upper": rng.choice(upper_names),
                "duration": duration,
                "progress": progress,
                "add_ts": _ts(rng, _TS_2024, _TS_2025_06),
                "bvid": _bvid(rng),
            }
        )
    items.sort(key=lambda it: it["add_ts"], reverse=True)
    return items


def make_user(rng: random.Random) -> dict:
    """当前登录的账号（用户自己）。"""
    return {
        "mid": 16888842,
        "name": "在摸鱼的阿卷",
        "face": f"https://i0.hdslb.com/bfs/face/{_hex(rng, 32)}.jpg",
        "vip": False,
    }


def make_mock_data(seed: int = DEFAULT_SEED) -> dict:
    """生成完整的一套 Mock 数据。

    返回 dict 结构（字段全部符合 ``docs/API-CONTRACT.md``）::

        {
            "seed": 42,
            "user": User,
            "follows": [FollowItem] * 2000,
            "groups": [FollowGroup] * 6,
            "dynamics": [DynamicItem] * 300,
            "folders": [FavFolder] * 3,
            "favorites": {media_id: [FavItem]},
            "toview": [ToviewItem] * 120,
            "counts": {"follows": ..., "mutuals": 40, ...},
        }

    同一个 seed 多次调用结果完全一致。
    """
    rng = random.Random(seed)
    names = _make_names(rng, FOLLOW_COUNT)
    follows = make_follows(rng, names)
    groups = make_groups(follows)
    dynamics = make_dynamics(rng, follows)
    folders, favorites = make_favorites(rng, follows)
    toview = make_toview(rng, follows)
    user = make_user(rng)

    return {
        "seed": seed,
        "user": user,
        "follows": follows,
        "groups": groups,
        "dynamics": dynamics,
        "folders": folders,
        "favorites": favorites,
        "toview": toview,
        "counts": {
            "follows": len(follows),
            "mutuals": sum(1 for f in follows if f["mutual"]),
            "groups": len(groups),
            "categories": len({f["category"] for f in follows if f["category"]}),
            "dynamics": len(dynamics),
            "folders": len(folders),
            "favorites": sum(len(v) for v in favorites.values()),
            "toview": len(toview),
        },
    }


__all__ = [
    "DEFAULT_SEED",
    "FOLLOW_COUNT",
    "MUTUAL_COUNT",
    "DYNAMIC_COUNT",
    "TOVIEW_COUNT",
    "DEFAULT_FAV_COUNT",
    "SELF_FAV_COUNTS",
    "GROUP_NAMES",
    "make_mock_data",
    "make_follows",
    "make_groups",
    "make_dynamics",
    "make_favorites",
    "make_toview",
    "make_user",
]
