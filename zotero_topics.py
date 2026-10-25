"""Title-first topic matching against existing collection paths.

Aliases cover the local neuroscience library. Unknown topics require a caller
supplied concise keyword; a paper title is never used as a new collection name.
"""
from __future__ import annotations

import re


ALIASES = (
    ("V2a", ("v2a",)),
    ("甘氨酸能神经元", ("glycinergic", "甘氨酸能")),
    ("星形胶质细胞", ("astrocyte", "astroglia", "星形胶质")),
    ("运动神经元", ("motor neuron", "motoneuron", "运动神经元")),
    ("脊髓损伤", ("spinal cord injury", "injured spinal cord", "脊髓损伤")),
    ("神经再生", ("axon regeneration", "neural regeneration", "neuroregeneration", "神经再生")),
    ("中间神经元", ("interneuron", "中间神经元")),
    ("胶质细胞", ("glial cell", "glia", "胶质细胞")),
    ("泛素-蛋白酶体系统", ("ubiquitin", "proteasom", "泛素", "蛋白酶体")),
    ("神经解剖", ("stereotaxic coordinates", "neuroanatom", "神经解剖")),
    ("铁死亡", ("ferroptosis", "铁死亡")),
)


def normalized(text):
    return re.sub(r"[^\w]+", " ", str(text).casefold()).strip()


def _contains(text, word):
    # ASCII single words must not match inside an unrelated word.
    if re.fullmatch(r"[a-z0-9]+", word):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(word), text))
    return word in text


def collection_path(key, collections):
    by_key = {row["key"]: row.get("data", {}) for row in collections}
    parts, seen = [], set()
    while key:
        if key in seen or key not in by_key:
            raise ValueError("INVALID_COLLECTION_TREE")
        seen.add(key)
        data = by_key[key]
        parts.append(data["name"])
        key = data.get("parentCollection")
    return "/".join(reversed(parts))


def choose_topic(title, query, collections, explicit=None):
    title_text = normalized(title)
    if explicit is not None:
        topics = [str(explicit).strip()]
    else:
        topics = [topic for topic, aliases in ALIASES
                  if any(_contains(title_text, normalized(alias)) for alias in aliases)]
        # Prefer a title topic; a simple search keyword is a fallback when the
        # title supplies no known topic. Boolean expressions are split below.
        if not topics:
            topics = [re.sub(r"\[[A-Za-z ]+\]", "", part).strip().strip('"')
                      for part in re.split(r"\b(?:AND|OR|NOT)\b", query or "", flags=re.I)]
    topics = [topic for topic in topics if topic and normalized(topic) != title_text]
    for topic in topics:
        names = {normalized(topic)}
        for canonical, aliases in ALIASES:
            if normalized(topic) in {normalized(canonical), *(normalized(a) for a in aliases)}:
                names.update(normalized(a) for a in (canonical, *aliases))
        matches = [row for row in collections if normalized(row["data"].get("name")) in names]
        # Exact existing title keywords can match collections outside ALIASES.
        if len(matches) > 1:
            raise ValueError("AMBIGUOUS_COLLECTION_NAME")
        if matches:
            row = matches[0]
            return {"topic_keyword": topic, "name": row["data"]["name"],
                    "key": row["key"], "path": collection_path(row["key"], collections)}
    if explicit is None:
        matches = [row for row in collections
                   if len(normalized(row["data"].get("name"))) >= 3
                   and normalized(row["data"].get("name")) != title_text
                   and _contains(title_text, normalized(row["data"]["name"]))]
        if matches:
            # Most specific existing topic first; equal specificity is ambiguous.
            matches.sort(key=lambda row: len(normalized(row["data"]["name"])), reverse=True)
            if len(matches) > 1 and len(normalized(matches[0]["data"]["name"])) == len(normalized(matches[1]["data"]["name"])):
                raise ValueError("AMBIGUOUS_COLLECTION_NAME")
            row = matches[0]
            return {"topic_keyword": row["data"]["name"], "name": row["data"]["name"],
                    "key": row["key"], "path": collection_path(row["key"], collections)}
    if not topics:
        raise ValueError("TOPIC_REVIEW_REQUIRED")
    topic = topics[0]
    # Avoid turning a long search sentence into a collection title.
    if len(topic) > 60 or len(topic.split()) > 7:
        raise ValueError("TOPIC_REVIEW_REQUIRED")
    return {"topic_keyword": topic, "name": topic, "key": None, "path": topic}
