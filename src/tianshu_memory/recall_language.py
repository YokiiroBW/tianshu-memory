"""Small bilingual topic lexicon; values and permissions never come from this layer."""

import re


def clean_query(value):
    """Remove operation wording outside quoted titles, retaining the requested content."""
    parts = re.split(r'([“「《"].*?[”」》"])', value)
    for index in range(0, len(parts), 2):
        text = re.sub(
            r"(?:请|帮我|替我|现在)?(?:调用|使用|用一下|用)?(?:显式)?(?:记忆|memory\s+)工具",
            " ",
            parts[index],
            flags=re.I,
        )
        parts[index] = re.sub(
            r"(?:没有|并未|并没有|未曾)(?:再)?(?:变化|改变)|"
            r"(?:还|仍)(?:是|用)(?:以前|之前|原来)(?:的|那个|那种)?|"
            r"(?:和|跟)(?:以前|之前|原来)一样|(?:请|帮我|替我)(?:记住|记下|更正|忘掉|忘记|查一下)",
            " ",
            text,
        )
    return "".join(parts)


def query_focus(value):
    """An explicit possessive/object question supplies stronger anchors than its preamble."""
    location = re.search(
        r"(?:我的?|我那|我家的)?([\u3400-\u9fff]{2,12}?)(?:一般|平时|通常|平常)?"
        r"(?:搁|放|收|存放|摆)(?:在)?哪(?:儿|里|个地方)?",
        value,
    )
    if location:
        target = re.sub(r"^(?:我的那个|我那个|那个|我的?|我那|帮我找找)", "", location[1])
        if target:
            return target
    missing = re.search(r"(?:找不到|找不着)([^，,。；;！？!?]+?)(?:了|来着)?$", value)
    if missing and len(missing[1]) >= 2:
        return missing[1]
    missing = re.search(r"^(.+?)(?:又|突然)?(?:找不到|找不着|不见|找不见)(?:了)?$", value)
    if missing:
        return re.sub(r"^(?:我的那个|我那个|那个|我的?|我那)", "", missing[1])
    ending = r"(?=(?:存放在|放在|放|在)?哪|叫什么|叫啥|用什么|使用什么|是什么|的(?:位置|名字|名称)|位置|[？?])"
    owned = re.search(r"(?:我的|我家(?:的)?|我那)(.+?)" + ending, value)
    if owned:
        return owned[1].strip()
    body = re.split(r"[：:，,]", value)[-1].strip()
    body = re.sub(r"^(?:(?:请|帮我|替我|记住|记下|更正|忘掉|忘记|查一下|查询)\s*)+", "", body)
    # An activity such as "我写手账" is not the name of the requested instrument.
    # Explicit possessives were handled above; preserve query aliases for activities.
    if body.startswith(("我", "你", "他", "她")):
        return None
    target = re.match(r"(.+?)(?:的位置|位置|在哪里|在哪|用什么|叫什么|叫啥)", body)
    return re.sub(r"^(?:我|你|他|她)", "", target[1].strip()) if target else None


def correction_target(value):
    body = re.sub(r"^(?:我的|我家(?:的)?|本人的)", "", value.strip())
    target = re.match(
        r"(?![我你他她])(.+?)的(?:标题|名字|名称|颜色|位置|墨水)"
        r"(?:更正为|改为|改成|调整为)",
        body,
    )
    return target[1] if target else None


def query_clauses(value):
    clauses = [""]
    for index, part in enumerate(re.split(r'([“「《"].*?[”」》"])', clean_query(value))):
        if index % 2:
            clauses[-1] += part
            continue
        pieces = re.split(r"[，,。；;！？!?、\n]|\b(?:and|also)\b", part)
        clauses[-1] += pieces[0]
        clauses.extend(pieces[1:])
    return [clause.strip() for clause in clauses if clause.strip()]


def topic_family(tokens):
    result = set(tokens)
    if result.intersection({"topic11", "topic12"}):
        result.add("topic10")
    if result.intersection({"topic5", "topic6"}):
        result.add("topic4")
    if result.intersection({"topic7", "topic8", "beverage", "drink", "饮料", "饮品"}):
        result.add("topic19")
    return result


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
    (
        "饮料",
        "饮品",
        "喝什么",
        "喝啥",
        "爱喝",
        "常喝",
        "点一杯",
        "点杯",
        "带一杯喝的",
        "beverage",
        "drink",
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
    # '喝什么咖啡/茶' asks for a specific drink family, not all beverages.
    if {"topic7", "topic8"}.intersection(result) and "topic19" in result:
        result.remove("topic19")
    return result


def topic_aliases(value):
    """Return the existing lexical aliases for an explicitly mentioned topic."""
    topics = set(topic_tokens(value))
    return {
        alias
        for index, aliases in enumerate(TOPICS)
        if f"topic{index}" in topics
        for alias in aliases
    }


# Query-side literal expansion also works with indexes created before these aliases
# existed. It never changes stored facts or adds a value absent from their sources.
QUERY_ALIASES = (
    ("小鸟", "鸟儿", "鹦鹉", "文鸟", "虎皮鹦鹉", "bird", "parrot", "budgie"),
    ("什么笔", "哪种笔", "钢笔", "圆珠笔", "铅笔", "pen", "pencil"),
)


def query_aliases(value):
    lowered = value.casefold()
    return [
        alias
        for group in QUERY_ALIASES
        if any(
            re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", lowered)
            if term.isascii()
            else term in lowered
            for term in group
        )
        for alias in group
    ]
