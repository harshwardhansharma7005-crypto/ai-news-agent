#!/usr/bin/env python3
"""
ai-news-agent
=============
Collects fresh AI news from RSS feeds, removes duplicates, verifies articles,
asks Gemini to analyse them, picks the 10 most significant stories (with
company diversity) and writes a Facebook-ready post.

Outputs:
    output/daily_post.txt   - the Facebook post text
    output/daily_news.json  - the selected stories as JSON

Needs one environment variable: GEMINI_API_KEY
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests

# --------------------------------------------------------------------------
# SETTINGS (safe to tweak)
# --------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")
OUTPUT_DIR = Path("output")

TOP_N = 10                 # how many stories in the final post
FRESH_HOURS = 36           # preferred maximum age of a story
FALLBACK_HOURS = 72        # only used if there are not enough fresh stories
MAX_PER_FEED = 6           # newest N stories taken from each feed
MAX_CANDIDATES = 60        # max stories sent to Gemini
MIN_TEXT_CHARS = 120       # below this we cannot verify an article -> excluded
MIN_SIGNIFICANCE = 3       # ignore stories Gemini rates below this (1-10)
BATCH_SIZE = 20            # stories per Gemini request
DIVERSITY_CAPS = (2, 3, 4) # max stories per company; relaxed only if needed
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ai-news-agent/1.0; +https://github.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml;q=0.9,*/*;q=0.8",
}

# Models are tried in this order. Set GEMINI_MODEL to put your own choice first.
_models = [
    os.environ.get("GEMINI_MODEL", "").strip(),
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite-preview",
]
GEMINI_MODELS = list(dict.fromkeys(m for m in _models if m))

# --------------------------------------------------------------------------
# NEWS SOURCES
#   official=True  -> company's own blog (small ranking bonus)
#   ai_only=False  -> general feed, so we keep only AI-looking stories
# --------------------------------------------------------------------------
OLSHANSK = "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds"

FEEDS = [
    # --- Official company sources ---
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "company": "OpenAI", "official": True, "ai_only": True},
    {"name": "Anthropic News", "url": f"{OLSHANSK}/feed_anthropic_news.xml", "company": "Anthropic", "official": True, "ai_only": True},
    {"name": "Anthropic Research", "url": f"{OLSHANSK}/feed_anthropic_research.xml", "company": "Anthropic", "official": True, "ai_only": True},
    {"name": "Google AI Blog", "url": "https://blog.google/technology/ai/rss/", "company": "Google", "official": True, "ai_only": True},
    {"name": "Google DeepMind", "url": "https://deepmind.google/blog/rss.xml", "company": "Google", "official": True, "ai_only": True},
    {"name": "Google Research", "url": "https://research.google/blog/rss/", "company": "Google", "official": True, "ai_only": False},
    {"name": "Meta Engineering", "url": "https://engineering.fb.com/feed/", "company": "Meta", "official": True, "ai_only": False},
    {"name": "xAI News", "url": f"{OLSHANSK}/feed_xainews.xml", "company": "xAI", "official": True, "ai_only": True},
    {"name": "Microsoft AI Blog", "url": "https://blogs.microsoft.com/ai/feed/", "company": "Microsoft", "official": True, "ai_only": True},
    {"name": "Microsoft Research", "url": "https://www.microsoft.com/en-us/research/feed/", "company": "Microsoft", "official": True, "ai_only": False},
    {"name": "NVIDIA Blog", "url": "https://blogs.nvidia.com/feed/", "company": "NVIDIA", "official": True, "ai_only": False},
    {"name": "NVIDIA Developer Blog", "url": "https://developer.nvidia.com/blog/feed", "company": "NVIDIA", "official": True, "ai_only": False},
    {"name": "Hugging Face Blog", "url": "https://huggingface.co/blog/feed.xml", "company": "Hugging Face", "official": True, "ai_only": True},
    {"name": "Mistral AI", "url": "https://mistral.ai/news/rss.xml", "company": "Mistral", "official": True, "ai_only": True},
    # --- Reputable tech media ---
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "company": None, "official": False, "ai_only": True},
    {"name": "The Verge AI", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "company": None, "official": False, "ai_only": True},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/", "company": None, "official": False, "ai_only": True},
    {"name": "Ars Technica AI", "url": "https://arstechnica.com/ai/feed/", "company": None, "official": False, "ai_only": True},
    {"name": "MIT Technology Review AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed", "company": None, "official": False, "ai_only": True},
    {"name": "WIRED AI", "url": "https://www.wired.com/feed/tag/ai/latest/rss", "company": None, "official": False, "ai_only": True},
]

COMPANIES = ["OpenAI", "Anthropic", "Google", "Meta", "xAI", "Microsoft",
             "NVIDIA", "Hugging Face", "Mistral", "Other"]

COMPANY_PATTERNS = {
    "OpenAI": r"\bopenai\b|\bchatgpt\b|\bgpt-?\d|\bsora\b",
    "Anthropic": r"\banthropic\b|\bclaude\b",
    "Google": r"\bgoogle\b|\bgemini\b|\bdeepmind\b|\bgemma\b",
    "Meta": r"\bmeta\b|\bllama\b|\bzuckerberg\b",
    "xAI": r"\bxai\b|\bgrok\b",
    "Microsoft": r"\bmicrosoft\b|\bcopilot\b|\bazure\b",
    "NVIDIA": r"\bnvidia\b|\bjensen huang\b",
    "Hugging Face": r"\bhugging ?face\b",
    "Mistral": r"\bmistral\b",
}

COMPANY_HASHTAGS = {
    "OpenAI": ["#OpenAI"],
    "Anthropic": ["#Anthropic", "#Claude"],
    "Google": ["#GoogleAI", "#DeepMind"],
    "Meta": ["#MetaAI"],
    "xAI": ["#xAI"],
    "Microsoft": ["#Microsoft"],
    "NVIDIA": ["#NVIDIA"],
    "Hugging Face": ["#HuggingFace"],
    "Mistral": ["#MistralAI"],
}

AI_KEYWORDS = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|machine learning|deep learning|llm|llms|gpt|chatgpt|"
    r"openai|claude|anthropic|gemini|gemma|deepmind|llama|grok|copilot|mistral|hugging face|"
    r"neural|generative|foundation model|agentic|agents?|inference|diffusion|transformer|"
    r"nemotron|robotics|chatbot|language model)\b",
    re.IGNORECASE,
)

STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
             "is", "are", "by", "at", "as", "from", "its", "new", "how", "why", "what"}


# --------------------------------------------------------------------------
# SMALL HELPERS
# --------------------------------------------------------------------------
def log(msg: str, level: str = "INFO") -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {level}: {msg}", flush=True)


def warn(msg: str) -> None:
    log(msg, "WARN")
    print(f"::warning::{msg}", flush=True)  # shows as a yellow warning in GitHub Actions


def step(title: str) -> None:
    print(f"\n===== {title} =====", flush=True)


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return " ".join(unescape(s).split())


class GeminiFatal(Exception):
    """Raised when Gemini cannot be used at all (e.g. bad API key)."""


@dataclass
class Article:
    title: str
    link: str
    source: str
    published: datetime
    summary: str
    official: bool
    feed_company: str | None
    id: int = 0
    text: str = ""
    verification: str = ""
    is_ai: bool = False
    verifiable: bool = False
    significance: int = 0
    company: str = "Other"
    duplicate_of: int | None = None
    explanation: str = ""
    score: float = 0.0

    def age_hours(self, now: datetime) -> float:
        return (now - self.published).total_seconds() / 3600

    @property
    def diversity_key(self) -> str:
        return self.company if self.company != "Other" else f"Other:{self.source}"


def detect_company(title: str) -> str | None:
    for company, pattern in COMPANY_PATTERNS.items():
        if re.search(pattern, title, re.IGNORECASE):
            return company
    return None


# --------------------------------------------------------------------------
# STEP 1: COLLECT FEEDS
# --------------------------------------------------------------------------
def fetch_one_feed(feed: dict, now: datetime):
    name = feed["name"]
    try:
        resp = requests.get(feed["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # one broken feed must never stop the run
        return name, [], f"FAILED ({type(exc).__name__}: {str(exc)[:120]})"

    if not parsed.entries:
        return name, [], "FAILED (feed opened but contained no entries)"

    articles: list[Article] = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title", ""))
        link = (entry.get("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if not t:
            continue  # no date = we cannot prove it is fresh
        published = datetime(*t[:6], tzinfo=timezone.utc)
        age = (now - published).total_seconds() / 3600
        if age < -2 or age > FALLBACK_HOURS:
            continue
        summary = clean_text(entry.get("summary") or (entry.get("content") or [{}])[0].get("value", ""))
        if not feed["ai_only"] and not AI_KEYWORDS.search(f"{title} {summary}"):
            continue  # general feed and not about AI
        articles.append(Article(title, link, name, published, summary[:1500],
                                feed["official"], feed["company"]))

    articles.sort(key=lambda a: a.published, reverse=True)
    articles = articles[:MAX_PER_FEED]
    return name, articles, f"OK ({len(parsed.entries)} entries, {len(articles)} recent AI candidates)"


def collect(now: datetime) -> list[Article]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda f: fetch_one_feed(f, now), FEEDS))
    all_articles: list[Article] = []
    failed = 0
    for name, arts, status in results:
        log(f"{name:28s} {status}")
        if status.startswith("FAILED"):
            failed += 1
            warn(f"Source unavailable: {name} - {status}")
        all_articles.extend(arts)
    log(f"Feeds OK: {len(FEEDS) - failed}/{len(FEEDS)}. Candidates collected: {len(all_articles)}")
    return all_articles


# --------------------------------------------------------------------------
# STEP 2: REMOVE DUPLICATES
# --------------------------------------------------------------------------
def norm_url(u: str) -> str:
    p = urlparse(u.strip())
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def title_tokens(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOPWORDS}


def is_duplicate(a: Article, b: Article) -> bool:
    if norm_url(a.link) == norm_url(b.link):
        return True
    ta, tb = title_tokens(a.title), title_tokens(b.title)
    if ta and tb and len(ta & tb) / len(ta | tb) >= 0.7:
        return True
    return SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio() >= 0.85


def dedupe(articles: list[Article]) -> list[Article]:
    # Official sources first, then newest, so the best copy of a story is kept.
    ordered = sorted(articles, key=lambda a: (not a.official, -a.published.timestamp()))
    kept: list[Article] = []
    for a in ordered:
        match = next((k for k in kept if is_duplicate(a, k)), None)
        if match:
            log(f"Duplicate dropped: '{a.title[:70]}' ({a.source}) ~ '{match.title[:50]}' ({match.source})")
        else:
            kept.append(a)
    log(f"After duplicate removal: {len(kept)} (removed {len(articles) - len(kept)})")
    return kept


# --------------------------------------------------------------------------
# STEP 3: VERIFY ARTICLES (fetch the page, extract real text)
# --------------------------------------------------------------------------
class _PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: list[str] = []
        self.paras: list[str] = []
        self._skip = 0
        self._in_p = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "meta":
            a = dict(attrs)
            if a.get("content") and (a.get("property") == "og:description"
                                     or a.get("name") in ("description", "twitter:description")):
                self.meta.append(a["content"].strip())
        elif tag == "p" and not self._skip:
            self._in_p, self._buf = True, []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "p" and self._in_p:
            text = " ".join("".join(self._buf).split())
            if len(text) >= 40:
                self.paras.append(text)
            self._in_p = False

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)


def fetch_page_text(url: str) -> tuple[str, str]:
    """Returns (text, status). status: ok | dead | blocked | unreachable | nonhtml."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception:
        return "", "unreachable"
    if r.status_code in (404, 410):
        return "", "dead"
    if r.status_code >= 400:
        return "", "blocked"
    if "html" not in r.headers.get("Content-Type", "").lower():
        return "", "nonhtml"
    parser = _PageText()
    try:
        parser.feed(r.text[:600_000])
    except Exception:
        pass
    text = " ".join(parser.meta[:1] + parser.paras[:12])
    return text[:1500], "ok"


def verify(articles: list[Article]) -> list[Article]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = list(pool.map(lambda a: fetch_page_text(a.link), articles))
    verified: list[Article] = []
    for art, (page_text, status) in zip(articles, pages):
        if status == "dead":
            log(f"Excluded (link is dead): {art.title[:70]}")
            continue
        if len(page_text) >= 200:
            art.text, art.verification = page_text, "article page"
        else:
            art.text = f"{page_text} {art.summary}".strip()
            art.verification = "feed summary"
        if len(art.text) < MIN_TEXT_CHARS:
            log(f"Excluded (not enough verifiable text): {art.title[:70]}")
            continue
        verified.append(art)
    log(f"Verified articles: {len(verified)}/{len(articles)}")
    return verified


# --------------------------------------------------------------------------
# GEMINI
# --------------------------------------------------------------------------
_dead_models: set[str] = set()


def call_gemini(prompt: str, max_tokens: int = 8192) -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise GeminiFatal("GEMINI_API_KEY is missing.")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens,
                             "responseMimeType": "application/json"},
    }
    last_error = "unknown error"
    for model in GEMINI_MODELS:
        if model in _dead_models:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(1, 4):
            try:
                r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                  json=body, timeout=120)
            except requests.RequestException as exc:
                last_error = f"{model}: network error {exc}"
                time.sleep(5 * attempt)
                continue
            if r.status_code == 200:
                data = r.json()
                try:
                    parts = data["candidates"][0]["content"]["parts"]
                    return "".join(p.get("text", "") for p in parts)
                except (KeyError, IndexError):
                    last_error = f"{model}: empty/blocked response"
                    break
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 15 * attempt
                last_error = f"{model}: HTTP {r.status_code}"
                log(f"Gemini {model} HTTP {r.status_code} - retry {attempt}/3 in {wait}s", "WARN")
                time.sleep(wait)
                continue
            if r.status_code == 404:
                log(f"Model {model} not available - trying next model", "WARN")
                _dead_models.add(model)
                break
            if r.status_code in (400, 401, 403) and re.search(r"API key|permission|PERMISSION", r.text):
                raise GeminiFatal(f"Gemini rejected the API key (HTTP {r.status_code}). Check the GEMINI_API_KEY secret.")
            last_error = f"{model}: HTTP {r.status_code} {r.text[:200]}"
            break
        log(f"Giving up on model {model}; trying the next one", "WARN")
    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


def parse_json(raw: str):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def numbers_in(s: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s)}


def is_grounded(explanation: str, source_text: str) -> bool:
    """Every number in the explanation must appear in the source text."""
    src = source_text.replace(",", "")
    return all(n in src for n in numbers_in(explanation))


# --------------------------------------------------------------------------
# STEP 4: ANALYSE WITH GEMINI
# --------------------------------------------------------------------------
def build_analysis_prompt(batch: list[Article]) -> str:
    blocks = []
    for a in batch:
        blocks.append(f"[ID {a.id}]\nSOURCE: {a.source}\nTITLE: {a.title}\nTEXT: {a.text[:1200]}")
    return f"""You are a careful, factual AI-news analyst. For each article below you know ONLY what is
written in its TITLE and TEXT.

STRICT RULES
- Use ONLY facts that appear in TITLE/TEXT. Never add facts, numbers, dates, names or claims from memory.
- "is_ai_news": true only if the article is mainly about artificial intelligence (models, AI products,
  AI research, AI chips/infrastructure, AI policy or business). False for general tech or unrelated news.
- "verifiable": true only if TEXT clearly describes a concrete development (launch, release, research
  result, funding, partnership, policy). False if vague, opinion-only, promotional, or too thin to
  summarise without guessing.
- "significance": integer 1-10 (10 = major industry-wide development such as a frontier model release,
  very large funding deal or major regulation; 1 = minor).
- "company": the main company the story is about, exactly one of {COMPANIES}.
- "duplicate_of": the ID of ANOTHER article in this list covering the exact same event, otherwise null.
- "explanation": 1-2 plain-English sentences (max 50 words) saying what happened and why it matters,
  based strictly on TEXT. Use "" if is_ai_news or verifiable is false.

Return ONLY a JSON array, one object per article, like:
[{{"id": 1, "is_ai_news": true, "verifiable": true, "significance": 7, "company": "OpenAI", "duplicate_of": null, "explanation": "..."}}]

ARTICLES
{chr(10).join(blocks)}
"""


def analyse(articles: list[Article]) -> None:
    by_id = {a.id: a for a in articles}
    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i:i + BATCH_SIZE]
        log(f"Gemini analysis batch {i // BATCH_SIZE + 1}: {len(batch)} articles")
        try:
            results = parse_json(call_gemini(build_analysis_prompt(batch)))
            if isinstance(results, dict):
                results = next((v for v in results.values() if isinstance(v, list)), [])
        except GeminiFatal:
            raise
        except Exception as exc:
            warn(f"Gemini batch failed and was skipped: {exc}")
            continue
        for item in results:
            try:
                art = by_id.get(int(item.get("id")))
                if art is None:
                    continue
                art.is_ai = bool(item.get("is_ai_news"))
                art.verifiable = bool(item.get("verifiable"))
                art.significance = max(1, min(10, int(item.get("significance", 1))))
                llm_company = item.get("company")
                art.company = llm_company if llm_company in COMPANIES else "Other"
                dup = item.get("duplicate_of")
                art.duplicate_of = int(dup) if dup not in (None, "", "null") else None
                art.explanation = " ".join(str(item.get("explanation", "")).split())
            except (TypeError, ValueError):
                continue
        time.sleep(3)  # be gentle with free-tier rate limits


# --------------------------------------------------------------------------
# STEP 5: FILTER, RANK, SELECT WITH DIVERSITY
# --------------------------------------------------------------------------
def rank_and_select(articles: list[Article], now: datetime) -> list[Article]:
    usable: list[Article] = []
    for a in articles:
        # Final company: official feed > Gemini's answer > keyword guess > Other
        if a.feed_company:
            a.company = a.feed_company
        elif a.company == "Other":
            a.company = detect_company(a.title) or "Other"

        if not a.is_ai:
            log(f"Excluded (not AI news): {a.title[:70]}")
        elif not a.verifiable or not a.explanation:
            log(f"Excluded (could not be verified): {a.title[:70]}")
        elif a.significance < MIN_SIGNIFICANCE:
            log(f"Excluded (low significance {a.significance}): {a.title[:70]}")
        elif len(a.explanation) > 500:
            log(f"Excluded (explanation too long): {a.title[:70]}")
        elif not is_grounded(a.explanation, f"{a.title} {a.text}"):
            log(f"Excluded (explanation has numbers not found in the article): {a.title[:70]}")
        else:
            freshness = max(0.0, 10 - a.age_hours(now) / 4)
            a.score = a.significance * 10 + (6 if a.official else 0) + freshness
            usable.append(a)
    usable.sort(key=lambda a: a.score, reverse=True)

    # Drop duplicates that Gemini spotted (keep the higher-scored one).
    kept: list[Article] = []
    kept_ids: set[int] = set()
    for a in usable:
        if a.duplicate_of in kept_ids or any(k.duplicate_of == a.id for k in kept):
            log(f"Duplicate dropped (found by Gemini): {a.title[:70]}")
            continue
        kept.append(a)
        kept_ids.add(a.id)
    log(f"Usable stories after analysis: {len(kept)}")

    # Select with diversity. Fresh stories first; older ones only to fill gaps.
    selected: list[Article] = []
    counts: dict[str, int] = {}
    for cap in DIVERSITY_CAPS:
        for max_age in (FRESH_HOURS, FALLBACK_HOURS):
            for a in kept:
                if len(selected) >= TOP_N:
                    break
                if a in selected or a.age_hours(now) > max_age:
                    continue
                if counts.get(a.diversity_key, 0) >= cap:
                    continue
                selected.append(a)
                counts[a.diversity_key] = counts.get(a.diversity_key, 0) + 1
                if max_age == FALLBACK_HOURS and a.age_hours(now) > FRESH_HOURS:
                    log(f"Using older story ({a.age_hours(now):.0f}h old) to fill the list: {a.title[:60]}")
        if len(selected) >= TOP_N:
            break
    selected.sort(key=lambda a: a.score, reverse=True)
    return selected


# --------------------------------------------------------------------------
# STEP 6: BUILD THE POST
# --------------------------------------------------------------------------
def make_headline_and_closing(stories: list[Article], date_str: str) -> tuple[str, str]:
    fallback = (f"Top {len(stories)} AI updates today ({date_str})",
                "That's your daily AI roundup! Follow this page so you never miss an update.")
    items = "\n".join(f"{i}. {s.title} - {s.explanation}" for i, s in enumerate(stories, 1))
    prompt = f"""Below are today's top AI news items. Write a headline and a closing line for a Facebook post.

RULES
- Use ONLY facts contained in the items. Do not add new facts or numbers.
- headline: max 90 characters, engaging, no hashtags, no emojis, no quotation marks.
- closing_line: max 25 words, friendly, invites readers to follow for tomorrow's update. No hashtags.

Return ONLY JSON: {{"headline": "...", "closing_line": "..."}}

ITEMS
{items}
"""
    try:
        data = parse_json(call_gemini(prompt, max_tokens=1024))
        headline = " ".join(str(data["headline"]).split())
        closing = " ".join(str(data["closing_line"]).split())
        source_blob = " ".join(f"{s.title} {s.explanation} {s.text}" for s in stories)
        if not (10 <= len(headline) <= 120) or not (5 <= len(closing) <= 200):
            raise ValueError("headline/closing length out of range")
        if not is_grounded(headline + " " + closing, source_blob):
            raise ValueError("headline/closing contains unsupported numbers")
        return headline, closing
    except GeminiFatal:
        raise
    except Exception as exc:
        warn(f"Using fallback headline/closing ({exc})")
        return fallback


def make_hashtags(stories: list[Article]) -> str:
    tags = ["#AI", "#ArtificialIntelligence", "#AINews", "#TechNews"]
    for company in dict.fromkeys(s.company for s in stories):
        tags.extend(COMPANY_HASHTAGS.get(company, []))
    return " ".join(dict.fromkeys(tags)[:14] if False else list(dict.fromkeys(tags))[:14])


def build_post(stories: list[Article], headline: str, closing: str, date_str: str) -> str:
    lines = [f"🤖 {headline}", f"📅 {date_str} | Daily AI News Roundup", ""]
    for i, s in enumerate(stories, 1):
        lines += [f"{i}. {s.title}", s.explanation, f"🔗 Source: {s.source} - {s.link}", ""]
    lines += [closing, "", make_hashtags(stories)]
    return "\n".join(lines).strip() + "\n"

# --------------------------------------------------------------------------
# STEP 7: PUBLISH TO FACEBOOK PAGE (official Graph API, text-only post)
# --------------------------------------------------------------------------
GRAPH_VERSION_RE = re.compile(r"^v\d+\.\d+$")
PUBLISH_STATE_FILE = OUTPUT_DIR / "facebook_published.json"

META_ERROR_HINTS = {
    190: "Token is invalid/expired/revoked. Regenerate the System User token in Business Settings and update META_ACCESS_TOKEN.",
    10: "Permission missing. Check pages_manage_posts is on the token and the Page is assigned to the System User with Content (CREATE_CONTENT) access.",
    200: "Permission denied. Check pages_manage_posts and that the System User has Content access on this Page.",
    100: "Invalid parameter. Check META_PAGE_ID and that this Page is assigned to the System User.",
    368: "Facebook temporarily blocked this action (spam/abuse filter).",
    506: "Facebook rejected this as a duplicate post.",
}


class FacebookPublishError(Exception):
    """Raised when the post could not be published to Facebook."""


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _load_publish_state() -> dict:
    try:
        data = json.loads(PUBLISH_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _meta_error_message(resp: requests.Response, *secrets: str) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        code = err.get("code")
        hint = META_ERROR_HINTS.get(code, "")
        msg = (f"Meta API error (HTTP {resp.status_code}, code {code}, subcode {err.get('error_subcode')}, "
               f"type {err.get('type')}): {err.get('message', '')} {hint} [fbtrace_id={err.get('fbtrace_id')}]")
    else:
        msg = f"Unexpected response (HTTP {resp.status_code}): {resp.text[:300]}"
    return _redact(" ".join(msg.split()), *secrets)


def _get_page_token(base_url: str, page_id: str, token: str) -> str:
    """A System User token is not a Page token. Ask Meta for the Page's own token.
    If Meta returns none (token already is a Page token), use the given token directly."""
    try:
        r = requests.get(f"{base_url}/{page_id}",
                         params={"fields": "access_token", "access_token": token}, timeout=30)
    except requests.RequestException as exc:
        raise FacebookPublishError(
            _redact(f"Network error while requesting the Page token ({type(exc).__name__}). "
                    "Nothing was posted; safe to re-run.", token)
        ) from None
    if r.ok:
        try:
            data = r.json()
        except ValueError:
            data = {}
        page_token = data.get("access_token") if isinstance(data, dict) else None
        if page_token:
            print(f"::add-mask::{page_token}", flush=True)  # hide derived token in GitHub logs too
            log("Using Page access token derived from META_ACCESS_TOKEN.")
            return page_token
        log("Meta returned no separate Page token; using META_ACCESS_TOKEN directly.")
        return token
    warn(_meta_error_message(r, token) + " | Page-token lookup failed; trying META_ACCESS_TOKEN directly.")
    return token


def publish_to_facebook(post_text: str, now_ist: datetime) -> str:
    """Publishes post_text to the Page. Returns the Facebook post id.
    Raises FacebookPublishError on any failure. Never logs any token."""
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    page_id = os.environ.get("META_PAGE_ID", "").strip()
    version = os.environ.get("META_GRAPH_VERSION", "").strip()
    missing = [n for n, v in (("META_ACCESS_TOKEN", token), ("META_PAGE_ID", page_id),
                              ("META_GRAPH_VERSION", version)) if not v]
    if missing:
        raise FacebookPublishError(
            f"Missing environment variable(s): {', '.join(missing)}. META_ACCESS_TOKEN and META_PAGE_ID are "
            "repository secrets; META_GRAPH_VERSION is a repository variable (e.g. v25.0 - use the version "
            "shown in your Meta App Dashboard). All three must be passed in the workflow.")
    if not GRAPH_VERSION_RE.match(version):
        raise FacebookPublishError(f"META_GRAPH_VERSION must look like 'v25.0', got '{version}'.")
    if not page_id.isdigit():
        raise FacebookPublishError("META_PAGE_ID must be the numeric Page ID (check for typos/extra text).")
    if not post_text.strip():
        raise FacebookPublishError("Post text is empty - nothing to publish.")

    # ---- Duplicate prevention: one post per IST day, and never the same exact text twice ----
    date_key = now_ist.strftime("%Y-%m-%d")
    text_hash = hashlib.sha256(post_text.encode("utf-8")).hexdigest()
    state = _load_publish_state()
    if os.environ.get("FORCE_PUBLISH", "0").strip() != "1":
        if state.get("ist_date") == date_key or state.get("text_sha256") == text_hash:
            log(f"Already published for {state.get('ist_date')} (post id {state.get('post_id')}). "
                "Skipping Facebook publish to avoid a duplicate.")
            return str(state.get("post_id", ""))
    else:
        log("FORCE_PUBLISH=1 - duplicate protection bypassed.", "WARN")

    # ---- Get the Page token, then publish (token goes in the POST body, never logged) ----
    base_url = f"https://graph.facebook.com/{version}"
    page_token = _get_page_token(base_url, page_id, token)
    try:
        resp = requests.post(f"{base_url}/{page_id}/feed",
                             data={"message": post_text, "access_token": page_token}, timeout=30)
    except requests.RequestException as exc:
        # No automatic retry: a timeout may still have created the post, and a retry could duplicate it.
        raise FacebookPublishError(
            _redact(f"Network error while calling Meta API ({type(exc).__name__}). "
                    "Check the Page before re-running - the post may have gone through.", token, page_token)
        ) from None

    if not resp.ok:
        raise FacebookPublishError(_meta_error_message(resp, token, page_token))
    try:
        data = resp.json()
    except ValueError:
        data = {}
    post_id = data.get("id") if isinstance(data, dict) else None
    if not post_id:
        raise FacebookPublishError(_meta_error_message(resp, token, page_token))

    # ---- Remember it so a re-run does not post again ----
    try:
        PUBLISH_STATE_FILE.parent.mkdir(exist_ok=True)
        PUBLISH_STATE_FILE.write_text(json.dumps({
            "ist_date": date_key,
            "post_id": post_id,
            "text_sha256": text_hash,
            "published_at_ist": now_ist.isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
    except OSError as exc:
        warn(f"Post {post_id} was published but the duplicate-protection file could not be saved: {exc}")
    return str(post_id)


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def main() -> int:
    step("START")
    now = datetime.now(timezone.utc)
    now_ist = now.astimezone(IST)
    date_str = now_ist.strftime("%d %b %Y")
    log(f"Run time: {now_ist:%Y-%m-%d %H:%M} IST | Gemini models: {', '.join(GEMINI_MODELS)}")
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        log("GEMINI_API_KEY is not set. Add it as a GitHub Secret (see README).", "ERROR")
        return 1

    step("1. COLLECTING FEEDS")
    articles = collect(now)
    if not articles:
        log("No recent articles found from any source.", "ERROR")
        return 1

    step("2. REMOVING DUPLICATES")
    articles = dedupe(articles)
    articles.sort(key=lambda a: (not a.official, -a.published.timestamp()))
    articles = articles[:MAX_CANDIDATES]

    step("3. VERIFYING ARTICLES")
    articles = verify(articles)
    for n, a in enumerate(articles, 1):
        a.id = n
    if not articles:
        log("No verifiable articles.", "ERROR")
        return 1

    step("4. ANALYSING WITH GEMINI")
    try:
        analyse(articles)
    except GeminiFatal as exc:
        log(str(exc), "ERROR")
        return 1

    step("5. RANKING AND SELECTING")
    stories = rank_and_select(articles, now)
    if not stories:
        log("No stories passed all checks. Output files were NOT changed.", "ERROR")
        return 1
    if len(stories) < TOP_N:
        warn(f"Only {len(stories)} stories passed all checks (wanted {TOP_N}). Posting only verified ones.")
    for i, s in enumerate(stories, 1):
        log(f"#{i} [{s.company}] score={s.score:.1f} age={s.age_hours(now):.0f}h "
            f"({s.verification}) {s.title[:70]} - {s.source}")

    step("6. WRITING THE POST")
    try:
        headline, closing = make_headline_and_closing(stories, date_str)
    except GeminiFatal as exc:
        log(str(exc), "ERROR")
        return 1
    post = build_post(stories, headline, closing, date_str)

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "daily_post.txt").write_text(post, encoding="utf-8")
    news = {
        "generated_at_ist": now_ist.isoformat(timespec="seconds"),
        "story_count": len(stories),
        "stories": [
            {
                "rank": i,
                "title": s.title,
                "company": s.company,
                "source": s.source,
                "url": s.link,
                "published_utc": s.published.isoformat(timespec="seconds"),
                "significance": s.significance,
                "score": round(s.score, 1),
                "verification": s.verification,
                "explanation": s.explanation,
            }
            for i, s in enumerate(stories, 1)
        ],
    }
    (OUTPUT_DIR / "daily_news.json").write_text(json.dumps(news, indent=2, ensure_ascii=False), encoding="utf-8")
    log("Saved output/daily_post.txt and output/daily_news.json")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(f"## Daily AI post ({date_str})\n\n```\n{post}```\n")

        step("FINAL POST")
    print(post)

    step("7. PUBLISHING TO FACEBOOK")
    try:
        post_id = publish_to_facebook(post, now_ist)
    except FacebookPublishError as exc:
        log(str(exc), "ERROR")
        print(f"::error::Facebook publishing failed: {exc}", flush=True)
        return 1
    log(f"Facebook post id: {post_id}")

    step("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
