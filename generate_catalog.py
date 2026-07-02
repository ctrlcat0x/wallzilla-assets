#!/usr/bin/env python3
"""Builds the Wallzilla Explore catalog from wallpapers/*.mov.

For every video in wallpapers/:
  - slugifies the filename into a URL-safe id and renames the file in place
    (spaces in filenames break raw/CDN URLs once this folder is hosted on
    GitHub, so this repo's own asset files become the slugged names)
  - reads resolution + duration via ffprobe
  - captures the literal first frame as a JPEG thumbnail via ffmpeg
  - records the file size in bytes
  - assigns a best-effort category from a keyword heuristic (edit the
    resulting metadata.json by hand afterwards for anything mis-tagged)

Writes metadata.json at the repo root. Run with: python3 generate_catalog.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
WALLPAPERS_DIR = REPO_ROOT / "wallpapers"
THUMBNAILS_DIR = REPO_ROOT / "thumbnails"
METADATA_PATH = REPO_ROOT / "metadata.json"

GITHUB_PUSH_LIMIT_BYTES = 100 * 1024 * 1024

# Keyword -> category. Checked in order, first match wins. Falls back to
# "Explore" if nothing matches. Purely a starting point — metadata.json is
# plain JSON, easy to hand-edit afterwards.
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Space", ["space", "stars", "black hole", "star"]),
    ("Games", [
        "mondstadt", "liyue", "inazuma", "furina", "genshin",
        "hollow knight", "minecraft", "ps4", "playstation", "gameboy",
        "pixel", "f1",
    ]),
    ("Anime", [
        "totoro", "frieren", "mahoraga", "denji", "luffy", "spiderman",
        "sabrina", "jake",
    ]),
    ("Movies", [
        "blade runner", "batman", "deadpool", "darth vader",
        "silver surfer",
    ]),
    ("Atmosphere", [
        "coffee", "cafe", "train", "temple", "street", "camera",
        "retro", "monochrome", "dune", "rain",
    ]),
    ("Nature", [
        "cat", "dog", "frog", "toad", "pigeon", "forest", "tree",
        "flower", "hydrangea", "grass", "field", "koi", "pond", "ocean",
        "beach", "fuji", "waterfall", "firefl", "whale", "cherry blossom",
        "sakura",
    ]),
]


@dataclass
class CatalogEntry:
    id: str
    title: str
    category: str
    resolution: str
    duration: float
    fileSizeBytes: int
    videoPath: str
    thumbnailPath: str


def slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def categorize(title: str) -> str:
    lowered = title.lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "Explore"


def run_ffprobe(video_path: Path) -> tuple[str, float]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    width, height = stream["width"], stream["height"]
    duration = float(data["format"]["duration"])
    return f"{width}x{height}", round(duration, 2)


def generate_thumbnail(video_path: Path, thumbnail_path: Path) -> None:
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", "0",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "3",
            str(thumbnail_path),
        ],
        capture_output=True, check=True,
    )


def main() -> int:
    if not WALLPAPERS_DIR.is_dir():
        print(f"error: {WALLPAPERS_DIR} not found", file=sys.stderr)
        return 1

    source_videos = sorted(
        p for p in WALLPAPERS_DIR.glob("*.mov") if not p.name.startswith(".")
    )
    if not source_videos:
        print(f"error: no .mov files found in {WALLPAPERS_DIR}", file=sys.stderr)
        return 1

    entries: list[CatalogEntry] = []
    oversized: list[tuple[str, int]] = []
    failures: list[tuple[str, str]] = []
    used_ids: set[str] = set()

    for video_path in source_videos:
        title = video_path.stem
        base_id = slugify(title)
        entry_id = base_id
        suffix = 2
        while entry_id in used_ids:
            entry_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(entry_id)

        renamed_path = WALLPAPERS_DIR / f"{entry_id}.mov"
        try:
            if video_path != renamed_path:
                video_path.rename(renamed_path)

            resolution, duration = run_ffprobe(renamed_path)
            thumbnail_path = THUMBNAILS_DIR / f"{entry_id}.jpg"
            generate_thumbnail(renamed_path, thumbnail_path)

            size_bytes = renamed_path.stat().st_size
            if size_bytes > GITHUB_PUSH_LIMIT_BYTES:
                oversized.append((renamed_path.name, size_bytes))

            entries.append(CatalogEntry(
                id=entry_id,
                title=title,
                category=categorize(title),
                resolution=resolution,
                duration=duration,
                fileSizeBytes=size_bytes,
                videoPath=f"wallpapers/{renamed_path.name}",
                thumbnailPath=f"thumbnails/{thumbnail_path.name}",
            ))
            print(f"  ok  {title!r:40s} -> {entry_id}  ({resolution}, {duration}s, {size_bytes / 1_000_000:.1f}MB)")
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else str(exc.stderr)
            failures.append((title, stderr.strip().splitlines()[-1] if stderr else str(exc)))
            print(f"  FAIL {title!r:40s} -> {failures[-1][1]}")

    manifest = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(entries),
        "wallpapers": [asdict(e) for e in entries],
    }
    METADATA_PATH.write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"\nWrote {METADATA_PATH} with {len(entries)} wallpapers.")

    if failures:
        print(f"\n{len(failures)} file(s) failed to process:")
        for title, reason in failures:
            print(f"  - {title}: {reason}")

    if oversized:
        limit_mb = GITHUB_PUSH_LIMIT_BYTES / 1_000_000
        print(f"\n{len(oversized)} file(s) exceed GitHub's ~{limit_mb:.0f}MB push limit "
              f"and will need Git LFS (or another hosting approach) to push successfully:")
        for name, size in oversized:
            print(f"  - {name}: {size / 1_000_000:.1f}MB")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
