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


def constraints(query, text):
    """Return False for a contradiction; True for a strong cover anchor; else None."""
    query = re.sub(r'[“「《"].*?[”」》"]', "", normalize_objects(query))
    text = re.sub(r'[“「《"].*?[”」》"]', "", normalize_objects(text))
    owner, stored_owner = subject(query), subject(text)
    if owner != stored_owner and (owner or stored_owner):
        return False
    kinds = {kind for kind, pattern in SPECIES.items() if re.search(pattern, query, re.I)}
    actual = {kind for kind, pattern in SPECIES.items() if re.search(pattern, text, re.I)}
    # A coordinated request such as '工作和狗' asks for two independent topics.
    # Its animal qualifier must not disqualify the separately requested occupation.
    other_topic = bool(re.search(r"和|与|以及|还有", query)) and bool(
        set(topic_tokens(query)).intersection(topic_tokens(text))
        - {"topic4", "topic5", "topic6", "topic20", "topic21", "topic22"}
    )
    if kinds and not kinds.intersection(actual) and not other_topic:
        return False
    if "succulent" in kinds and "succulent" not in actual:
        return False
    moments = {kind for kind, pattern in TIMES.items() if re.search(pattern, query)}
    actual_moments = {kind for kind, pattern in TIMES.items() if re.search(pattern, text)}
    if moments and actual_moments and not moments.intersection(actual_moments):
        return False
    if re.search(r"哪(?:个|一)?区", query) and "区" not in text:
        return False
    covers = set(re.findall(r"[红蓝绿黑白黄紫棕灰橙]封面", query))
    if covers:
        return bool(covers.intersection(re.findall(r"[红蓝绿黑白黄紫棕灰橙]封面", text)))
    return None


def time_specificity(query, text):
    moments = {key for key, pattern in TIMES.items() if re.search(pattern, query)}
    if not moments:
        return 1
    actual = {key for key, pattern in TIMES.items() if re.search(pattern, text)}
    if moments.intersection(actual):
        return 2
    # A general prohibition remains relevant at every time of day.
    return 2 if not actual and re.search(r"不(?:喝|吃|能|可)|不能|禁止", text) else 1
