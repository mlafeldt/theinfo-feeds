"""Tests for scripts/feeds.py. Run with: uv run pytest"""

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape

import feedparser
import pytest

import feeds

UPSTREAM_FIXTURE = (Path(__file__).parent / "fixtures" / "upstream.xml").read_bytes()


def entry(
    n: int,
    type_: str = "Briefing",
    *,
    host: str = "www.theinformation.com",
    link_host: str = "www.theinformation.com",
    title: str | None = None,
    content: str = "<p>Body</p>",
    authors: tuple[str, ...] = ("Jane Doe",),
    published: str = "2026-09-20T10:00:00Z",
    updated: str | None = None,
) -> str:
    author_xml = "".join(f"<author><name>{escape(a)}</name></author>" for a in authors)
    return f"""
  <entry>
    <id>tag:{host},2005:{type_}/{n}</id>
    <published>{published}</published>
    <updated>{updated or published}</updated>
    <link rel="alternate" type="text/html" href="https://{link_host}/{type_.lower()}s/story-{n}"/>
    <title>{escape(title or f"Story {n}")}</title>
    <content type="html">{escape(content)}</content>
    {author_xml}
  </entry>"""


def atom(*entries: str, xml_base: str | None = None) -> bytes:
    base = f' xml:base="{xml_base}"' if xml_base else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xml:lang="en-US" xmlns="http://www.w3.org/2005/Atom"{base}>
  <id>tag:www.theinformation.com,2005:/feed?cb=1790401542050</id>
  <link rel="self" type="application/atom+xml" href="https://www.theinformation.com/feed?cb=1790401542050"/>
  <title>The Information</title>
  <updated>2026-09-26T01:02:39Z</updated>
  {"".join(entries)}
</feed>""".encode()


FEED = {feed.name: feed for feed in feeds.FEEDS}


def rendered(archive: feeds.Entries, name: str) -> feedparser.FeedParserDict:
    return feedparser.parse(feeds.render(archive, FEED[name]), sanitize_html=False)


# parse


def test_parse_real_upstream_payload():
    entries = feeds.parse(UPSTREAM_FIXTURE)
    assert len(entries) == 20
    assert {e["type"] for e in entries.values()} == {"Briefing", "Article"}
    # Content is upstream's HTML string, unescaped and unsanitized.
    assert all(e["content"].startswith("<p>") for e in entries.values())


def test_parse_rejects_html_challenge_page():
    with pytest.raises(feeds.Rejected):
        feeds.parse(b"<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>")


def test_parse_keeps_content_html_verbatim():
    html = (
        '<p style="color:red"><a href="/org-charts/openai">OpenAI</a></p>'
        '<iframe src="https://example.com/x"></iframe>'
    )
    [e] = feeds.parse(atom(entry(1, content=html), xml_base="https://www.theinformation.com/")).values()
    assert e["content"] == html


def test_parse_rejects_truncated_payload():
    # A cut-off download still parses into entries unless malformed XML is refused.
    with pytest.raises(feeds.Rejected, match="malformed"):
        feeds.parse(UPSTREAM_FIXTURE[: len(UPSTREAM_FIXTURE) // 2])


def test_parse_rejects_feed_without_entries():
    with pytest.raises(feeds.Rejected):
        feeds.parse(atom())


@pytest.mark.parametrize("field", ["title", "content", "published", "updated"])
def test_parse_rejects_entry_missing_a_field(field):
    xml = re.sub(rf"<{field}[ >].*?</{field}>", "", entry(2), count=1, flags=re.S)
    assert f"<{field}" not in xml
    with pytest.raises(feeds.Rejected, match=f"has no {field}"):
        feeds.parse(atom(entry(1), xml))


@pytest.mark.parametrize("field", ["published", "updated"])
@pytest.mark.parametrize("value", ["2026-09-20T10:00:00", "Sun, 20 Sep 2026 10:00:00 +0000"])
def test_parse_rejects_unrenderable_timestamp(field, value):
    with pytest.raises(feeds.Rejected, match=field):
        feeds.parse(atom(entry(1, **{field: value})))


def test_parse_rejects_entry_without_link():
    xml = re.sub(r"<link [^>]*/>", "", entry(2))
    assert "<link" not in xml
    with pytest.raises(feeds.Rejected, match="has no link"):
        feeds.parse(atom(entry(1), xml))


@pytest.mark.parametrize("host", ["info-reader-production.herokuapp.com", "wwwXtheinformation.com"])
def test_parse_rejects_foreign_host_in_id(host):
    with pytest.raises(feeds.Rejected, match="entry id"):
        feeds.parse(atom(entry(1), entry(2, host=host)))


def test_parse_rejects_foreign_host_in_link():
    with pytest.raises(feeds.Rejected, match="entry link"):
        feeds.parse(atom(entry(1, link_host="info-reader-production.herokuapp.com")))


# merge


def test_merge_reports_new_entries():
    archive = {}
    changes = feeds.merge(archive, feeds.parse(atom(entry(1), entry(2, "Article"))))
    assert [(c.entry["title"], c.edited) for c in changes] == [("Story 1", ()), ("Story 2", ())]
    assert len(archive) == 2


def test_merge_ignores_reordered_authors():
    archive = feeds.parse(atom(entry(1, authors=("Zoe", "Adam"))))
    fresh = feeds.parse(atom(entry(1, authors=("Adam", "Zoe"), updated="2026-09-21T09:00:00Z")))
    assert feeds.merge(archive, fresh) == []
    assert archive["tag:www.theinformation.com,2005:Briefing/1"]["authors"] == ["Adam", "Zoe"]


def test_merge_takes_published_correction():
    archive = feeds.parse(atom(entry(1)))
    fresh = feeds.parse(atom(entry(1, published="2026-09-19T10:00:00Z")))
    [change] = feeds.merge(archive, fresh)
    assert change.edited == ("published",)
    assert archive["tag:www.theinformation.com,2005:Briefing/1"]["published"] == "2026-09-19T10:00:00Z"


@pytest.mark.parametrize(
    "change",
    [
        {"title": "New headline"},
        {"content": "<p>Revised body</p>"},
        {"authors": ("Jane Doe", "John Roe")},
    ],
)
def test_merge_takes_real_edits(change):
    archive = feeds.parse(atom(entry(1)))
    fresh = feeds.parse(atom(entry(1, updated="2026-09-21T09:00:00Z", **change)))
    [c] = feeds.merge(archive, fresh)
    assert c.edited == tuple(change)
    assert archive["tag:www.theinformation.com,2005:Briefing/1"]["updated"] == "2026-09-21T09:00:00Z"


# render


def test_render_splits_by_type():
    archive = feeds.parse(atom(entry(1), entry(2, "Article"), entry(3, "Podcast")))

    def ids(name: str) -> set[str]:
        return {e.id.rsplit(":", 1)[1] for e in rendered(archive, name).entries}

    assert ids("all.xml") == {"Briefing/1", "Article/2", "Podcast/3"}
    assert ids("briefings.xml") == {"Briefing/1"}
    assert ids("articles.xml") == {"Article/2"}


def test_render_orders_newest_first_and_caps(monkeypatch):
    monkeypatch.setattr(feeds, "MAX_ENTRIES", 3)
    archive = feeds.parse(atom(*(entry(n, published=f"2026-09-{n:02d}T10:00:00Z") for n in range(1, 8))))
    assert len(archive) == 7
    got = [e.id.rsplit("/", 1)[1] for e in rendered(archive, "all.xml").entries]
    assert got == ["7", "6", "5"]


def test_render_has_our_identity_and_no_cache_buster():
    archive = feeds.parse(UPSTREAM_FIXTURE)
    for feed in feeds.FEEDS:
        out = feeds.render(archive, feed)
        assert b"cb=" not in out
        d = feedparser.parse(out)
        assert not d.bozo
        assert d.feed.id == f"{feeds.PAGES}/{feed.name}"
        assert d.feed.title == feed.title
        assert {(link.rel, link.href) for link in d.feed.links} == {
            ("alternate", feeds.SITE),
            ("self", f"{feeds.PAGES}/{feed.name}"),
        }


def test_render_takes_feed_updated_from_newest_entry():
    archive = feeds.parse(
        atom(entry(1, updated="2026-09-20T12:00:00Z"), entry(2, published="2026-09-19T10:00:00Z"))
    )
    assert rendered(archive, "all.xml").feed.updated == "2026-09-20T12:00:00+00:00"


def test_render_keeps_sorted_author_order():
    archive = feeds.parse(atom(entry(1, authors=("Zoe", "Adam", "Mia"))))
    [e] = rendered(archive, "all.xml").entries
    assert [a.name for a in e.authors] == ["Adam", "Mia", "Zoe"]


def test_render_round_trips_entry_fields():
    archive = feeds.parse(UPSTREAM_FIXTURE)
    out = rendered(archive, "all.xml").entries
    assert len(out) == len(archive)
    for e in out:
        o = archive[e.id]
        assert e.title == o["title"]
        assert e.link == o["link"]
        assert e.content[0].value == o["content"]
        assert [a.name for a in e.authors] == o["authors"]
        assert datetime.fromisoformat(e.published) == datetime.fromisoformat(o["published"])


# index


def test_index_links_every_feed():
    # public/index.html is written by hand, so it must follow any change to FEEDS.
    found = []

    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "link" and a.get("rel") == "alternate":
                found.append((a["type"], a["title"], a["href"]))

    html = (feeds.OUT_DIR / "index.html").read_text()
    Links().feed(html)
    code_urls = [code.replace("<wbr>", "") for code in re.findall(r"<code>(.*?)</code>", html, flags=re.S)]
    assert found == [("application/atom+xml", f.title, f"{feeds.PAGES}/{f.name}") for f in feeds.FEEDS]
    assert code_urls == [f"{feeds.PAGES}/", *(f"{feeds.PAGES}/{f.name}" for f in feeds.FEEDS)]


# run and summary


def run_in_tmp(tmp_path: Path, payload: bytes = UPSTREAM_FIXTURE) -> tuple[str, Path, Path]:
    archive_path = tmp_path / "entries.json"
    out_dir = tmp_path / "feeds"
    return feeds.run(payload, archive_path, out_dir), archive_path, out_dir


def test_run_publishes_fixture_and_replay_keeps_archive_bytes(tmp_path):
    result, archive_path, out_dir = run_in_tmp(tmp_path)
    archived_entries = json.loads(archive_path.read_text())
    assert result.startswith("Add ")
    assert archived_entries == feeds.parse(UPSTREAM_FIXTURE)
    for feed in feeds.FEEDS:
        published = feedparser.parse((out_dir / feed.name).read_bytes())
        assert not published.bozo
        assert {e.id for e in published.entries} == {
            id_ for id_, e in archived_entries.items() if feed.type is None or e["type"] == feed.type
        }

    before = archive_path.read_bytes()
    assert feeds.run(UPSTREAM_FIXTURE, archive_path, out_dir) == "No changes"
    assert archive_path.read_bytes() == before


def test_run_rejects_whole_payload_before_touching_archive_or_feeds(tmp_path):
    _, archive_path, out_dir = run_in_tmp(tmp_path)
    before = archive_path.read_bytes()
    feed_bytes = {feed.name: (out_dir / feed.name).read_bytes() for feed in feeds.FEEDS}

    with pytest.raises(feeds.Rejected, match="entry link"):
        feeds.run(atom(entry(21), entry(22, link_host="wrong.example")), archive_path, out_dir)

    assert archive_path.read_bytes() == before
    assert {feed.name: (out_dir / feed.name).read_bytes() for feed in feeds.FEEDS} == feed_bytes


def test_run_renders_before_saving_archive(tmp_path, monkeypatch):
    _, archive_path, out_dir = run_in_tmp(tmp_path)
    before = archive_path.read_bytes()
    original_render = feeds.render

    def fail_on_articles(archive, feed):
        if feed.name == "articles.xml":
            raise ValueError("cannot render articles")
        return original_render(archive, feed)

    monkeypatch.setattr(feeds, "render", fail_on_articles)
    with pytest.raises(ValueError, match="cannot render articles"):
        feeds.run(atom(entry(21, "Article")), archive_path, out_dir)
    assert archive_path.read_bytes() == before


def test_run_keeps_entries_upstream_dropped(tmp_path):
    _, archive_path, out_dir = run_in_tmp(tmp_path)

    assert feeds.run(atom(entry(21)), archive_path, out_dir).startswith("Add 1 briefing")
    assert len(json.loads(archive_path.read_text())) == 21
    assert len(feedparser.parse((out_dir / "all.xml").read_bytes()).entries) == 21


def test_run_ignores_restamped_updated(tmp_path):
    _, archive_path, out_dir = run_in_tmp(tmp_path, atom(entry(1)))
    before = archive_path.read_bytes()

    assert feeds.run(atom(entry(1, updated="2026-09-21T09:00:00Z")), archive_path, out_dir) == "No changes"
    assert archive_path.read_bytes() == before
    assert json.loads(archive_path.read_text())["tag:www.theinformation.com,2005:Briefing/1"]["updated"] == (
        "2026-09-20T10:00:00Z"
    )


def test_main_reports_rejected_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(feeds, "fetch", lambda: atom(entry(1, link_host="wrong.example")))
    monkeypatch.setattr(feeds, "ARCHIVE_PATH", tmp_path / "entries.json")
    monkeypatch.setattr(feeds, "OUT_DIR", tmp_path / "feeds")

    with pytest.raises(SystemExit, match="refusing to update: unexpected entry link"):
        feeds.main()
    assert not (tmp_path / "entries.json").exists()


def change(type_: str, title: str, *edited: str) -> feeds.Change:
    return feeds.Change({"type": type_, "title": title}, edited)


@pytest.mark.parametrize(
    "changes, want",
    [
        ([], "No changes"),
        (
            [change("Briefing", "B1"), change("Article", "A1"), change("Briefing", "B2")],
            "Add 1 article, 2 briefings\n\n+ Briefing: B1\n+ Article: A1\n+ Briefing: B2",
        ),
        (
            [change("Article", "A1", "content")],
            "Edit 1 article\n\n~ Article: A1 (content)",
        ),
        (
            [change("Briefing", "B1", "title", "authors"), change("Briefing", "B2")],
            "Add 1 briefing, edit 1 briefing\n\n+ Briefing: B2\n~ Briefing: B1 (title, authors)",
        ),
    ],
)
def test_message(changes, want):
    assert feeds.message(changes) == want
