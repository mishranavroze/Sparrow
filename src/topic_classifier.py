"""Classify newsletter articles into podcast topic segments."""

import logging
import re
from enum import StrEnum

from src.models import Article

logger = logging.getLogger(__name__)


class Topic(StrEnum):
    """Podcast segment topics in presentation order."""

    WORLD_POLITICS = "World Politics"
    US_POLITICS = "US Politics"
    INDIAN_POLITICS = "Indian Politics"
    TECH_AI = "Latest in Tech"
    ENTERTAINMENT = "Entertainment"
    PRODUCT_MANAGEMENT = "Product Management"
    CROSSFIT = "CrossFit"
    F1 = "Formula 1"
    ARSENAL = "Arsenal"
    INDIAN_CRICKET = "Indian Cricket"
    BADMINTON = "Badminton"
    OTHER = "Other Newsletter Insights"


# Ordered list for segment rendering (priority order)
SEGMENT_ORDER: list[Topic] = [
    Topic.TECH_AI,
    Topic.PRODUCT_MANAGEMENT,
    Topic.WORLD_POLITICS,
    Topic.US_POLITICS,
    Topic.INDIAN_POLITICS,
    Topic.CROSSFIT,
    Topic.ENTERTAINMENT,
    Topic.F1,
    Topic.ARSENAL,
    Topic.INDIAN_CRICKET,
    Topic.BADMINTON,
    Topic.OTHER,
]

# Suggested duration labels per segment (total: 30 minutes)
SEGMENT_DURATIONS: dict[Topic, str] = {
    Topic.TECH_AI: "~5 minutes",
    Topic.PRODUCT_MANAGEMENT: "~4 minutes",
    Topic.WORLD_POLITICS: "~4 minutes",
    Topic.US_POLITICS: "~3 minutes",
    Topic.INDIAN_POLITICS: "~3 minutes",
    Topic.ENTERTAINMENT: "~3 minutes",
    Topic.CROSSFIT: "~2 minutes",
    Topic.F1: "~2 minutes",
    Topic.ARSENAL: "~1 minute",
    Topic.INDIAN_CRICKET: "~1 minute",
    Topic.BADMINTON: "~1 minute",
    Topic.OTHER: "~1 minute",
}

# Transactional senders to filter out entirely
FILTERED_SENDERS: set[str] = {
    "google",
    "google one",
    "google play",
    "google gemini",
    "gmail team",
    "notebooklm",
    "noreply",
    "no-reply",
    "substack",
}

# Single-topic newsletter sources — map sender name (lowercased) to topic
SOURCE_TOPIC_MAP: dict[str, Topic] = {
    "the neuron": Topic.TECH_AI,
    "cassidoo": Topic.TECH_AI,
    "tldr": Topic.TECH_AI,
    "ben's bites": Topic.TECH_AI,
    "the verge": Topic.TECH_AI,
    "nyt wirecutter": Topic.PRODUCT_MANAGEMENT,
    "wirecutter": Topic.PRODUCT_MANAGEMENT,
    "lenny's newsletter": Topic.PRODUCT_MANAGEMENT,
    "the product compass": Topic.PRODUCT_MANAGEMENT,
    "department of product": Topic.PRODUCT_MANAGEMENT,
    "aakash gupta": Topic.PRODUCT_MANAGEMENT,
    "product growth": Topic.PRODUCT_MANAGEMENT,
    "the athletic pulse": Topic.ENTERTAINMENT,
    "the athletic": Topic.ENTERTAINMENT,
    "the hollywood reporter": Topic.ENTERTAINMENT,
    "polygon": Topic.ENTERTAINMENT,
    "kirkus reviews": Topic.ENTERTAINMENT,
    "morning chalk up": Topic.CROSSFIT,
    "wodwell": Topic.CROSSFIT,
    "the hindu": Topic.INDIAN_POLITICS,
    "the indian express": Topic.INDIAN_POLITICS,
    "mint": Topic.INDIAN_POLITICS,
    "the chai brief": Topic.INDIAN_POLITICS,
    "chai brief": Topic.INDIAN_POLITICS,
    "peter steinberger": Topic.TECH_AI,
    "interesting facts": Topic.OTHER,
    "better report": Topic.OTHER,
}

# Keyword lists for multi-topic sources (NYT, 1440, Apple News, etc.)
TOPIC_KEYWORDS: dict[Topic, list[str]] = {
    Topic.WORLD_POLITICS: [
        r"\bUN\b", r"\bNATO\b", r"\bEU\b", r"\bG7\b", r"\bG20\b",
        r"\bglobal\b", r"\binternational\b", r"\bdiplomat", r"\btreaty\b",
        r"\bwar\b", r"\bconflict\b", r"\brefugee", r"\bsanction",
        r"\bUkraine\b", r"\bRussia\b", r"\bChina\b", r"\bMiddle East\b",
        r"\bIsrael\b", r"\bPalestine\b", r"\bGaza\b", r"\bIran\b",
        r"\bNorth Korea\b", r"\bforeign policy\b", r"\bgeopolit",
        r"\bclimate summit\b", r"\bpeace\s+talk", r"\bceasefire\b",
        r"\bworld leader", r"\bambassador\b", r"\bterroris",
    ],
    Topic.US_POLITICS: [
        r"\bCongress\b", r"\bSenate\b", r"\bHouse\b", r"\bWhite House\b",
        r"\bPresident\b", r"\bSupreme Court\b", r"\bRepublican",
        r"\bDemocrat", r"\bGOP\b", r"\bbipartisan\b", r"\belection\b",
        r"\bvoter", r"\bcampaign\b", r"\bimpeach", r"\bfilibuster\b",
        r"\blegislat", r"\bfederal\b", r"\bDOJ\b", r"\bFBI\b",
        r"\bWashington\b", r"\bCapitol\b", r"\bTrump\b", r"\bBiden\b",
        r"\bpoll\b", r"\bprimary\b", r"\bpartisan\b",
    ],
    Topic.INDIAN_POLITICS: [
        r"\bIndia\b", r"\bModi\b", r"\bBJP\b", r"\bDelhi\b",
        r"\bMumbai\b", r"\bLok Sabha\b", r"\bRajya Sabha\b",
        r"\bParliament\b.*\bIndia", r"\bRupee\b", r"\bRBI\b",
        r"\bBollywood\b",
    ],
    Topic.TECH_AI: [
        r"\bAI\b", r"\bartificial intelligence\b", r"\bGPT\b",
        r"\bOpenAI\b", r"\bGoogle\b.*\bAI\b", r"\bApple\b.*\bchip",
        r"\bstartup\b", r"\btech\b", r"\bsoftware\b", r"\bcybersecur",
        r"\bblockchain\b", r"\bcrypto\b", r"\bapp\b.*\blaunch",
        r"\bsilicon valley\b", r"\bcloud\b", r"\bdata\b.*\bprivacy",
        r"\bmachine learning\b", r"\bLLM\b", r"\bChatGPT\b",
        r"\bAnthrop", r"\bneural\b", r"\brobot",
    ],
    Topic.ENTERTAINMENT: [
        r"\bmovie\b", r"\bfilm\b", r"\bNetflix\b", r"\bDisney\b",
        r"\bHBO\b", r"\bstreaming\b", r"\bbox office\b", r"\btrailer\b",
        r"\bseries\b", r"\bseason\b", r"\bepisode\b", r"\bshow\b",
        r"\bbook\b", r"\bnovel\b", r"\bauthor\b", r"\bbestseller\b",
        r"\bmusic\b", r"\balbum\b", r"\bconcert\b", r"\baward",
        r"\bOscar", r"\bEmmy\b", r"\bGrammy\b", r"\bcelebrit",
        r"\bTV\b", r"\bgame\b.*\breleas", r"\bvideo game",
    ],
    Topic.PRODUCT_MANAGEMENT: [
        r"\bproduct manag", r"\bPM\b", r"\broadmap\b",
        r"\buser research\b", r"\bA/B test", r"\bmetric",
        r"\bOKR\b", r"\bKPI\b", r"\bbacklog\b", r"\bsprint\b",
        r"\bfeature\b.*\bpriori", r"\bstakeholder\b",
        r"\bproduct-market fit\b", r"\buser experience\b",
    ],
    Topic.CROSSFIT: [
        r"\bCrossFit\b", r"\bWOD\b", r"\bsnatch\b", r"\bclean and jerk\b",
        r"\bdeadlift\b", r"\bkipping\b", r"\bAMRAP\b", r"\bEMOM\b",
        r"\bfunctional fitness\b", r"\bCrossFit Games\b",
        r"\bRogue\b", r"\bbox\b.*\bgym\b",
    ],
    Topic.F1: [
        r"\bFormula 1\b", r"\bFormula One\b", r"\bF1\b", r"\bGrand Prix\b",
        r"\bpole position\b", r"\bpit stop\b", r"\bFIA\b",
        r"\bVerstappen\b", r"\bHamilton\b", r"\bLeclerc\b", r"\bNorris\b",
        r"\bRed Bull Racing\b", r"\bFerrari\b.*\bF1", r"\bMcLaren\b",
        r"\bMercedes\b.*\bF1", r"\bqualifying\b", r"\bpodium\b",
        r"\bDRS\b", r"\btyre\b.*\bstrategy", r"\bcircuit\b",
    ],
    Topic.ARSENAL: [
        r"\bArsenal\b", r"\bGunners\b", r"\bEmirates Stadium\b",
        r"\bArteta\b", r"\bPremier League\b.*\bArsenal",
        r"\bArsenal\b.*\bPremier League",
        r"\bSaka\b", r"\bSaliba\b", r"\bOdegaard\b", r"\bRice\b",
        r"\bHavertz\b", r"\bRamsdale\b", r"\bRaya\b",
        r"\bNorth London\b", r"\bArsenal\b.*\btransfer",
    ],
    Topic.INDIAN_CRICKET: [
        r"\bBCCI\b", r"\bIPL\b", r"\bIndian Premier League\b",
        r"\bTeam India\b", r"\bIndia\b.*\bcricket",
        r"\bcricket\b.*\bIndia", r"\bVirat\b", r"\bKohli\b",
        r"\bRohit\b", r"\bSharma\b.*\bcricket", r"\bBumrah\b",
        r"\bDhoni\b", r"\bTest match\b.*\bIndia",
        r"\bODI\b.*\bIndia", r"\bT20\b.*\bIndia",
        r"\bWorld Cup\b.*\bcricket", r"\bcricket\b.*\bWorld Cup",
        r"\bwicket\b", r"\bbatting\b.*\bIndia", r"\bbowling\b.*\bIndia",
    ],
    Topic.BADMINTON: [
        r"\bbadminton\b", r"\bBWF\b", r"\bshuttlecock\b",
        r"\bAll England\b.*\bbadminton", r"\bThomas Cup\b",
        r"\bUber Cup\b", r"\bSudirman Cup\b",
        r"\bPV Sindhu\b", r"\bSindhu\b", r"\bSrikanth\b",
        r"\bLakshya\b", r"\bSen\b.*\bbadminton",
        r"\bSatwiksairaj\b", r"\bChirag\b", r"\bShetty\b",
        r"\bSuper 750\b", r"\bSuper 1000\b", r"\bsmash\b.*\bbadminton",
    ],
}


def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, strip, and replace curly quotes."""
    return text.lower().strip().replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')


def _is_filtered_sender(sender: str) -> bool:
    """Check if an email sender should be filtered out."""
    sender_norm = _normalize(sender)
    return sender_norm in FILTERED_SENDERS


def _score_keywords(text: str, topic: Topic) -> int:
    """Count keyword matches for a topic in the given text."""
    keywords = TOPIC_KEYWORDS.get(topic, [])
    score = 0
    for pattern in keywords:
        if re.search(pattern, text, re.IGNORECASE):
            score += 1
    return score


def classify_article(article: Article) -> Topic | None:
    """Classify an article into a podcast topic segment.

    Args:
        article: The article to classify.

    Returns:
        A Topic for the article, or None if the sender should be filtered.
    """
    # Filter transactional senders
    if _is_filtered_sender(article.source):
        logger.info("Filtering transactional sender: '%s'", article.source)
        return None

    # Check source-based mapping first
    source_lower = _normalize(article.source)
    for source_key, topic in SOURCE_TOPIC_MAP.items():
        if source_key in source_lower or source_lower in source_key:
            logger.debug("Source map: '%s' -> %s", article.source, topic)
            return topic

    # Keyword-based classification for multi-topic sources
    text = f"{article.title} {article.content}"
    best_topic = Topic.OTHER
    best_score = 0

    for topic in Topic:
        if topic == Topic.OTHER:
            continue
        score = _score_keywords(text, topic)
        if score > best_score:
            best_score = score
            best_topic = topic

    # Require at least 2 keyword matches to assign a specific topic
    if best_score < 2:
        best_topic = Topic.OTHER

    logger.debug(
        "Keyword classify: '%s' -> %s (score=%d)", article.title, best_topic, best_score
    )
    return best_topic
