"""Resource Radar recommendations for videos, books, and GitHub repos.

The engine only creates transparent discovery links from the user's own
subjects, themes, weak areas, and recent files. It does not invent
rankings, video titles, authors, or availability -- video titles only ever
come from a real YouTube Data API search result (see youtube_api.py) when
one is configured; without a key, a video "candidate" is a plain search
link, never a guessed title. GitHub repo candidates work the same way,
except no key is required at all -- GitHub's Search API allows anonymous
use (see github_api.py), so repo recommendations work out of the box.
"""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from . import github_api, youtube_api
from . import makerere_curricula

_MAX_GITHUB_SEARCHES_PER_SYNC = 5


@dataclass(frozen=True)
class ResourceCandidate:
    subject_code: str
    theme: str
    source_type: str
    title: str
    query: str
    url: str
    reason: str
    score: int


def _clean_text(value, fallback="learning topic"):
    text = " ".join(str(value or "").replace("_", " ").split())
    return text[:140] or fallback


def _youtube_url(query):
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def _book_url(query):
    return f"https://openlibrary.org/search?q={quote_plus(query)}"


def _github_search_url(query):
    return f"https://github.com/search?q={quote_plus(query)}&type=repositories"


def _subject_topics(profile, subject_code=None):
    from organizer.models import MoveEvent, SubjectMemory, SubjectTheme

    memories = SubjectMemory.objects.filter(profile=profile)
    if subject_code:
        memories = memories.filter(code=subject_code)

    topics = []
    for memory in memories.order_by("code"):
        seen = set()

        for weak in memory.weak_areas or []:
            name = _clean_text(weak)
            key = name.lower()
            if key not in seen:
                topics.append((memory.code, name, 90, "weak area"))
                seen.add(key)

        for focus in memory.current_focus or []:
            name = _clean_text(focus)
            key = name.lower()
            if key not in seen:
                topics.append((memory.code, name, 72, "current focus"))
                seen.add(key)

        themes = SubjectTheme.objects.filter(
            profile=profile,
            subject_code=memory.code,
        ).order_by("-weight", "name")[:8]
        for theme in themes:
            name = _clean_text(theme.name)
            key = name.lower()
            if key not in seen:
                topics.append((memory.code, name, 50 + min(theme.weight, 40), "subject theme"))
                seen.add(key)

        if not seen:
            recent_files = MoveEvent.objects.filter(
                profile=profile,
                course_code=memory.code,
                success=True,
            ).order_by("-timestamp")[:3]
            for event in recent_files:
                name = _clean_text(Path(event.filename).stem, memory.code)
                key = name.lower()
                if key not in seen:
                    topics.append((memory.code, name, 40, "recent file"))
                    seen.add(key)

        if not seen:
            topics.append((memory.code, _clean_text(memory.title or memory.code), 32, "subject"))

    return topics


def _youtube_candidates(subject, topic, video_query, reason_source, score):
    """One real, verified video per YouTube result when the API is
    configured; a single plain search-results link (never a guessed
    title) when it isn't, exactly what this returned before YouTube was
    wired in."""
    videos = youtube_api.search_videos(video_query, max_results=2)
    if not videos:
        return [
            ResourceCandidate(
                subject_code=subject,
                theme=topic,
                source_type="youtube",
                title=f"Find strong video lessons for {subject}: {topic}",
                query=video_query,
                url=_youtube_url(video_query),
                reason=f"Based on {reason_source}: {topic}",
                score=score + 8,
            )
        ]

    return [
        ResourceCandidate(
            subject_code=subject,
            theme=topic,
            source_type="youtube",
            title=video["title"],
            # video_id makes each real result its own row instead of
            # collapsing onto one another in sync_recommendations'
            # update_or_create, which keys on (subject, source_type, query).
            query=f"{video_query} #{video['video_id']}",
            url=video["url"],
            reason=f"{video['channel']} · based on {reason_source}: {topic}" if video["channel"] else f"Based on {reason_source}: {topic}",
            score=score + 8,
        )
        for video in videos
    ]


def _github_candidates(subject, topic, code, reason_source, score, token):
    """One real repo per GitHub search result when the search succeeds; a
    single plain github.com search link (never a guessed repo) when it
    doesn't -- same shape as _youtube_candidates."""
    course_name = makerere_curricula.name_for_code(code) or subject
    # The "subject" fallback topic (no theme/weak-area yet) is just the
    # course's own label, e.g. "CSC2100 - Data Structures and Algorithms" --
    # a course code prefix isn't real search text (confirmed against the
    # live API during manual testing: it returns zero results), so that
    # case searches on the resolved course name alone rather than the raw
    # topic. Other topics (weak areas, themes, recent files) are specific
    # enough to combine with the course name for a more targeted search.
    if reason_source == "subject":
        repo_query = course_name
    else:
        repo_query = f"{course_name} {topic}"
    repos = github_api.search_repos(repo_query, token=token, max_results=2)

    if not repos:
        return [
            ResourceCandidate(
                subject_code=code,
                theme=topic,
                source_type="github_repo",
                title=f"Find GitHub repos for {subject}: {topic}",
                query=repo_query,
                url=_github_search_url(repo_query),
                reason=f"Based on {reason_source}: {topic}",
                score=score + 4,
            )
        ]

    return [
        ResourceCandidate(
            subject_code=code,
            theme=topic,
            source_type="github_repo",
            title=repo["full_name"],
            # Suffixing the repo name onto the query makes each real result
            # its own row instead of collapsing together in
            # sync_recommendations' update_or_create, which keys on
            # (subject, source_type, query).
            query=f"{repo_query} #{repo['full_name']}",
            url=repo["url"],
            reason=(
                f"{repo['stars']} stars · based on {reason_source}: {topic}"
                if repo["stars"]
                else f"Based on {reason_source}: {topic}"
            ),
            score=score + 4,
        )
        for repo in repos
    ]


def build_candidates(profile, subject_code=None, limit=16):
    candidates = []
    github_token = github_api.get_any_token(profile)
    github_searches_used = 0

    for code, theme, score, reason_source in _subject_topics(profile, subject_code=subject_code):
        subject = _clean_text(code, "General")
        topic = _clean_text(theme)

        video_query = f"{subject} {topic} lecture tutorial"
        book_query = f"{subject} {topic} textbook study guide"

        candidates.extend(_youtube_candidates(subject, topic, video_query, reason_source, score))
        candidates.append(
            ResourceCandidate(
                subject_code=code,
                theme=topic,
                source_type="book",
                title=f"Find books and study guides for {subject}: {topic}",
                query=book_query,
                url=_book_url(book_query),
                reason=f"Based on {reason_source}: {topic}",
                score=score,
            )
        )

        if github_searches_used < _MAX_GITHUB_SEARCHES_PER_SYNC:
            candidates.extend(_github_candidates(subject, topic, code, reason_source, score, github_token))
            github_searches_used += 1

    candidates.sort(key=lambda item: (-item.score, item.subject_code, item.source_type, item.query))
    return candidates[:limit]


def sync_recommendations(profile, subject_code=None, limit=16):
    from organizer.models import ResourceRecommendation

    results = []
    created_count = 0
    for candidate in build_candidates(profile, subject_code=subject_code, limit=limit):
        item, created = ResourceRecommendation.objects.update_or_create(
            profile=profile,
            subject_code=candidate.subject_code,
            source_type=candidate.source_type,
            query=candidate.query,
            defaults={
                "theme": candidate.theme,
                "title": candidate.title,
                "url": candidate.url,
                "reason": candidate.reason,
                "score": candidate.score,
            },
        )
        created_count += int(created)
        results.append(item)
    if created_count:
        from . import notifications

        notifications.notify_new_items(
            "New study recommendations",
            "study recommendation",
            created_count,
            profile=profile,
        )
    return results


def recommendations_for_profile(profile, subject_code=None, include_dismissed=False, limit=30):
    from organizer.models import ResourceRecommendation

    items = ResourceRecommendation.objects.filter(profile=profile)
    if subject_code:
        items = items.filter(subject_code=subject_code)
    if not include_dismissed:
        items = items.exclude(status="dismissed")
    return items.order_by("status", "-score", "-updated_at")[:limit]


def set_recommendation_status(item, status):
    allowed = {"suggested", "saved", "dismissed", "opened"}
    if status not in allowed:
        raise ValueError(f"Unknown recommendation status: {status}")
    item.status = status
    item.save(update_fields=["status", "updated_at"])
    return item
