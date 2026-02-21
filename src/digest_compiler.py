"""Compile parsed articles into a single source document for NotebookLM."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from src.exceptions import DigestCompileError
from src.models import Article, CompiledDigest, DailyDigest
from src.topic_classifier import SEGMENT_DURATIONS, SEGMENT_ORDER, Topic

logger = logging.getLogger(__name__)

# NotebookLM source limit (100K characters)
MAX_SOURCE_CHARS = 100_000

# Approximate words per minute for podcast speech
WORDS_PER_MINUTE = 150

PODCAST_PREAMBLE = """\
**PODCAST PRODUCTION INSTRUCTIONS:**
This is a pre-written podcast script for "The Hootline".
- Start with a warm welcome, mention today's date ({date}), and briefly \
mention the weather in Seattle
- The script flows as a continuous narrative — do NOT announce topic \
changes mechanically ("now let's talk about...")
- Blend between topics with natural transitions and conversational bridges
- Use a conversational but informative two-host format
- Summarize and discuss the key points, do not read them verbatim
- Always end on a positive, uplifting note before saying goodbye
"""

INTRO_SECTION = """\
## INTRO (~1 minute)
Welcome to The Hootline! Today is {date}. {weather}Let's get into what's happening.
"""

OUTRO_SECTION = """\
## OUTRO (~1 minute)
And that wraps up today's Hootline! We hope you found something in there \
that made you smile, think, or learn something new. Remember — every day \
brings new possibilities. Thanks for listening, and we'll see you next time. Bye!
"""

SUMMARIZATION_SYSTEM_PROMPT = """\
You are a script writer for "The Hootline", a daily news podcast.
Write a segment that flows as part of a larger narrative — not a \
standalone block. Synthesize the key points conversationally.
Do NOT use bullet points or list format. Do NOT include host labels \
like "[Host 1]:". Write as flowing prose that two hosts can naturally \
discuss. Stay within the word budget."""


def _fetch_seattle_weather() -> str:
    """Fetch current Seattle weather from wttr.in. Returns a brief description or empty string."""
    try:
        resp = requests.get(
            "https://wttr.in/Seattle?format=j1",
            timeout=5,
            headers={"User-Agent": "NoctuaPodcast/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        current = data["current_condition"][0]
        temp_f = current["temp_F"]
        desc = current["weatherDesc"][0]["value"]
        return f"It's {temp_f}\u00b0F and {desc.lower()} here in Seattle. "
    except Exception as e:
        logger.warning("Weather fetch failed: %s — skipping weather", e)
        return ""


def _build_topics_summary(digest: DailyDigest, segment_counts: dict[str, int]) -> str:
    """Build a brief topics summary reflecting active segments."""
    parts = []
    for topic in SEGMENT_ORDER:
        count = segment_counts.get(topic.value, 0)
        if count > 0:
            parts.append(f"{topic.value} ({count})")
    return "; ".join(parts) if parts else "No segments"


def _generate_rss_summary(articles: list[Article]) -> str:
    """Generate a short (~15 word) episode description using the Claude API."""
    from src.llm_client import call_haiku
    from src.exceptions import ClaudeAPIError

    titles = [a.title for a in articles[:30]]
    titles_text = "\n".join(f"- {t}" for t in titles)

    try:
        summary = call_haiku(
            system="You write concise podcast episode descriptions.",
            user_message=(
                "Below are today's podcast episode article titles.\n\n"
                f"{titles_text}\n\n"
                "Write a single sentence (~15 words) summarizing what "
                "this episode covers. No quotes, no preamble — just the sentence."
            ),
            max_tokens=60,
            temperature=0.0,
            timeout=15,
        )
        summary = summary.strip().rstrip(".")
        if len(summary.split()) > 25:
            summary = " ".join(summary.split()[:20])
        return summary
    except (ClaudeAPIError, Exception) as e:
        logger.warning("RSS summary generation failed: %s — using fallback", e)
        return "Your nightly knowledge briefing from The Hootline."


def _summarize_segment(topic: Topic, articles: list[Article], word_budget: int) -> str | None:
    """Use Claude Sonnet to write a narrative summary for a topic segment.

    Returns the summary text, or None if the API call fails.
    """
    from src.llm_client import call_sonnet
    from src.exceptions import ClaudeAPIError

    article_texts = []
    for a in articles:
        content_preview = a.content[:1500]
        article_texts.append(f"### {a.title}\nSource: {a.source}\n{content_preview}")
    combined = "\n\n---\n\n".join(article_texts)

    user_message = (
        f"Topic: {topic.value}\n"
        f"Word budget: ~{word_budget} words\n\n"
        f"Articles:\n\n{combined}\n\n"
        f"Write a flowing narrative segment (~{word_budget} words) covering the key points from these articles."
    )

    try:
        return call_sonnet(
            system=SUMMARIZATION_SYSTEM_PROMPT,
            user_message=user_message,
            max_tokens=word_budget * 3,
            temperature=0.3,
            timeout=60,
        )
    except ClaudeAPIError as e:
        logger.warning("Segment summarization failed for %s: %s — using raw text", topic.value, e)
        return None


def _parse_minutes(topic: Topic) -> int:
    """Extract the integer minutes from a SEGMENT_DURATIONS entry."""
    dur_str = SEGMENT_DURATIONS.get(topic, "~1 minute")
    return int(dur_str.replace("~", "").replace(" minutes", "").replace(" minute", ""))


def _allocate_budget(articles: list[Article], budget: int) -> dict[int, int]:
    """Allocate a character budget across articles."""
    sizes = {i: len(a.content) for i, a in enumerate(articles)}
    allocated: dict[int, int] = {}
    remaining_budget = budget
    remaining_ids = set(sizes.keys())

    while remaining_ids:
        share = remaining_budget // len(remaining_ids) if remaining_ids else 0
        settled = set()
        for i in remaining_ids:
            if sizes[i] <= share:
                allocated[i] = sizes[i]
                remaining_budget -= sizes[i]
                settled.add(i)
        remaining_ids -= settled
        if not settled:
            for i in remaining_ids:
                allocated[i] = share
            break

    return allocated


def _raw_fallback_segment(articles: list[Article], word_budget: int) -> str:
    """Build raw text fallback for a segment when AI summarization is unavailable."""
    chars_per_word = 6
    overhead_per_article = 60
    segment_char_budget = max(
        word_budget * chars_per_word - overhead_per_article * len(articles),
        len(articles) * 200,
    )
    article_budgets = _allocate_budget(articles, segment_char_budget)

    parts = []
    for i, article in enumerate(articles):
        content = article.content
        cap = article_budgets.get(i, len(content))
        if len(content) > cap:
            content = content[:cap].rsplit("\n", 1)[0] + "\n[...]"
        parts.append(f"### {article.title}\n{content}")

    return "\n\n---\n\n".join(parts)


def _compile_text(
    digest: DailyDigest, date_str: str
) -> tuple[str, dict[str, int], dict[str, list[str]]]:
    """Compile articles into a segment-structured markdown document with AI summaries."""
    # Group articles by topic
    grouped: dict[str, list[Article]] = defaultdict(list)
    for article in digest.articles:
        topic_key = article.topic if article.topic else Topic.OTHER.value
        grouped[topic_key].append(article)

    # Calculate word budget per segment
    segment_word_budgets: dict[str, int] = {}
    for topic in SEGMENT_ORDER:
        mins = _parse_minutes(topic)
        segment_word_budgets[topic.value] = mins * WORDS_PER_MINUTE

    # Cap articles per topic
    total_before = sum(len(v) for v in grouped.values())
    for topic in SEGMENT_ORDER:
        mins = _parse_minutes(topic)
        max_articles = max(2, round(mins * 1.5))
        articles_list = grouped.get(topic.value, [])
        if len(articles_list) > max_articles:
            logger.info(
                "Capping %s from %d to %d articles (allocated %dm)",
                topic.value, len(articles_list), max_articles, mins,
            )
            grouped[topic.value] = articles_list[:max_articles]
    total_after = sum(len(v) for v in grouped.values())
    if total_after < total_before:
        logger.info("Topic capping: %d -> %d articles", total_before, total_after)

    # Fetch weather for intro
    weather = _fetch_seattle_weather()

    # Build the document
    intro = INTRO_SECTION.format(date=date_str, weather=weather)
    outro = OUTRO_SECTION
    preamble = PODCAST_PREAMBLE.format(date=date_str)

    sections = [f"# The Hootline — Daily Briefing — {date_str}\n"]
    sections.append(preamble)
    sections.append(intro)

    segment_counts: dict[str, int] = {}
    segment_sources: dict[str, list[str]] = {}
    segment_number = 0

    for topic in SEGMENT_ORDER:
        articles = grouped.get(topic.value, [])
        if not articles:
            continue

        segment_number += 1
        segment_counts[topic.value] = len(articles)
        sources = list(dict.fromkeys(a.source for a in articles))
        segment_sources[topic.value] = sources

        mins = _parse_minutes(topic)
        word_budget = segment_word_budgets[topic.value]
        duration = SEGMENT_DURATIONS.get(topic, "")
        duration_label = f" ({duration})" if duration else ""

        section_header = f"## SEGMENT {segment_number}: {topic.value}{duration_label}"
        section_header += f"\n**Word budget: ~{word_budget} words**"
        sections.append(section_header)

        # Try AI summarization, fall back to raw text
        summary = _summarize_segment(topic, articles, word_budget)
        if summary:
            sections.append(summary)
        else:
            sections.append(_raw_fallback_segment(articles, word_budget))

        sections.append("\n---\n")

    sections.append(outro)
    text = "\n\n".join(sections)

    # Final safety check against NotebookLM limit
    if len(text) > MAX_SOURCE_CHARS:
        logger.warning(
            "Compiled text %d chars exceeds %d limit, truncating",
            len(text), MAX_SOURCE_CHARS,
        )
        text = text[:MAX_SOURCE_CHARS - 100] + "\n\n[Document truncated to fit source limit.]"

    return text, segment_counts, segment_sources


def compile(digest: DailyDigest) -> CompiledDigest:
    """Compile all articles into a single well-structured text document."""
    if not digest.articles:
        raise DigestCompileError("No articles to compile.")

    PST = timezone(timedelta(hours=-8))
    episode_date = datetime.now(PST).date()
    date_ymd = episode_date.strftime("%Y-%m-%d")
    date_display = episode_date.strftime("%B %-d, %Y")

    try:
        text, segment_counts, segment_sources = _compile_text(digest, date_display)
        topics_summary = _build_topics_summary(digest, segment_counts)
        rss_summary = _generate_rss_summary(digest.articles)
        total_words = len(text.split())

        compiled = CompiledDigest(
            text=text,
            article_count=len(digest.articles),
            total_words=total_words,
            date=date_ymd,
            topics_summary=topics_summary,
            rss_summary=rss_summary,
            segment_counts=segment_counts,
            segment_sources=segment_sources,
        )

        logger.info(
            "Compiled digest: %d articles, %d words, %d chars, %d segments",
            compiled.article_count,
            compiled.total_words,
            len(compiled.text),
            len(compiled.segment_counts),
        )

        return compiled

    except DigestCompileError:
        raise
    except Exception as e:
        raise DigestCompileError(f"Failed to compile digest: {e}") from e
