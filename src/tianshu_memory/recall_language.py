"""Small bilingual topic lexicon; values and permissions never come from this layer."""

import re

# General retrieval vocabulary, not user facts. Index and query use the same normalization.
TOPICS = (
    ("居住", "住址", "住在", "住所", "住哪", "搬家", "家在哪", "residence", "address", "live"),
    ("家乡", "老家", "出生地", "hometown", "birthplace"),
    ("工作", "职业", "做什么工作", "上班", "谋生", "occupation", "profession", "job", "work"),
    ("公司", "工作室", "单位", "雇主", "employer", "company", "studio"),
    ("宠物", "毛孩子", "养的", "pet"),
    ("猫", "猫咪", "小猫", "喵", "cat", "kitten"),
    ("狗", "狗狗", "小狗", "汪", "dog", "puppy"),
    ("咖啡", "美式", "拿铁", "coffee", "espresso", "caffeine"),
    ("茶", "茶叶", "茶饮", "泡茶", "tea"),
    ("饮食", "口味", "忌口", "点菜", "配菜", "调料", "吃什么", "不吃", "food", "diet"),
    ("语言", "外语", "学什么", "language", "learning"),
    ("西班牙语", "西语", "spanish"),
    ("日语", "日本语", "japanese"),
    ("编程", "程序", "开发", "写代码", "programming", "developer", "coding"),
    ("骑行", "骑车", "自行车", "单车", "cycling", "bicycle"),
    ("陶艺", "陶瓷", "捏陶", "拉坯", "pottery", "ceramics"),
    ("旅行", "旅游", "出差", "行程", "travel", "trip"),
    ("笔记", "备忘", "记事", "note", "notebook"),
    (
        "软件",
        "工具",
        "应用",
        "编程语言",
        "software",
        "tool",
        "application",
        "app",
        "programming language",
    ),
)


def topic_tokens(value):
    lowered = value.casefold()
    result = []
    for index, aliases in enumerate(TOPICS):
        if any(
            (
                re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", lowered)
                if alias.isascii()
                else (
                    re.search(r"工作(?!室|坊|台|间|服|证|餐)", lowered)
                    if alias == "工作"
                    else alias in lowered
                )
            )
            for alias in aliases
        ):
            result.append("topic" + str(index))
    return result
