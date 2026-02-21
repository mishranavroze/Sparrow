"""Tests for topic_classifier module."""

from src.models import Article
from src.topic_classifier import (
    Topic,
    _is_filtered_sender,
    _score_keywords,
    classify_article,
)


def _make_article(
    source: str = "Test Source",
    title: str = "Test Title",
    content: str = "Some content for testing purposes.",
) -> Article:
    return Article(
        source=source,
        title=title,
        content=content,
        estimated_words=len(content.split()),
    )


# --- Filtered senders ---


def test_filtered_sender_google():
    assert _is_filtered_sender("Google") is True


def test_filtered_sender_notebooklm():
    assert _is_filtered_sender("NotebookLM") is True


def test_filtered_sender_noreply():
    assert _is_filtered_sender("noreply") is True


def test_filtered_sender_google_one():
    assert _is_filtered_sender("Google One") is True


def test_non_filtered_sender():
    assert _is_filtered_sender("The New York Times") is False


def test_non_filtered_sender_neuron():
    assert _is_filtered_sender("The Neuron") is False


# --- Source-based mapping ---


def test_source_map_neuron():
    article = _make_article(source="The Neuron", title="AI Weekly Roundup")
    assert classify_article(article) == Topic.TECH_AI


def test_source_map_cassidoo():
    article = _make_article(source="cassidoo", title="Weekly Newsletter")
    assert classify_article(article) == Topic.TECH_AI


def test_wirecutter_not_product_management():
    """Wirecutter is consumer product reviews, NOT Product Management."""
    article = _make_article(source="NYT Wirecutter", title="Best Laptops")
    assert classify_article(article) != Topic.PRODUCT_MANAGEMENT


def test_source_map_athletic():
    article = _make_article(source="The Athletic Pulse", title="Game Recap")
    assert classify_article(article) == Topic.ENTERTAINMENT


def test_source_map_morning_chalk_up():
    article = _make_article(source="Morning Chalk Up", title="CrossFit Open")
    assert classify_article(article) == Topic.CROSSFIT


def test_source_map_interesting_facts():
    article = _make_article(source="Interesting Facts", title="Fun Trivia")
    assert classify_article(article) == Topic.OTHER


# --- Keyword classification ---


def test_keyword_scoring_us_politics():
    score = _score_keywords(
        "Congress passed a bipartisan bill in the Senate today", Topic.US_POLITICS
    )
    assert score >= 2


def test_keyword_scoring_tech_ai():
    score = _score_keywords(
        "OpenAI releases new GPT model with improved AI capabilities", Topic.TECH_AI
    )
    assert score >= 2


def test_keyword_scoring_world_politics():
    score = _score_keywords(
        "NATO summit discusses Ukraine conflict and international sanctions",
        Topic.WORLD_POLITICS,
    )
    assert score >= 2


def test_keyword_scoring_no_match():
    score = _score_keywords("The weather is nice today", Topic.CROSSFIT)
    assert score == 0


# --- Full classify_article ---


def test_classify_filters_transactional():
    article = _make_article(source="Google", title="Your storage is full")
    assert classify_article(article) is None


def test_classify_filters_google_play():
    article = _make_article(source="Google Play", title="New apps for you")
    assert classify_article(article) is None


def test_classify_keyword_us_politics():
    article = _make_article(
        source="The New York Times",
        title="Senate Passes Major Bill",
        content="Congress debated the legislation as Democrats and Republicans clashed in the Senate chamber.",
    )
    result = classify_article(article)
    assert result == Topic.US_POLITICS


def test_classify_keyword_world_politics():
    article = _make_article(
        source="1440 Daily Digest",
        title="Global Tensions Rise",
        content="NATO allies discussed the Ukraine conflict amid international sanctions against Russia.",
    )
    result = classify_article(article)
    assert result == Topic.WORLD_POLITICS


def test_classify_keyword_tech():
    article = _make_article(
        source="Apple News",
        title="New AI Breakthrough",
        content="OpenAI announced a new GPT model that pushes the boundaries of artificial intelligence.",
    )
    result = classify_article(article)
    assert result == Topic.TECH_AI


def test_classify_falls_back_to_other():
    article = _make_article(
        source="Random Newsletter",
        title="Interesting Tidbits",
        content="Here are some random facts about the world that you might find interesting.",
    )
    result = classify_article(article)
    assert result == Topic.OTHER


def test_classify_entertainment():
    article = _make_article(
        source="Apple News",
        title="Oscar Nominations Announced",
        content="The Oscar nominations for best film and best series were announced at the award ceremony.",
    )
    result = classify_article(article)
    assert result == Topic.ENTERTAINMENT


# --- New source mappings ---


def test_source_map_aakash_gupta():
    article = _make_article(source="Aakash Gupta", title="Weekly PM Insights")
    assert classify_article(article) == Topic.PRODUCT_MANAGEMENT


def test_source_map_product_growth():
    article = _make_article(source="Product Growth", title="Growth Tactics")
    assert classify_article(article) == Topic.PRODUCT_MANAGEMENT


# --- New sport topics: keyword classification ---


def test_keyword_scoring_f1():
    score = _score_keywords(
        "Verstappen takes pole position at the Grand Prix in a thrilling F1 qualifying session",
        Topic.F1,
    )
    assert score >= 2


def test_classify_f1():
    article = _make_article(
        source="1440 Daily Digest",
        title="F1 Grand Prix Results",
        content="Verstappen won the Grand Prix after a dramatic pit stop battle with Hamilton.",
    )
    assert classify_article(article) == Topic.F1


def test_keyword_scoring_arsenal():
    score = _score_keywords(
        "Arsenal defeated their rivals as Saka scored twice at Emirates Stadium",
        Topic.ARSENAL,
    )
    assert score >= 2


def test_classify_arsenal():
    article = _make_article(
        source="Apple News Sports",
        title="Arsenal Win Big",
        content="The Gunners secured victory at Emirates Stadium with goals from Saka and Odegaard.",
    )
    assert classify_article(article) == Topic.ARSENAL


def test_keyword_scoring_indian_cricket():
    score = _score_keywords(
        "India cricket team led by Kohli wins the IPL T20 match at BCCI event",
        Topic.INDIAN_CRICKET,
    )
    assert score >= 2


def test_classify_indian_cricket():
    article = _make_article(
        source="Apple News",
        title="IPL Season Update",
        content="Kohli and Bumrah led Team India to a stunning victory in the IPL T20 match.",
    )
    assert classify_article(article) == Topic.INDIAN_CRICKET


def test_keyword_scoring_badminton():
    score = _score_keywords(
        "PV Sindhu advances in BWF badminton tournament",
        Topic.BADMINTON,
    )
    assert score >= 2


def test_classify_badminton():
    article = _make_article(
        source="Apple News",
        title="Badminton World Tour",
        content="PV Sindhu and Lakshya Sen won their BWF Super 750 badminton matches.",
    )
    assert classify_article(article) == Topic.BADMINTON
