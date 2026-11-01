"""Online status of the clan activity table ("Last online 3 days ago") as days since last online.

Russian is built in. Other game languages are learned from the history of checks, without any dictionary:
the number of an offline member's status grows with time - by 1 an hour for an hours word, by 1 a day for a
days word - so two checks of the same member a few hours apart tell what the word after the number means.
Phrases without a number ("a day ago") are learned from the checks next to them.
"""
import math
import re

LONG_AGO = 10000.0
HOUR = 1 / 24
UNITS = {"minute": 1 / 1440, "hour": HOUR, "day": 1.0, "week": 7.0, "month": 30.0}   # in days
PHRASES = (0.0, HOUR, 1.0, 7.0, 30.0)   # what a phrase without a number can mean: just now, an hour, a day...

# Cyrillic and Latin letters that look alike, folded to one script so one word always gives the same token
_FOLD = str.maketrans("аеорсхукмнвт", "aeopcxykmhbt")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_NUM = re.compile(r"\d+")


def builtin_ru(text):
    """Russian client: "Был(а) в сети 3 дн. назад", "... день назад", "... 5 ч назад", "... час назад",
    "Был в сети: 7 мин. наз.", "Не в сети" (long ago). None when the text is not Russian or not understood."""
    s = text.lower().replace("ё", "е")
    s = s.translate(str.maketrans("aeopcxyk", "аеорсхук"))   # Latin look-alikes OCR puts into Russian words
    # a number read as letters ("В ч назад" is "8 ч назад"): only right before a time unit
    s = re.sub(r"(?<![а-я])([взоб]{1,2})(?=\s*(?:дн|ч\b|час|мин|нед|мес))",
               lambda m: m.group(1).translate(str.maketrans("взоб", "8306")), s)
    if re.search(r"не\s*в\s*сет", s):
        return LONG_AGO
    for pattern, unit in ((r"(\d+)\s*мес", 30), (r"(\d+)\s*нед", 7), (r"(\d+)\s*(?:дн|дня|дней|день|д\.)", 1),
                          (r"(\d+)\s*(?:ч|час)", HOUR), (r"(\d+)\s*мин", 1 / 1440), (r"(\d+)\s*сек", 0)):
        m = re.search(pattern, s)
        if m:
            return int(m.group(1)) * unit
    for word, days in (("месяц", 30), ("недел", 7), ("день", 1), ("вчера", 1), ("час", HOUR), ("мин", 0),
                       ("только что", 0)):
        if word in s:
            return days
    return None


def tokens(text):
    """(number, unit word) of a status, or (None, phrase) when it has no number. Words are folded so OCR
    mixing Cyrillic and Latin look-alikes still gives the same token."""
    s = text.lower()
    words = [w.translate(_FOLD) for w in _WORD.findall(s)]
    m = _NUM.search(s)
    if not m:
        return None, " ".join(words)
    after = [w.translate(_FOLD) for w in _WORD.findall(s[m.end():])]
    before = [w.translate(_FOLD) for w in _WORD.findall(s[:m.start()])]
    unit = after[0] if after else (before[-1] if before else "")   # "3 days ago", "vor 3 Tagen" / "3天前"
    return int(m.group()), unit


class Model:
    """What was learned: {unit word: days per unit}, {phrase: days}."""

    def __init__(self, units=None, phrases=None):
        self.units, self.phrases = units or {}, phrases or {}

    def days(self, text, online=False):
        """Days since last online for a status, None if unknown. Online members: 0."""
        if online:
            return 0.0
        if not text:
            return None
        ru = builtin_ru(text)
        if ru is not None:
            return ru
        n, word = tokens(text)
        if n is None:
            return self.phrases.get(word)
        unit = self.units.get(word)
        return n * unit if unit is not None else None


def learn(history, known=None):
    """Model from the history of checks: [(scan_at, member_key, status, online)].

    Unit words: for two checks of the same offline member with the same word, the number grows by the elapsed
    time in that unit (a lower number means the member was online in between: not used). A unit is taken when
    it explains at least three such pairs 50 times better than any other unit; numbers above 23 or 59 rule out
    hours and minutes. Phrases: estimated from a check with a known status right before or after them."""
    known = known or Model()
    by_member = {}
    for scan_at, key, status, online in sorted(history):
        by_member.setdefault(key, []).append((scan_at, status or "", bool(online)))

    # log-likelihood of each unit for each word. A status shows floor(age / unit), so after `e` units of time
    # the number grows by floor(e) or floor(e) + 1, the latter with probability frac(e). Over many pairs this
    # tells hours from days and days from weeks even when the number changed by 0 or 1 only.
    score, pairs, top = {}, {}, {}
    for checks in by_member.values():
        for (t1, s1, on1), (t2, s2, on2) in zip(checks, checks[1:]):
            if on1 or on2 or builtin_ru(s1) is not None or builtin_ru(s2) is not None:
                continue
            (n1, w1), (n2, w2) = tokens(s1), tokens(s2)
            for n, w in ((n1, w1), (n2, w2)):
                if n is not None:
                    top[w] = max(top.get(w, 0), n)
            hours = (t2 - t1) / 3600
            if n1 is None or n2 is None or w1 != w2 or n2 < n1 or not 0.25 <= hours <= 24 * 20:
                continue   # another word, or the number went down: the member was online in between
            g = n2 - n1
            pairs[w1] = pairs.get(w1, 0) + 1
            for unit, size in UNITS.items():
                e = hours / 24 / size
                base, frac = int(e), e - int(e)
                p = frac if g == base + 1 else (1 - frac) if g == base else 0.0
                score.setdefault(w1, {}).setdefault(unit, 0.0)
                score[w1][unit] += math.log(max(p, 0.002))
    units = dict(known.units)
    for word, sc in score.items():
        if top.get(word, 0) > 59:
            sc = {u: v for u, v in sc.items() if u not in ("minute", "hour")}
        elif top.get(word, 0) > 23:
            sc = {u: v for u, v in sc.items() if u != "hour"}
        if not sc or pairs[word] < 3:
            continue
        ranked = sorted(sc, key=sc.get, reverse=True)
        # taken only when clearly ahead: 50 times more likely than the next unit
        if len(ranked) == 1 or sc[ranked[0]] - sc[ranked[1]] >= math.log(50):
            units[word] = UNITS[ranked[0]]

    # phrases: bounds that hold even if the member was online between the checks (the age only grows while
    # offline and drops when the member comes online):
    #   phrase at t1, "n units" at t2  ->  phrase age >= n * unit - (t2 - t1)
    #   "n units" at t1, phrase at t2  ->  phrase age <= (n + 1) * unit + (t2 - t1)
    # the smallest meaning that fits the bounds is taken: a doubt can only keep a member, never kick one
    model = Model(units, dict(known.phrases))
    low, high = {}, {}
    for checks in by_member.values():
        for (t1, s1, on1), (t2, s2, on2) in zip(checks, checks[1:]):
            if on1 or on2:
                continue
            (n1, w1), (n2, w2) = tokens(s1), tokens(s2)
            days = (t2 - t1) / 86400
            if n1 is None and n2 is not None and w2 in units:
                low.setdefault(w1, []).append(n2 * units[w2] - days)
            elif n2 is None and n1 is not None and w1 in units:
                high.setdefault(w2, []).append((n1 + 1) * units[w1] + days)
    for phrase, lows in low.items():
        if not phrase or len(lows) < 2:
            continue
        lo, hi = max(lows), min(high.get(phrase, [LONG_AGO]))
        for value in PHRASES:   # each meaning covers [value, 2 * value): "a day ago" is 1 to 2 days
            top = 2 * value if value else 1 / 1440
            if top > lo - 1 / 1440 and value <= hi + 1 / 1440:
                model.phrases[phrase] = value
                break
    return model
