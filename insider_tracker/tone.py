"""Tone of news headlines (negative ... positive), to show what insiders were buying into.

VADER (vaderSentiment, MIT) scores short text from -1 to +1. It was tuned on social media and
misreads finance: it doesn't know "beats", "downgrade" or "guidance", and treats "cancer", "crude"
and "gross" as strongly negative wherever they appear ("positive cancer trial", "crude oil",
"gross margin"). The finance word weights and phrase rules below correct that; phrases are
rewritten into single made-up tokens before scoring, so VADER's own handling of negation and
intensifiers still applies ("not expected to cut guidance").

Everything is tunable from the Tone tab, which writes tone.toml (the full settings) and
tone_labels.json (your good/neutral/bad labels for real headlines, used to measure agreement).
Neither belongs in git: they are one person's tuning, like watchlist.json. With no tone.toml,
DEFAULTS apply. The word list is ours (MIT, like the repo); the Loughran-McDonald dictionary would
be the academic choice, but its terms limit it to individual research use.
"""
import json
import os
import re
import tomllib
from pathlib import Path

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
except ImportError:  # optional: without it, tone is simply blank everywhere
    SentimentIntensityAnalyzer = None

CONFIG_PATH = Path(os.environ.get("INSIDER_TONE", "tone.toml"))
LABELS_PATH = Path(os.environ.get("INSIDER_TONE_LABELS", "tone_labels.json"))

_OBJ = r"(?:estimates?|expectations|forecasts?|views?|consensus|targets?)"
_PLAN = r"(?:guidance|outlook|forecasts?|dividends?|price target|targets?|view)"

# VADER's scale for words and phrases: about -4 (worst) to +4 (best); most sit between 1 and 2.5.
DEFAULTS = {
    "item_cut": 0.05,   # one headline at or beyond +-this reads good / bad (VADER's own convention)
    "label_at": 0.15,   # a set's average beyond +-this reads positive / negative
    "mixed_at": 0.30,   # otherwise "mixed" if it holds a headline at least this bad and this good
    "lead_days": 30,    # a cluster's tone covers this many days before its first buy
    "words": {
        # moves and ratings VADER doesn't know
        "soar": 2.2, "soars": 2.2, "soared": 2.2, "soaring": 2.2, "surge": 2.0, "surges": 2.0,
        "surged": 2.0, "surging": 2.0, "rally": 1.8, "rallies": 1.8, "rallied": 1.8, "jump": 1.4,
        "jumps": 1.4, "jumped": 1.4, "climbs": 1.1, "climbed": 1.1, "rebound": 1.4, "rebounds": 1.4,
        "upgrade": 2.0, "upgrades": 2.0, "upgraded": 2.0, "outperform": 1.8, "outperforms": 1.8,
        "bullish": 2.0, "beat": 1.6, "beats": 1.6, "buyback": 1.3, "buybacks": 1.3,
        "repurchase": 1.0, "approval": 1.6, "approves": 1.6, "approved": 1.6, "breakthrough": 2.0,
        "profitable": 1.6, "record": 1.0,
        "plunge": -2.6, "plunges": -2.6, "plunged": -2.6, "plummet": -2.8, "plummets": -2.8,
        "plummeted": -2.8, "tumble": -2.2, "tumbles": -2.2, "tumbled": -2.2, "slump": -2.0,
        "slumps": -2.0, "slumped": -2.0, "sinks": -1.8, "sank": -1.8, "drops": -1.3,
        "dropped": -1.3, "falls": -1.2, "fell": -1.2, "slides": -1.3, "slid": -1.3, "dip": -1.0,
        "dips": -1.0, "dipped": -1.0, "downgrade": -2.0, "downgrades": -2.0, "downgraded": -2.0,
        "underperform": -1.8, "underperforms": -1.8, "bearish": -2.0, "bankruptcy": -3.0,
        "bankrupt": -3.0, "default": -2.2, "defaults": -2.2, "delisting": -2.5, "delisted": -2.5,
        "recall": -1.8, "recalls": -1.8, "probe": -1.8, "investigation": -1.8, "subpoena": -2.0,
        "layoffs": -1.8, "impairment": -1.8, "writedown": -1.8, "dilution": -1.4, "dilutive": -1.4,
        "resigns": -1.2, "resigned": -1.2, "halts": -1.5, "halted": -1.5, "restatement": -2.2,
        # ordinary finance words VADER scores as if they were news. Found by counting the
        # VADER-scored words in real headlines (the Tone tab lists them): "shares" alone is +1.2
        # in VADER and appears in a large share of all headlines, tilting everything positive.
        "shares": 0.0, "share": 0.0, "worth": 0.0, "value": 0.0, "trust": 0.0, "energy": 0.0,
        "natural": 0.0, "treasury": 0.0, "treasuries": 0.0, "securities": 0.0, "fair": 0.0,
        "solutions": 0.0, "intelligence": 0.0, "credit": 0.0, "clean": 0.0, "agreement": 0.3,
        "demand": 0.0, "dick": 0.0, "disorder": 0.0, "depressive": 0.0, "dx": 0.0,
        "cancer": 0.0, "tumor": 0.0, "crude": 0.0, "gross": 0.0, "interest": 0.0,
        "liability": 0.0, "debt": -0.3, "offering": 0.0, "cut": -0.5,
    },
    "phrases": [
        {"name": "beats estimates", "weight": 2.2, "pattern":
            rf"\b(?:beats?|tops?|topped|exceeds?|exceeded|surpass(?:es|ed)?|crush(?:es|ed)?)\b[^.]{{0,30}}?\b{_OBJ}"},
        {"name": "misses estimates", "weight": -2.2, "pattern": rf"\bmiss(?:es|ed)?\b[^.]{{0,30}}?\b{_OBJ}"},
        {"name": "raises guidance", "weight": 2.0, "pattern":
            rf"\b(?:raises?|raised|lifts?|lifted|boosts?|boosted|hikes?|hiked|ups)\b[^.]{{0,25}}?\b{_PLAN}"},
        {"name": "cuts guidance", "weight": -2.2, "pattern":
            rf"\b(?:cuts?|lowers?|lowered|slash(?:es|ed)?|trims?|trimmed|reduces?|reduced|withdraws?)\b[^.]{{0,25}}?\b{_PLAN}"},
        {"name": "suspends dividend", "weight": -2.6,
         "pattern": r"\b(?:suspends?|suspended|eliminates?|omits?)\b[^.]{0,20}?\bdividends?"},
        {"name": "going concern", "weight": -3.0, "pattern": r"\bgoing concern\b"},
        {"name": "short seller", "weight": -1.8, "pattern": r"\bshort[- ]sellers?\b|\bshort report\b"},
        {"name": "profit warning", "weight": -2.5, "pattern": r"\bprofit warning\b"},
        {"name": "exchange compliance notice", "weight": -2.2,
         "pattern": r"\b(?:minimum bid price|deficiency (?:letter|notice)|listing (?:rule|standard)s? (?:notice|deficiency))\b"},
        {"name": "new high", "weight": 1.6, "pattern": r"\b(?:52-week|all-time|record) highs?\b"},
        {"name": "new low", "weight": -1.6, "pattern": r"\b(?:52-week|all-time|record) lows?\b"},
    ],
    # 8-K item types that are bad news by their nature, scored -1..0. Others (results, an officer
    # change) could go either way and aren't scored.
    "bad_8k": {"bankruptcy": -0.8, "debt accelerated": -0.7, "restructuring": -0.4,
               "impairment": -0.5, "delisting notice": -0.7, "past financials unreliable": -0.8,
               "cybersecurity incident": -0.5},
}
NUMBERS = ("item_cut", "label_at", "mixed_at", "lead_days")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'&-]*")


class SettingsError(ValueError):
    pass


# ---------------------------------------------------------------- settings

def load(path=None) -> dict:
    """The settings in tone.toml, or DEFAULTS if there is none. A broken file raises SettingsError."""
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        return json.loads(json.dumps(DEFAULTS))
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path}: {exc}") from None
    return validate(raw)


def validate(raw) -> dict:
    """Check a settings dict (from tone.toml or the Tone tab) and fill in anything missing."""
    problems = []
    out = {k: raw.get(k, DEFAULTS[k]) for k in NUMBERS}
    for k in NUMBERS:
        if not isinstance(out[k], (int, float)) or isinstance(out[k], bool) or out[k] < 0:
            problems.append(f"{k} must be a number >= 0")
    words = raw.get("words", DEFAULTS["words"])
    out["words"] = {}
    for w, v in (words or {}).items():
        if not WORD_RE.fullmatch(str(w)):
            problems.append(f"word {w!r}: lowercase letters, digits, ' & - only")
        elif not isinstance(v, (int, float)) or not -4 <= v <= 4:
            problems.append(f"word {w!r}: weight must be between -4 and 4")
        else:
            out["words"][str(w)] = float(v)
    out["phrases"] = []
    for p in raw.get("phrases", DEFAULTS["phrases"]) or []:
        name, pat, v = str(p.get("name") or "").strip(), p.get("pattern") or "", p.get("weight")
        try:
            re.compile(pat, re.I)
        except re.error as exc:
            problems.append(f"phrase {name!r}: pattern doesn't compile ({exc})")
            continue
        if not name or ":" in name or not pat or "'" in pat or "\n" in pat:
            problems.append(f"phrase {name!r}: needs a name without ':' and a one-line pattern without '")
        elif not isinstance(v, (int, float)) or not -4 <= v <= 4:
            problems.append(f"phrase {name!r}: weight must be between -4 and 4")
        else:
            out["phrases"].append({"name": name, "pattern": pat, "weight": float(v)})
    out["bad_8k"] = {}
    for k, v in (raw.get("bad_8k", DEFAULTS["bad_8k"]) or {}).items():
        if not isinstance(v, (int, float)) or not -1 <= v <= 0:
            problems.append(f"8-K type {k!r}: score must be between -1 and 0")
        else:
            out["bad_8k"][str(k)] = float(v)
    if problems:
        raise SettingsError("; ".join(problems))
    return out


def to_toml(s) -> str:
    q = json.dumps  # a JSON string is a valid TOML basic string
    lines = ["# Headline tone settings, written by the Tone tab. Edit there, or here and reload.",
             "# Words and phrases use VADER's scale: about -4 (worst news) to +4 (best).", ""]
    lines += [f"{k} = {s[k]}" for k in NUMBERS] + ["", "[words]"]
    lines += [f"{q(w)} = {v}" for w, v in sorted(s["words"].items())] + ["", "[bad_8k]"]
    lines += [f"{q(k)} = {v}" for k, v in sorted(s["bad_8k"].items())]
    for p in s["phrases"]:
        lines += ["", "[[phrases]]", f"name = {q(p['name'])}", f"pattern = '{p['pattern']}'",
                  f"weight = {p['weight']}"]
    return "\n".join(lines) + "\n"


def save(s, path=None):
    Path(path or CONFIG_PATH).write_text(to_toml(validate(s)), encoding="utf-8")


def reset(path=None):
    p = Path(path or CONFIG_PATH)
    if p.exists():
        p.unlink()


# The Tone tab edits words and phrases as text, one per line.
def words_text(s) -> str:
    return "\n".join(f"{w} = {v:g}" for w, v in sorted(s["words"].items(), key=lambda kv: (kv[1], kv[0])))


def phrases_text(s) -> str:
    return "\n".join(f"{p['name']}: {p['pattern']} = {p['weight']:g}" for p in s["phrases"])


def bad_8k_text(s) -> str:
    return "\n".join(f"{k} = {v:g}" for k, v in sorted(s["bad_8k"].items()))


def _pairs(text, what):
    out, problems = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, eq, val = line.rpartition("=")
        try:
            out[key.strip().lower() if what == "word" else key.strip()] = float(val)
        except ValueError:
            problems.append(f"{what}s line {n}: expected 'name = number', got {line!r}")
        if not eq:
            problems.append(f"{what}s line {n}: missing '='")
    return out, problems


def from_form(fields) -> dict:
    """Settings from the Tone tab's form (a parse_qs dict). Raises SettingsError on any mistake."""
    one = lambda k: (fields.get(k) or [""])[0]
    problems = []
    raw = {}
    for k in NUMBERS:
        try:
            raw[k] = float(one(k)) if k != "lead_days" else int(float(one(k)))
        except ValueError:
            problems.append(f"{k} must be a number")
    raw["words"], p1 = _pairs(one("words"), "word")
    raw["bad_8k"], p2 = _pairs(one("bad_8k"), "8-K type")
    raw["phrases"] = []
    for n, line in enumerate(one("phrases").splitlines(), 1):
        if not line.strip():
            continue
        m = re.fullmatch(r"\s*([^:]+?)\s*:\s*(.+?)\s*=\s*([-+]?\d+(?:\.\d+)?)\s*", line)
        if not m:
            problems.append(f"phrases line {n}: expected 'name: pattern = weight'")
            continue
        raw["phrases"].append({"name": m[1], "pattern": m[2], "weight": float(m[3])})
    problems += p1 + p2
    if problems:
        raise SettingsError("; ".join(problems))
    return validate(raw)


# ---------------------------------------------------------------- scoring

_analyzers = {}


def available() -> bool:
    return SentimentIntensityAnalyzer is not None


def _analyzer(s):
    """A VADER analyzer with these settings' words and phrase tokens, cached per settings."""
    key = json.dumps(s, sort_keys=True)
    if key not in _analyzers:
        if len(_analyzers) > 8:
            _analyzers.clear()
        a = SentimentIntensityAnalyzer()
        a.lexicon.update(s["words"])
        a.lexicon.update({f"zzphrase{i}": p["weight"] for i, p in enumerate(s["phrases"])})
        _analyzers[key] = (a, [(re.compile(p["pattern"], re.I), f"zzphrase{i}", p["name"])
                               for i, p in enumerate(s["phrases"])])
    return _analyzers[key]


def explain(text, s=None):
    """(score -1..+1, drivers) for one headline. Drivers are the words and phrases that carried
    weight, as (name, weight), biggest first. (None, []) if VADER isn't installed."""
    if not available():
        return None, []
    s = s or load()
    a, phrases = _analyzer(s)
    names = {}
    for rx, tok, name in phrases:
        text, n = rx.subn(f" {tok} ", text)
        if n:
            names[tok] = name
    drivers = []
    for w in dict.fromkeys(WORD_RE.findall(text.lower())):
        v = a.lexicon.get(w)
        if v:
            drivers.append((names.get(w, w), v))
    drivers.sort(key=lambda d: -abs(d[1]))
    return a.polarity_scores(text)["compound"], drivers


def score_text(text, s=None):
    return explain(text, s)[0]


def score_item(item, s=None):
    """A news item's score: headlines by VADER, 8-Ks only when the filing type is bad news."""
    s = s or load()
    if item.get("kind") == "8k":
        words = (item.get("title") or "").split(":", 1)[-1]
        hits = [v for k, v in s["bad_8k"].items() if k in words]
        return min(hits) if hits else None
    return score_text(item.get("title") or "", s)


def classify(score, s=None):
    """+1 / 0 / -1 for one headline's score."""
    cut = (s or load())["item_cut"]
    return 1 if score >= cut else -1 if score <= -cut else 0


def summarize(items, s=None):
    """(label, average, count) over the items that can be scored, or None if none can."""
    s = s or load()
    scores = [x for x in (score_item(i, s) for i in items) if x is not None]
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    if avg <= -s["label_at"]:
        label = "negative"
    elif avg >= s["label_at"]:
        label = "positive"
    elif min(scores) <= -s["mixed_at"] and max(scores) >= s["mixed_at"]:
        label = "mixed"
    else:
        label = "neutral"
    return label, avg, len(scores)


def frequent_words(titles, s=None, limit=40):
    """[(word, headlines it appears in, weight, from_settings)] for words that carry a score, most
    common first: the quickest way to spot a name or a jargon word VADER reads as emotion."""
    s = s or load()
    a, _ = _analyzer(s)
    seen = {}
    for t in titles:
        for w in set(WORD_RE.findall(t.lower())):
            if w in a.lexicon and (a.lexicon[w] or w in s["words"]):
                seen[w] = seen.get(w, 0) + 1
    ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [(w, n, a.lexicon[w], w in s["words"]) for w, n in ranked]


# ---------------------------------------------------------------- your labels

def load_labels(path=None) -> dict:
    """{headline: +1 good / 0 neutral / -1 bad}."""
    p = Path(path or LABELS_PATH)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {str(k): int(v) for k, v in data.items() if v in (-1, 0, 1)}


def set_label(title, value, path=None):
    """Label one headline (+1, 0, -1), or remove its label with None."""
    labels = load_labels(path)
    if value is None:
        labels.pop(title, None)
    else:
        labels[title] = int(value)
    Path(path or LABELS_PATH).write_text(json.dumps(labels, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                                         encoding="utf-8")


def agreement(labels, s=None) -> dict:
    """How the scores line up with your labels. Bad news is counted on its own: it is rare, and it is
    what the tone column most needs to get right."""
    s = s or load()
    got = {t: classify(score_text(t, s), s) for t in labels} if available() else {}
    n = len(got)
    bad = [t for t, v in labels.items() if v == -1]
    good = [t for t, v in labels.items() if v == 1]
    return {
        "labeled": n,
        "agree": sum(got[t] == labels[t] for t in got),
        "bad": len(bad), "bad_caught": sum(got.get(t) == -1 for t in bad),
        "good": len(good), "good_caught": sum(got.get(t) == 1 for t in good),
        "false_bad": sum(1 for t in got if got[t] == -1 and labels[t] != -1),
        "opposite": sum(1 for t in got if got[t] * labels[t] == -1),
        "verdicts": got,
    }
