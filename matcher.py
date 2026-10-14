#!/usr/bin/env python3

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from mutagen import File
from rapidfuzz.fuzz import ratio
from ytmusicapi import YTMusic


BASE = Path(__file__).resolve().parent
YTDLP = BASE / "venv/bin/yt-dlp"


# =========================================================
# TEXT CLEANING
# =========================================================

def normalize(text):
    if not text:
        return ""

    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    # Words that don't define the actual song
    text = re.sub(
        r"\b("
        r"official|official video|official audio|"
        r"music video|audio|video|lyrics?|clip"
        r")\b",
        " ",
        text,
    )

    text = re.sub(r"[^a-z0-9]+", " ", text)

    return " ".join(text.split())


def clean_local_artist(artist):
    artist = artist or ""

    # Remove decorative Unicode text commonly added to YouTube channel names.
    # Keep normal ASCII artist name when possible.
    ascii_part = "".join(
        c for c in artist
        if c.isascii()
    ).strip()

    if ascii_part:
        artist = ascii_part

    # Remove common channel decorations.
    artist = re.sub(
        r"\b(official|original|music|channel|records?)\b",
        " ",
        artist,
        flags=re.I,
    )

    artist = re.sub(r"\s+", " ", artist)

    return artist.strip(" -–—_|")


def clean_local_title(title, artist):
    title = title or ""
    artist = artist or ""

    # Remove download garbage.
    title = re.sub(
        r"\(?\s*mp3[_ -]?\d+k?\s*\)?",
        "",
        title,
        flags=re.I,
    )

    # Remove common YouTube labels, with or without brackets.
    title = re.sub(
        r"[\[\(]?\s*"
        r"(official\s+music\s+video|"
        r"official\s+video|"
        r"official\s+audio|"
        r"official\s+visual(?:\s+art)?(?:\s+video)?|"
        r"clip\s+officiel|"
        r"clip\s+officielle|"
        r"music\s+video|"
        r"lyrics?\s+video|"
        r"lyrics?|"
        r"audio)"
        r"\s*[\]\)]?",
        " ",
        title,
        flags=re.I,
    )

    # Remove hashtags often appended by uploaders.
    title = re.sub(
        r"\s+#[A-Za-z0-9_]+",
        " ",
        title,
    )

    # Artist can occur at the beginning of the title.
    # Try both original and cleaned artist.
    artist_variants = [
        artist,
        clean_local_artist(artist),
    ]

    # Also use the first ASCII-looking token as a fallback.
    m = re.match(
        r"\s*([A-Za-z0-9][A-Za-z0-9._'-]*)",
        artist,
    )

    if m:
        artist_variants.append(m.group(1))

    for a in sorted(
        set(x for x in artist_variants if x),
        key=len,
        reverse=True,
    ):
        title = re.sub(
            rf"^\s*{re.escape(a)}\s*[-–—]\s*",
            "",
            title,
            flags=re.I,
        )

    # Remove trailing producer credit from the SEARCH title only.
    # Supports:
    #   (Prod Adam K)
    #   (Prod. 808Godz)
    #   (Prod by ILL DANE)
    #   Prod by Chefi Beat
    #   Produced by ...
    # This does NOT modify the original audio file.
    title = re.sub(
        r"\s*[\[(]?\s*"
        r"(?:prod(?:uced)?\.?)"
        r"(?:\s+by)?\s+"
        r"[^\]\)]+"
        r"[\]\)]?\s*$",
        "",
        title,
        flags=re.I,
    )

    title = re.sub(r"\s+", " ", title)

    return title.strip(" -–—_[]()#")


# =========================================================
# BAD YOUTUBE RESULTS
# =========================================================

BAD_PATTERNS = [
    r"\breaction\b",
    r"\breacts?\b",
    r"\breview\b",
    r"\btutorial\b",
    r"\bkaraoke\b",
    r"\bnightcore\b",
    r"\bsped\s*up\b",
    r"\bslowed\b",
    r"\bslowed\s*(?:and|&)\s*reverb\b",
]


def is_bad_result(title):
    text = normalize(title)

    for pattern in BAD_PATTERNS:
        if re.search(pattern, text, flags=re.I):
            return True

    return False


# =========================================================
# LOCAL METADATA
# =========================================================

def get_tag(audio, key):
    if not audio or not audio.tags:
        return ""

    value = audio.tags.get(key)

    if not value:
        return ""

    if isinstance(value, list):
        return str(value[0])

    return str(value)


def read_local_file(path):
    easy = File(path, easy=True)
    full = File(path)

    if not easy or not full:
        raise RuntimeError("Could not read audio file")

    artist = get_tag(easy, "artist")
    title = get_tag(easy, "title")

    artist = clean_local_artist(artist)

    # Recover artist/title from an explicit:
    # "Artist - Title"
    #
    # YouTube metadata often stores the uploader/channel in the
    # artist tag (e.g. "AnasOfficielVEVO") while the real artist
    # is clearly present at the beginning of the title.
    source = title or Path(path).stem

    parts = re.split(
        r"\s+[-–—]\s+",
        source,
        maxsplit=1,
    )

    if len(parts) == 2:
        possible_artist = clean_local_artist(parts[0])
        possible_title = parts[1].strip()

        if possible_artist and possible_title:
            use_title_artist = False

            if not artist:
                use_title_artist = True
            else:
                current_norm = normalize(artist)
                possible_norm = normalize(possible_artist)

                # Same artist, but the tag may contain uploader/channel
                # decorations such as "AnasOfficielVEVO".
                if (
                    current_norm
                    and possible_norm
                    and (
                        current_norm == possible_norm
                        or current_norm.startswith(possible_norm)
                        or possible_norm.startswith(current_norm)
                    )
                ):
                    use_title_artist = True

            if use_title_artist:
                artist = possible_artist
                title = possible_title

    if not title:
        title = Path(path).stem

    title = clean_local_title(
        title,
        artist,
    )

    try:
        duration = float(full.info.length)
    except Exception:
        duration = None

    return artist, title, duration



# =========================================================
# V7 MULTI-SOURCE LOCAL IDENTITIES
# =========================================================

def split_artist_title(text):
    """
    Parse a strong explicit 'Artist - Title' string.
    Returns (artist, title) or None.
    """
    if not text:
        return None

    text = re.sub(
        r"\(?\s*mp3[_ -]?\d+k?\s*\)?",
        "",
        str(text),
        flags=re.I,
    ).strip()

    parts = re.split(
        r"\s+[-–—]\s+",
        text,
        maxsplit=1,
    )

    if len(parts) != 2:
        return None

    artist = clean_local_artist(parts[0])
    title = clean_local_title(parts[1], artist)

    if not artist or not title:
        return None

    return artist, title


def get_local_identities(path):
    """
    Build multiple possible Artist/Title identities.

    Nothing is written to the audio file.
    """
    path = Path(path)

    easy = File(path, easy=True)
    full = File(path)

    if not easy or not full:
        raise RuntimeError("Could not read audio file")

    tag_artist_raw = get_tag(easy, "artist")
    tag_title_raw = get_tag(easy, "title")

    tag_artist = clean_local_artist(tag_artist_raw)

    identities = []

    def add(artist, title, source):
        artist = clean_local_artist(artist)
        title = clean_local_title(title, artist)

        if not title:
            return

        key = (
            normalize(artist),
            normalize(title),
        )

        for item in identities:
            if item["key"] == key:
                return

        identities.append({
            "artist": artist,
            "title": title,
            "identity_source": source,
            "key": key,
        })

    # 1. Current V6 parser.
    try:
        a, t, _ = read_local_file(path)
        add(a, t, "current-parser")
    except Exception:
        pass

    # 2. Raw tags.
    if tag_title_raw:
        add(
            tag_artist,
            tag_title_raw,
            "tags",
        )

    # 3. Explicit Artist - Title inside TITLE tag.
    parsed = split_artist_title(tag_title_raw)

    if parsed:
        add(
            parsed[0],
            parsed[1],
            "title-artist",
        )

    # 4. Explicit Artist - Title in filename.
    parsed = split_artist_title(path.stem)

    if parsed:
        add(
            parsed[0],
            parsed[1],
            "filename",
        )

    # 5. Filename as title with tag artist.
    #
    # Useful when filename contains a cleaner title but
    # doesn't contain an explicit Artist - Title pair.
    add(
        tag_artist,
        path.stem,
        "filename+tag-artist",
    )

    try:
        duration = float(full.info.length)
    except Exception:
        duration = None

    for item in identities:
        item.pop("key", None)

    return identities, duration



# =========================================================
# DURATION
# =========================================================

def duration_score(local_duration, remote_duration):

    if not local_duration or not remote_duration:
        return None

    diff = abs(
        local_duration - remote_duration
    )

    if diff <= 2:
        return 100

    if diff <= 5:
        return 97

    if diff <= 10:
        return 90

    if diff <= 15:
        return 75

    if diff <= 25:
        return 50

    if diff <= 40:
        return 20

    return 0


# =========================================================
# YOUTUBE MUSIC
# =========================================================

def search_ytmusic(query):

    yt = YTMusic()

    results = []

    try:

        found = yt.search(
            query,
            filter="songs",
            limit=20,
        )

    except Exception as e:

        print(
            f"[YT Music error] {e}"
        )

        return results

    for item in found:

        artists = ", ".join(
            a.get("name", "")
            for a in item.get(
                "artists",
                [],
            )
        )

        duration = item.get(
            "duration_seconds"
        )

        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None

        results.append({
            "source": "YT Music",
            "artist": artists,
            "title": item.get(
                "title",
                "",
            ),
            "album": (
                item.get("album") or {}
            ).get(
                "name",
                "",
            ),
            "duration": duration,
            "id": item.get(
                "videoId",
                "",
            ),
        })

    return results


# =========================================================
# NORMAL YOUTUBE
# =========================================================

def youtube_search_once(query):

    # Ask YouTube to avoid obvious junk
    search_query = (
        f"{query} "
        "-reaction "
        "-react "
        "-review "
        "-tutorial "
        "-karaoke "
        "-nightcore "
        "-slowed "
        "-spedup"
    )

    cmd = [
        str(YTDLP),
        "--flat-playlist",
        "--dump-single-json",
        f"ytsearch20:{search_query}",
    ]

    try:

        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
        )

    except Exception as e:

        print(
            f"[YouTube error] {e}"
        )

        return []

    if process.returncode != 0:

        print(
            "[YouTube search failed]"
        )

        if process.stderr:
            print(
                process.stderr.strip()
            )

        return []

    try:
        data = json.loads(
            process.stdout
        )
    except Exception as e:

        print(
            f"[YouTube JSON error] {e}"
        )

        return []

    results = []

    for item in data.get(
        "entries",
        [],
    ):

        title = item.get(
            "title",
            "",
        )

        # HARD reject
        if is_bad_result(title):
            continue

        duration = item.get(
            "duration"
        )

        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None

        results.append({
            "source": "YouTube",
            "artist": (
                item.get("channel")
                or item.get("uploader")
                or ""
            ),
            "title": title,
            "album": "",
            "duration": duration,
            "id": item.get(
                "id",
                "",
            ),
        })

    return results


def search_youtube(
    artist,
    title,
):

    #
    # MULTI-QUERY SEARCH
    #
    # Don't trust only one YouTube search.
    #

    queries = []

    if artist and title:

        queries.append(
            f"{artist} {title}"
        )

        # Remove producer information for broader search
        simple_title = re.sub(
            r"\([^)]*(?:prod|produced)[^)]*\)",
            "",
            title,
            flags=re.I,
        ).strip()

        if simple_title != title:

            queries.append(
                f"{artist} {simple_title}"
            )

        #
        # If title contains Prod. X,
        # try artist + song + producer.
        #

        producer = re.search(
            r"(?:prod\.?|produced\s+by)\s*"
            r"([^)]+)",
            title,
            flags=re.I,
        )

        if producer:

            producer_name = (
                producer.group(1).strip()
            )

            queries.append(
                f"{artist} "
                f"{simple_title} "
                f"{producer_name}"
            )

        queries.append(
            f"{simple_title} {artist}"
        )

    else:

        queries.append(
            f"{artist} {title}".strip()
        )

    #
    # Remove duplicate queries
    #

    unique_queries = []

    seen_queries = set()

    for q in queries:

        key = normalize(q)

        if not key:
            continue

        if key in seen_queries:
            continue

        seen_queries.add(key)

        unique_queries.append(q)

    results = []

    seen_videos = set()

    for query in unique_queries:

        print(
            f"  YouTube query: {query}"
        )

        found = youtube_search_once(
            query
        )

        for item in found:

            video_id = item["id"]

            if not video_id:
                continue

            if video_id in seen_videos:
                continue

            seen_videos.add(
                video_id
            )

            results.append(
                item
            )

    return results


# =========================================================
# SCORING
# =========================================================

def score_candidate(
    local_artist,
    local_title,
    local_duration,
    candidate,
):

    remote_artist = candidate[
        "artist"
    ]

    remote_title = candidate[
        "title"
    ]

    remote_duration = candidate[
        "duration"
    ]

    source = candidate[
        "source"
    ]

    title_score = ratio(
        normalize(local_title),
        normalize(remote_title),
    )

    if (
        local_artist
        and remote_artist
    ):

        artist_score = ratio(
            normalize(local_artist),
            normalize(remote_artist),
        )

    else:

        artist_score = None

    dur_score = duration_score(
        local_duration,
        remote_duration,
    )

    #
    # YT Music:
    # artist field is reliable.
    #

    if source == "YT Music":

        if artist_score is None:

            score = (
                title_score * 0.80
            )

            if dur_score is not None:
                score += (
                    dur_score * 0.20
                )

        else:

            score = (
                artist_score * 0.40
                + title_score * 0.45
            )

            if dur_score is not None:
                score += (
                    dur_score * 0.15
                )
            else:
                score += 7.5

    #
    # Normal YouTube:
    # channel != necessarily artist.
    #

    else:

        score = (
            title_score * 0.70
        )

        if artist_score is not None:

            score += (
                artist_score * 0.15
            )

        if dur_score is not None:

            score += (
                dur_score * 0.15
            )

        #
        # Strong duration mismatch:
        # probably reaction / compilation /
        # wrong recording.
        #

        if (
            local_duration
            and remote_duration
        ):

            diff = abs(
                local_duration
                - remote_duration
            )

            if diff > 45:
                score -= 35

            elif diff > 25:
                score -= 15

    score = max(
        0,
        min(
            100,
            score,
        ),
    )

    return {
        "score": score,
        "title_score": title_score,
        "artist_score": artist_score,
        "duration_score": dur_score,
    }


# =========================================================
# MAIN
# =========================================================


def main():
    if len(sys.argv) != 2:

        print(
            "Usage: matcher.py <audio-file>"
        )

        sys.exit(1)


    path = Path(
        sys.argv[1]
    )


    if not path.exists():

        print(
            f"File not found: {path}"
        )

        sys.exit(1)


    try:

        artist, title, local_duration = (
            read_local_file(path)
        )

    except Exception as e:

        print(
            f"Error: {e}"
        )

        sys.exit(1)


    print()
    print("LOCAL")
    print("─────")

    print(
        "Artist   :",
        artist or "[unknown]",
    )

    print(
        "Title    :",
        title,
    )

    print(
        "Duration :",
        (
            f"{local_duration:.1f}s"
            if local_duration
            else "?"
        ),
    )


    query = (
        f"{artist} {title}"
    ).strip()


    print(
        "Query    :",
        query,
    )


    # =========================================================
    # SEARCH
    # =========================================================

    print()
    print(
        "Searching YouTube Music..."
    )

    ytm_results = search_ytmusic(
        query
    )


    print()
    print(
        "Searching normal YouTube..."
    )

    yt_results = search_youtube(
        artist,
        title,
    )


    print()
    print(
        f"YT Music candidates : "
        f"{len(ytm_results)}"
    )

    print(
        f"YouTube candidates  : "
        f"{len(yt_results)}"
    )


    # =========================================================
    # SCORE EVERYTHING
    # =========================================================

    candidates = []

    for candidate in (
        ytm_results
        + yt_results
    ):

        scores = score_candidate(
            artist,
            title,
            local_duration,
            candidate,
        )

        candidates.append({
            **candidate,
            **scores,
        })


    candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )


    # =========================================================
    # DISPLAY
    # =========================================================

    print()
    print("BEST MATCHES")
    print("────────────")


    for i, item in enumerate(
        candidates[:15],
        1,
    ):

        a_score = (
            f"{item['artist_score']:.0f}%"
            if item["artist_score"]
            is not None
            else "?"
        )

        d_score = (
            f"{item['duration_score']:.0f}%"
            if item["duration_score"]
            is not None
            else "?"
        )

        duration = (
            f"{item['duration']:.0f}s"
            if item["duration"]
            else "?"
        )

        print(
            f"{i:2}. "
            f"{item['score']:5.1f}%"
            f" | {item['source']}"
        )

        print(
            f"    Artist score   : "
            f"{a_score}"
        )

        print(
            f"    Title score    : "
            f"{item['title_score']:.0f}%"
        )

        print(
            f"    Duration score : "
            f"{d_score}"
            f" ({duration})"
        )

        print(
            f"    {item['artist']} "
            f"- {item['title']}"
        )

        if item["album"]:

            print(
                f"    Album: "
                f"{item['album']}"
            )

        print(
            f"    ID: "
            f"{item['id']}"
        )

        print()


    # =========================================================
    # FINAL DECISION
    # =========================================================

    print("FINAL")
    print("─────")


    if not candidates:

        print(
            "❌ UNMATCHED"
        )

        sys.exit(0)


    best = candidates[0]


    #
    # For HIGH confidence we require:
    #
    # - very good title
    # - good duration when available
    # - strong total score
    #

    duration_safe = (
        best["duration_score"] is None
        or best["duration_score"] >= 75
    )


    if (
        best["score"] >= 88
        and best["title_score"] >= 88
        and duration_safe
    ):

        status = "✅ HIGH CONFIDENCE"

    elif (
        best["score"] >= 70
        and best["title_score"] >= 65
    ):

        status = "⚠️ REVIEW"

    else:

        status = "❌ UNMATCHED"


    print(
        f"{status} "
        f"({best['score']:.1f}%)"
    )

    print(
        f"Source    : "
        f"{best['source']}"
    )

    print(
        f"Candidate : "
        f"{best['artist']} "
        f"- {best['title']}"
    )

    print(
        f"ID        : "
        f"{best['id']}"
    )


if __name__ == "__main__":
    main()
