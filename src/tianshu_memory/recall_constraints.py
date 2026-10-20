"""Explicit subjects, object types and qualifiers constrain lexical topic matches."""

import re

from .recall_language import normalize_objects, topic_tokens

RELATIVES = (
    r"姐姐|妹妹|哥哥|弟弟|妈妈|母亲|爸爸|父亲|室友|同事|朋友|丈夫|妻子|儿子|女儿|姐|妹|哥|弟"
)
SPECIES = {
    "dog": r"狗|柯基|金毛|柴犬|拉布拉多|哈士奇|腊肠犬|\bdog\b|\bcorgi\b",
    "cat": r"猫|\bcat\b|\bkitten\b",
    "bird": r"鹦鹉|小鸟|鸟儿|文鸟|\bparrot\b|\bbird\b",
    "snake": r"蛇|\bsnake\b",
    "succulent": r"多肉|仙人掌|\bsucculent\b",
    "plant": r"植物|盆栽|百里香|薄荷|绿萝|吊兰|多肉|仙人掌|\bplant\b",
}
TIMES = {
    "morning": r"早上|上午|清晨",
    "afternoon": r"下午|午后",
    "night": r"晚上|夜里|睡前|临睡|睡觉前",
}


def subject(value):
    match = re.search(r"(?:作者|用户|我)(?:的)?(" + RELATIVES + r")", value)
    if match:
        return {
            "母亲": "妈妈",
            "父亲": "爸爸",
            "姐": "姐姐",
            "妹": "妹妹",
            "哥": "哥哥",
            "弟": "弟弟",
        }.get(match[1], match[1])
    return None


class Constraints:
    """Parse query qualifiers once; they never supply stored facts or authority."""

    def __init__(self, query):
        self.time_query = {k for k, p in TIMES.items() if re.search(p, query)}
        query = re.sub(r'[“「《"].*?[”」》"]', "", normalize_objects(query))
        self.owner = subject(query)
        self.kinds = {k for k, p in SPECIES.items() if re.search(p, query, re.I)}
        self.moments = {k for k, p in TIMES.items() if re.search(p, query)}
        self.topics = set(topic_tokens(query)) if re.search(r"和|与|以及|还有", query) else set()
        self.district = bool(re.search(r"哪(?:个|一)?区", query))
        self.covers = set(re.findall(r"[红蓝绿黑白黄紫棕灰橙]封面", query))
        self.new_cup = (
            "水杯" in query
            and re.search(r"(?:买|挑|选)(?:个|一个|只|一只)", query)
            and not re.search(r"我的|我那|之前|原来|同款|一样|旧|名字|叫啥|叫什么|放哪|在哪", query)
        )

    def admit(self, text):
        """False for a contradiction, True for a cover anchor, otherwise None."""
        text = re.sub(r'[“「《"].*?[”」》"]', "", normalize_objects(text))
        owner = subject(text)
        if self.owner != owner and (self.owner or owner):
            return False
        if self.new_cup and not re.search(r"喜欢|偏好|习惯|偏爱|不爱|倾向", text):
            return False
        if self.kinds:
            actual = {k for k, p in SPECIES.items() if re.search(p, text, re.I)}
            other_topic = self.topics.intersection(topic_tokens(text)) - {
                "topic4",
                "topic5",
                "topic6",
                "topic20",
                "topic21",
                "topic22",
            }
            if not self.kinds.intersection(actual) and not other_topic:
                return False
            if "succulent" in self.kinds and "succulent" not in actual:
                return False
        if self.moments:
            actual = {k for k, p in TIMES.items() if re.search(p, text)}
            if actual and not self.moments.intersection(actual):
                return False
        if self.district and "区" not in text:
            return False
        if self.covers:
            return bool(self.covers.intersection(re.findall(r"[红蓝绿黑白黄紫棕灰橙]封面", text)))
        return None

    def specificity(self, text):
        if not self.time_query:
            return 1
        actual = {k for k, p in TIMES.items() if re.search(p, text)}
        if self.time_query.intersection(actual):
            return 2
        # A general prohibition remains relevant at every time of day.
        return 2 if not actual and re.search(r"不(?:喝|吃|能|可)|不能|禁止", text) else 1
