#!/usr/bin/env python3

import csv
import os
import random
import sys
import time
from pathlib import Path

import matcher


INBOX = Path(os.getenv("MUSIC_INBOX", "./data/inbox"))

REPORT_DIR = (
    Path(__file__).resolve().parent / "reports"
)

AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".opus",
}


def classify(best):
    if not best:
        return "UNMATCHED"

    duration_safe = (
        best["duration_score"] is None
        or best["duration_score"] >= 75
    )

    if (
        best["score"] >= 88
        and best["title_score"] >= 88
        and duration_safe
    ):
        return "HIGH"

    if (
        best["score"] >= 70
        and best["title_score"] >= 65
    ):
        return "REVIEW"

    return "UNMATCHED"


def search_identity(
    artist,
    title,
    duration,
    identity_source,
):
    query = f"{artist} {title}".strip()

    print(
        f"       identity [{identity_source}] "
        f"{artist or '?'} - {title}"
    )

    ytm = matcher.search_ytmusic(query)

    yt = matcher.search_youtube(
        artist,
        title,
    )

    candidates = []

    for candidate in ytm + yt:
        scores = matcher.score_candidate(
            artist,
            title,
            duration,
            candidate,
        )

        candidates.append({
            **candidate,
            **scores,
            "identity_source": identity_source,
            "local_artist": artist,
            "local_title": title,
        })

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    return candidates


def scan_file(path):
    identities, duration = (
        matcher.get_local_identities(path)
    )

    all_candidates = []

    for identity in identities:
        candidates = search_identity(
            identity["artist"],
            identity["title"],
            duration,
            identity["identity_source"],
        )

        all_candidates.extend(candidates)

    all_candidates.sort(
        key=lambda x: x["score"],
        reverse=True,
    )

    best = (
        all_candidates[0]
        if all_candidates
        else None
    )

    return {
        "duration": duration,
        "identities": identities,
        "best": best,
        "status": classify(best),
    }


def main():
    limit = 50

    if len(sys.argv) >= 2:
        limit = int(sys.argv[1])

    files = [
        p
        for p in INBOX.rglob("*")
        if p.is_file()
        and p.suffix.lower()
        in AUDIO_EXTENSIONS
    ]

    random.shuffle(files)

    if limit > 0:
        files = files[:limit]

    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    stamp = time.strftime(
        "%Y%m%d-%H%M%S"
    )

    report = (
        REPORT_DIR
        / f"batch-v7-{stamp}.csv"
    )

    rows = []

    counts = {
        "HIGH": 0,
        "REVIEW": 0,
        "UNMATCHED": 0,
        "ERROR": 0,
    }

    print()
    print("V7 MULTI-SOURCE DRY RUN")
    print("───────────────────────")
    print("Tracks :", len(files))
    print("Writes : NONE")
    print()

    for index, path in enumerate(files, 1):
        print(
            f"\n[{index:03}/{len(files):03}] "
            f"{path.name}"
        )

        try:
            result = scan_file(path)

            best = result["best"]
            status = result["status"]

            counts[status] += 1

            if best:
                print()
                print(
                    f"     => {status} "
                    f"{best['score']:.1f}%"
                )

                print(
                    f"        identity : "
                    f"{best['identity_source']}"
                )

                print(
                    f"        local    : "
                    f"{best['local_artist']} - "
                    f"{best['local_title']}"
                )

                print(
                    f"        match    : "
                    f"{best['artist']} - "
                    f"{best['title']}"
                )

                print(
                    f"        source   : "
                    f"{best['source']}"
                )

            else:
                print("     => UNMATCHED")

            rows.append({
                "file": str(path),
                "status": status,
                "score": (
                    round(best["score"], 1)
                    if best else ""
                ),
                "identity_source": (
                    best["identity_source"]
                    if best else ""
                ),
                "local_artist": (
                    best["local_artist"]
                    if best else ""
                ),
                "local_title": (
                    best["local_title"]
                    if best else ""
                ),
                "local_duration": (
                    round(
                        result["duration"],
                        1,
                    )
                    if result["duration"]
                    else ""
                ),
                "title_score": (
                    round(
                        best["title_score"],
                        1,
                    )
                    if best else ""
                ),
                "artist_score": (
                    round(
                        best["artist_score"],
                        1,
                    )
                    if best
                    and best["artist_score"]
                    is not None
                    else ""
                ),
                "duration_score": (
                    round(
                        best["duration_score"],
                        1,
                    )
                    if best
                    and best["duration_score"]
                    is not None
                    else ""
                ),
                "match_source": (
                    best["source"]
                    if best else ""
                ),
                "match_artist": (
                    best["artist"]
                    if best else ""
                ),
                "match_title": (
                    best["title"]
                    if best else ""
                ),
                "album": (
                    best["album"]
                    if best else ""
                ),
                "id": (
                    best["id"]
                    if best else ""
                ),
            })

        except Exception as e:
            counts["ERROR"] += 1

            print(
                f"     => ERROR: "
                f"{type(e).__name__}: {e}"
            )

        time.sleep(0.5)

    fields = [
        "file",
        "status",
        "score",
        "identity_source",
        "local_artist",
        "local_title",
        "local_duration",
        "title_score",
        "artist_score",
        "duration_score",
        "match_source",
        "match_artist",
        "match_title",
        "album",
        "id",
    ]

    with report.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        writer.writeheader()
        writer.writerows(rows)

    total = len(files)

    print()
    print()
    print("SUMMARY")
    print("───────")

    for key in (
        "HIGH",
        "REVIEW",
        "UNMATCHED",
        "ERROR",
    ):
        value = counts[key]

        pct = (
            value / total * 100
            if total else 0
        )

        print(
            f"{key:<10}: "
            f"{value:4} "
            f"({pct:5.1f}%)"
        )

    print()
    print("Report:", report)
    print("✅ Originals untouched")


if __name__ == "__main__":
    main()
