"""Compile parsed articles into a single source document for NotebookLM."""

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from config import ShowConfig, ShowFormat, SHOW_FORMATS
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
This is a pre-written podcast script for "{podcast_name}".
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
## INTRO (~{intro_dur} minute)
Welcome to {podcast_name}! Today is {date}. {weather}Let's get into what's happening.
"""

INTRO_SECTION_SHORT = """\
## INTRO (~30 seconds)
Welcome to {podcast_name}! It's {date}. {weather}Here's what's happening.
"""

OUTRO_SECTION = """\
## OUTRO (~{outro_dur} minute)
And that wraps up today's {podcast_name}! We hope you found something in there \
that made you smile, think, or learn something new. Remember — every day \
brings new possibilities. Thanks for listening, and we'll see you next time. Bye!
"""

OUTRO_SECTION_SHORT = """\
## OUTRO (~30 seconds)
That's your morning briefing from {podcast_name}! Thanks for listening — see you tomorrow.
"""

SUMMARIZATION_SYSTEM_PROMPT_TEMPLATE = """\
You are a script writer for "{podcast_name}", a daily news podcast.
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


def _build_topics_summary(digest: DailyDigest, segment_counts: dict[str, int],
                          show_format: ShowFormat | None = None) -> str:
    """Build a brief topics summary reflecting active segments."""
    if show_format:
        topic_order = show_format.segment_order
    else:
        topic_order = [t.value for t in SEGMENT_ORDER]
    parts = []
    for topic_name in topic_order:
        count = segment_counts.get(topic_name, 0)
        if count > 0:
            parts.append(f"{topic_name} ({count})")
    return "; ".join(parts) if parts else "No segments"


def _summarize_all_segments(
    grouped: dict[str, list[Article]],
    segment_word_budgets: dict[str, int],
    segment_order: list[str],
    podcast_name: str = "The Hootline",
) -> tuple[dict[str, str], str] | None:
    """Summarize all segments and generate RSS summary in a single API call.

    Returns (segment_texts, rss_summary) or None if the call fails.
    """
    from src.llm_client import call_sonnet
    from src.exceptions import ClaudeAPIError

    system_prompt = SUMMARIZATION_SYSTEM_PROMPT_TEMPLATE.format(podcast_name=podcast_name)

    # Build the prompt with all segments
    parts = []
    segment_number = 0
    total_word_budget = 0
    for topic_name in segment_order:
        articles = grouped.get(topic_name, [])
        if not articles:
            continue
        segment_number += 1
        word_budget = segment_word_budgets.get(topic_name, 150)
        total_word_budget += word_budget

        article_texts = []
        for a in articles:
            content_preview = a.content[:1500]
            article_texts.append(f"### {a.title}\nSource: {a.source}\n{content_preview}")
        combined = "\n\n".join(article_texts)

        parts.append(
            f"## SEGMENT {segment_number}: {topic_name}\n"
            f"Word budget: ~{word_budget} words\n\n"
            f"Articles:\n{combined}"
        )

    if not parts:
        return None

    # Collect article titles for RSS summary
    all_titles = []
    for topic_name in segment_order:
        for a in grouped.get(topic_name, []):
            all_titles.append(a.title)
    titles_text = "\n".join(f"- {t}" for t in all_titles[:30])

    user_message = (
        "Write ALL of the following podcast segment narratives. "
        "For each segment, write a flowing prose narrative (no bullet points, no host labels) "
        "that stays within its word budget. "
        "Use the exact header format shown (## SEGMENT N: Topic) for each segment.\n\n"
        + "\n\n---\n\n".join(parts)
        + "\n\n---RSS_SUMMARY---\n"
        "Finally, after the delimiter above, write a single sentence (~15 words) "
        "summarizing what this episode covers. No quotes, no preamble — just the sentence.\n\n"
        f"Article titles for reference:\n{titles_text}"
    )

    try:
        response = call_sonnet(
            system=system_prompt,
            user_message=user_message,
            max_tokens=total_word_budget * 3 + 100,
            temperature=0.3,
            timeout=120,
        )
    except ClaudeAPIError as e:
        logger.warning("Single-call summarization failed: %s — using raw fallback", e)
        return None

    # Parse the response into per-segment texts and RSS summary
    segment_texts: dict[str, str] = {}
    rss_summary = ""

    # Split off RSS summary
    if "---RSS_SUMMARY---" in response:
        body, rss_part = response.split("---RSS_SUMMARY---", 1)
        rss_summary = rss_part.strip().rstrip(".")
        if len(rss_summary.split()) > 25:
            rss_summary = " ".join(rss_summary.split()[:20])
    else:
        body = response

    # Parse segment blocks by header pattern
    segment_pattern = re.compile(r"## SEGMENT \d+:\s*(.+)")
    current_topic = None
    current_lines: list[str] = []

    for line in body.split("\n"):
        match = segment_pattern.match(line.strip())
        if match:
            # Save previous segment
            if current_topic is not None:
                segment_texts[current_topic] = "\n".join(current_lines).strip()
            current_topic = match.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Save last segment
    if current_topic is not None:
        segment_texts[current_topic] = "\n".join(current_lines).strip()

    logger.info(
        "Single API call produced %d segment summaries (RSS summary: %s)",
        len(segment_texts),
        "yes" if rss_summary else "no",
    )

    return segment_texts, rss_summary


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
    digest: DailyDigest, date_str: str, podcast_name: str = "The Hootline",
    show_format: ShowFormat | None = None,
) -> tuple[str, dict[str, int], dict[str, list[str]], str]:
    """Compile articles into a segment-structured markdown document with AI summaries.

    Returns (text, segment_counts, segment_sources, rss_summary).
    """
    # Resolve segment config: use show format if provided, else global defaults
    if show_format:
        format_segment_order = show_format.segment_order
        format_durations = show_format.segment_durations
    else:
        format_segment_order = [t.value for t in SEGMENT_ORDER]
        format_durations = {t.value: int(d.replace("~", "").replace(" minutes", "").replace(" minute", ""))
                           for t, d in SEGMENT_DURATIONS.items()}

    # Group articles by topic, only keeping topics in this show's format
    allowed_topics = set(format_segment_order)
    grouped: dict[str, list[Article]] = defaultdict(list)
    for article in digest.articles:
        topic_key = article.topic if article.topic else Topic.OTHER.value
        if topic_key in allowed_topics:
            grouped[topic_key].append(article)

    # Calculate word budget per segment
    segment_word_budgets: dict[str, int] = {}
    for topic_name in format_segment_order:
        mins = format_durations.get(topic_name, 1)
        segment_word_budgets[topic_name] = mins * WORDS_PER_MINUTE

    # Cap articles per topic
    total_before = sum(len(v) for v in grouped.values())
    for topic_name in format_segment_order:
        mins = format_durations.get(topic_name, 1)
        max_articles = max(2, round(mins * 1.5))
        articles_list = grouped.get(topic_name, [])
        if len(articles_list) > max_articles:
            logger.info(
                "Capping %s from %d to %d articles (allocated %dm)",
                topic_name, len(articles_list), max_articles, mins,
            )
            grouped[topic_name] = articles_list[:max_articles]
    total_after = sum(len(v) for v in grouped.values())
    if total_after < total_before:
        logger.info("Topic capping: %d -> %d articles", total_before, total_after)

    # Single API call for all segment summaries + RSS summary
    ai_result = _summarize_all_segments(
        grouped, segment_word_budgets, format_segment_order, podcast_name=podcast_name,
    )
    if ai_result:
        ai_segments, rss_summary = ai_result
    else:
        ai_segments = {}
        rss_summary = f"Your daily knowledge briefing from {podcast_name}."

    # Fetch weather for intro
    weather = _fetch_seattle_weather()

    # Choose intro/outro based on show duration
    is_short = show_format and show_format.intro_minutes < 1
    if is_short:
        intro = INTRO_SECTION_SHORT.format(podcast_name=podcast_name, date=date_str, weather=weather)
        outro = OUTRO_SECTION_SHORT.format(podcast_name=podcast_name)
    else:
        intro = INTRO_SECTION.format(podcast_name=podcast_name, date=date_str, weather=weather,
                                     intro_dur=int(show_format.intro_minutes) if show_format else 1)
        outro = OUTRO_SECTION.format(podcast_name=podcast_name,
                                     outro_dur=int(show_format.outro_minutes) if show_format else 1)
    preamble = PODCAST_PREAMBLE.format(podcast_name=podcast_name, date=date_str)

    sections = [f"# {podcast_name} — Daily Briefing — {date_str}\n"]
    sections.append(preamble)
    sections.append(intro)

    segment_counts: dict[str, int] = {}
    segment_sources: dict[str, list[str]] = {}
    segment_number = 0

    for topic_name in format_segment_order:
        articles = grouped.get(topic_name, [])
        if not articles:
            continue

        segment_number += 1
        segment_counts[topic_name] = len(articles)
        sources = list(dict.fromkeys(a.source for a in articles))
        segment_sources[topic_name] = sources

        mins = format_durations.get(topic_name, 1)
        word_budget = segment_word_budgets[topic_name]
        duration_label = f" (~{mins} {'minute' if mins == 1 else 'minutes'})"

        section_header = f"## SEGMENT {segment_number}: {topic_name}{duration_label}"
        section_header += f"\n**Word budget: ~{word_budget} words**"
        sections.append(section_header)

        # Use pre-generated AI summary, fall back to raw text
        summary = ai_segments.get(topic_name)
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

    return text, segment_counts, segment_sources, rss_summary


def compile(digest: DailyDigest, show: ShowConfig | None = None) -> CompiledDigest:
    """Compile all articles into a single well-structured text document.

    Args:
        digest: The daily digest containing articles to compile.
        show: Show-specific config for podcast name. Falls back to "The Hootline".
    """
    if not digest.articles:
        raise DigestCompileError("No articles to compile.")

    podcast_name = show.podcast_title if show else "The Hootline"

    PST = timezone(timedelta(hours=-8))
    episode_date = datetime.now(PST).date()
    date_ymd = episode_date.strftime("%Y-%m-%d")
    date_display = episode_date.strftime("%B %-d, %Y")

    show_format = show.format if show else None

    try:
        text, segment_counts, segment_sources, rss_summary = _compile_text(
            digest, date_display, podcast_name=podcast_name, show_format=show_format,
        )
        topics_summary = _build_topics_summary(digest, segment_counts, show_format=show_format)
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
