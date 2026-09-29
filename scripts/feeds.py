"""Fold The Information's public Atom feed into an archive and publish it as feeds.

Upstream only ever serves its latest 20 entries. Each run merges them into
data/entries.json, which keeps every entry ever seen, and renders the newest
entries from it as three Atom feeds: everything, briefings only, articles only.
Upstream's feed names no category, so each new article's page is fetched once
for the label the site shows above its headline, such as "AI Agenda".

The archive is written deterministically, so a run that learns nothing new
leaves it byte-identical and the workflow commits nothing.
"""

import json
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, NotRequired, TypedDict
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from feedgen.feed import FeedGenerator

HOST = "www.theinformation.com"
SITE = f"https://{HOST}"
SOURCE_HOST = "info-reader-production.herokuapp.com"
SOURCE = f"https://{SOURCE_HOST}"
UPSTREAM = f"{SOURCE}/feed"
PAGES = "https://mlafeldt.github.io/theinfo-feeds"

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_PATH = ROOT / "data" / "entries.json"
OUT_DIR = ROOT / "public"

# Readers download the whole file on every poll, so each feed publishes only
# up to this many entries. The archive itself is never trimmed.
MAX_ENTRIES = 500

# Maximum attempts per fetch, including the initial request.
ATTEMPTS = 5

# Seconds to wait before retrying a transient HTTP failure.
RETRY_DELAY = 5

# Seconds between article page fetches to avoid sending a burst to the origin.
PAGE_PACE = 5

# The byline of sponsored articles, and the label the site shows them with.
PARTNER_AUTHOR = "The Information Partnerships"
PARTNER_LABEL = "Partner Content"


class Entry(TypedDict):
    """Fields shared by fresh and archived entries, plus an archived article's label."""

    type: str
    link: str
    title: str
    content: str  # HTML
    authors: list[str]  # sorted
    published: str  # RFC 3339, as upstream wrote it
    updated: str
    # Articles only, once their page was fetched: the site's label, or None
    # for an article without one. Never fetched again.
    label: NotRequired[str | None]


Entries = dict[str, Entry]  # keyed by upstream's id


class Feed(NamedTuple):
    name: str
    title: str
    type: str | None  # None publishes every type

    def includes(self, entry: Entry) -> bool:
        return self.type is None or entry["type"] == self.type


# public/index.html links to each of these by hand; update it along with them.
FEEDS = [
    Feed("all.xml", "The Information: All", None),
    Feed("briefings.xml", "The Information: Briefings", "Briefing"),
    Feed("articles.xml", "The Information: Articles", "Article"),
]


class Change(NamedTuple):
    """An entry a run added to the archive or edited in it."""

    entry: Entry
    edited: tuple[str, ...]  # the fields that changed; empty for a new entry


# Upstream ids look like tag:www.theinformation.com,2005:Briefing/18111.
ID_RE = re.compile(rf"^tag:{re.escape(HOST)},2005:(?P<type>[A-Za-z]+)/\d+$")
ATOM = "{http://www.w3.org/2005/Atom}"


class Rejected(Exception):
    """Upstream served something we must not fold into the archive."""


def transient(e: httpx.HTTPError) -> bool:
    """Whether an HTTP failure can recover on a later attempt."""
    return (
        not isinstance(e, httpx.HTTPStatusError)
        or e.response.status_code in {408, 429}
        or e.response.status_code >= 500
    )


def fetch(url: str) -> bytes:
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = httpx.get(
                url,
                follow_redirects=True,
                timeout=60,
            )
            return resp.raise_for_status().content
        except httpx.HTTPError as e:
            if not transient(e) or attempt == ATTEMPTS:
                raise
            print(f"fetch attempt {attempt} for {url} failed: {e}", file=sys.stderr)
            time.sleep(RETRY_DELAY)


def text(e: ElementTree.Element, key: str) -> str:
    return (e.findtext(ATOM + key) or "").strip()


def required(e: ElementTree.Element, key: str) -> str:
    value = text(e, key)
    if not value:
        raise Rejected(f"entry {text(e, 'id')!r} has no {key}")
    return value


def timestamp(e: ElementTree.Element, key: str) -> str:
    value = required(e, key)
    try:
        aware = datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        aware = False
    if not aware:
        raise Rejected(
            f"entry {text(e, 'id')!r} has invalid {key} timestamp: {value!r}"
        )
    return value


def parse(payload: bytes) -> Entries:
    """Turn an upstream payload into entries, or reject it outright.

    Rejecting the whole payload rather than skipping bad entries is deliberate:
    anything that lands in the archive stays there, so a partial or malformed
    feed must never get that far.
    """
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as e:
        raise Rejected(
            f"malformed feed: {e}; first 200 bytes: {payload[:200]!r}"
        ) from None
    entries_xml = root.findall(ATOM + "entry")
    if root.tag != ATOM + "feed" or not entries_xml:
        raise Rejected(
            f"not an Atom feed with entries; first 200 bytes: {payload[:200]!r}"
        )

    entries: Entries = {}
    for e in entries_xml:
        id_ = required(e, "id")
        m = ID_RE.match(id_)
        # On 2026-09-10 upstream rendered its Heroku origin hostname into entry
        # ids and links, sending readers to a host where no subscriber session
        # can exist. Checking every id and link against the canonical host
        # catches that and anything like it.
        if not m:
            raise Rejected(f"unexpected entry id: {id_!r}")
        link = next(
            (
                el.get("href")
                for el in e.findall(ATOM + "link")
                if el.get("rel", "alternate") == "alternate"
            ),
            None,
        )
        if not link:
            raise Rejected(f"entry {id_!r} has no link")
        parts = urlsplit(link)
        if parts.scheme != "https" or parts.hostname != HOST:
            raise Rejected(f"unexpected entry link: {link!r}")
        entries[id_] = {
            "type": m["type"],
            "link": link,
            "title": required(e, "title"),
            "content": required(e, "content"),
            # Upstream's author order changes from one render to the next, and
            # no order it uses reliably matches the site's byline, so sort.
            "authors": sorted(
                filter(None, (text(a, "name") for a in e.findall(ATOM + "author")))
            ),
            "published": timestamp(e, "published"),
            "updated": timestamp(e, "updated"),
        }
    return entries


LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)


def page_label(page: str, link: str) -> str | None:
    """The label an article's page shows above its headline, if any.

    The page's JSON-LD names it as articleSection: a newsletter such as
    "AI Agenda", or a kicker such as "Exclusive" or "Opinion". An article
    without one gets a lowercase default like "technology" instead, which is
    not what the site shows, so it counts as none.
    """
    for m in LD_JSON_RE.finditer(page):
        ld = json.loads(m[1])
        if isinstance(ld, dict) and ld.get("@type") == "NewsArticle":
            sections = ld.get("articleSection", [])
            if not isinstance(sections, list) or not all(
                isinstance(s, str) for s in sections
            ):
                raise Rejected(f"unexpected articleSection on {link}: {sections!r}")
            return next((s for s in sections if s != s.lower()), None)
    # Without it the page is not an article page as we know it: a challenge
    # page, or a redesign this code has to learn about.
    raise Rejected(f"no NewsArticle JSON-LD on {link}")


def source_url(link: str) -> str:
    """The origin URL for a canonical article link."""
    parts = urlsplit(link)
    return urlunsplit(("https", SOURCE_HOST, parts.path, parts.query, ""))


def label_articles(archive: Entries) -> None:
    """Label every archived article that has no label yet, in place."""
    pace = 0
    for e in archive.values():
        if e["type"] != "Article" or "label" in e:
            continue
        # Partner pages name no section in JSON-LD; their byline labels them
        # without a fetch, so they do not affect pacing between page fetches.
        if PARTNER_AUTHOR in e["authors"]:
            e["label"] = PARTNER_LABEL
        else:
            if pace:
                time.sleep(pace)
            e["label"] = page_label(fetch(source_url(e["link"])).decode(), e["link"])
            pace = PAGE_PACE
        print(f"label {e['label']!r}: {e['link']}", file=sys.stderr)


def edits(old: Entry, new: Entry) -> tuple[str, ...]:
    """The fields a reader would see differ in between two versions of an entry.

    Upstream re-stamps <updated> in bulk without touching the entries, so a new
    timestamp alone does not count as an edit.
    """
    # A newly added field also counts as an edit for older archive entries.
    return tuple(
        k for k, value in new.items() if k != "updated" and old.get(k) != value
    )


def merge(archive: Entries, entries: Entries) -> list[Change]:
    """Fold fresh entries into the archive in place and return what changed."""
    changes = []
    for id_, new in entries.items():
        old = archive.get(id_)
        if old is None:
            fields = ()
        elif not (fields := edits(old, new)):
            continue
        archive[id_] = merged = new if old is None else old | new
        # Share the archived dict so labels filled in later reach message().
        changes.append(Change(merged, fields))
    return changes


def category(e: Entry, feed: Feed) -> str | None:
    """What to tell a feed's readers an entry is, unless the feed already says."""
    if e["type"] == "Article":
        return e.get("label")
    return e["type"] if feed.type is None else None


def short(label: str) -> str:
    """A label as titles show it, without the site's name every feed already has.

    Some newsletters carry it, as in "The Information Finance". Categories
    keep the label whole.
    """
    return label.removeprefix("The Information ")


def render(archive: Entries, feed: Feed) -> bytes:
    selected = sorted(
        ((id_, e) for id_, e in archive.items() if feed.includes(e)),
        key=lambda item: (datetime.fromisoformat(item[1]["published"]), item[0]),
        reverse=True,
    )[:MAX_ENTRIES]

    url = f"{PAGES}/{feed.name}"
    fg = FeedGenerator()
    fg.id(url)
    fg.title(feed.title)
    fg.language("en-US")
    fg.author(name="The Information")
    fg.link(href=SITE, rel="alternate", type="text/html")
    fg.link(href=url, rel="self", type="application/atom+xml")
    # Atom's <updated> is when the feed last changed meaningfully: its newest
    # entry, not the time it happened to be rendered.
    newest = (datetime.fromisoformat(e["updated"]) for _, e in selected)
    fg.updated(max(newest, default=datetime.fromtimestamp(0, UTC)))

    for id_, e in selected:
        fe = fg.add_entry(order="append")
        fe.id(id_)
        label = category(e, feed)
        fe.title(f"{e['title']} [{short(label)}]" if label else e["title"])
        fe.link(href=e["link"], rel="alternate", type="text/html")
        fe.content(e["content"], type="html")
        if label:
            fe.category(term=label)
        for author in e["authors"]:
            fe.author(name=author)
        fe.published(e["published"])
        fe.updated(e["updated"])

    return fg.atom_str(pretty=True)


def load_archive(archive_path: Path) -> Entries:
    return json.loads(archive_path.read_text()) if archive_path.exists() else {}


def dump_archive(archive: Entries, archive_path: Path) -> None:
    # Written aside and renamed into place: a half-written archive would fail to
    # load on the next run.
    tmp = archive_path.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(
        json.dumps(archive, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    tmp.replace(archive_path)


def publish(archive: Entries, out_dir: Path) -> None:
    rendered = {feed.name: render(archive, feed) for feed in FEEDS}
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in rendered.items():
        (out_dir / name).write_bytes(payload)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def count(changes: list[Change]) -> str:
    types = Counter(c.entry["type"] for c in changes)
    return ", ".join(plural(n, t.lower()) for t, n in sorted(types.items()))


def line(c: Change) -> str:
    e = c.entry
    label = e.get("label")
    prefix = f"[{short(label)}] " if label else ""
    return f"{e['type']}: {prefix}{e['title']}"


def message(changes: list[Change]) -> str:
    """The commit message for a run: counts per type, then one line per entry."""
    if not changes:
        return "No changes"
    added = [c for c in changes if not c.edited]
    edited = [c for c in changes if c.edited]
    parts = []
    if added:
        parts.append(f"add {count(added)}")
    if edited:
        parts.append(f"edit {count(edited)}")
    lines = [f"+ {line(c)}" for c in added]
    lines += [f"~ {line(c)} ({', '.join(c.edited)})" for c in edited]
    return ", ".join(parts).capitalize() + "\n\n" + "\n".join(lines)


def run(payload: bytes, archive_path: Path, out_dir: Path) -> str:
    """Validate and publish a payload, then save the archive and report changes."""
    entries = parse(payload)
    archive = load_archive(archive_path)
    changes = merge(archive, entries)
    # Before anything is written: a page that cannot be fetched or read fails
    # the run, and the next one tries again.
    label_articles(archive)
    # Render first so a failure cannot save entries the feeds cannot publish.
    publish(archive, out_dir)
    dump_archive(archive, archive_path)
    return message(changes)


def main() -> None:
    try:
        result = run(fetch(UPSTREAM), ARCHIVE_PATH, OUT_DIR)
    except Rejected as e:
        sys.exit(f"refusing to update: {e}")
    print(result)


if __name__ == "__main__":
    main()
