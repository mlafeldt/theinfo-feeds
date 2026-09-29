"""Tests for scripts/feeds.py. Run with: uv run pytest"""

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape

import feedparser
import feeds
import httpx
import pytest

UPSTREAM_FIXTURE = (Path(__file__).parent / "fixtures" / "upstream.xml").read_bytes()
ARTICLE_PAGE = (
    Path(__file__).parent / "fixtures" / "article.html"
).read_bytes()  # labelled "The Briefing"


def page(*sections: str) -> bytes:
    ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "articleSection": list(sections),
        }
    )
    return f'<html><head><script type="application/ld+json">{ld}</script></head></html>'.encode()


class Pages:
    """Fakes all HTTP GET requests: ARTICLE_PAGE unless a payload is supplied."""

    def __init__(self):
        self.served: dict[str, bytes] = {}
        self.fetched: list[str] = []

    def get(self, url: str, **kwargs) -> httpx.Response:
        self.fetched.append(url)
        return httpx.Response(
            200,
            content=self.served.get(url, ARTICLE_PAGE),
            request=httpx.Request("GET", url),
        )


@pytest.fixture(autouse=True)
def pages(monkeypatch) -> Pages:
    # No test touches the network, or waits between page fetches.
    fake = Pages()
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(feeds, "PAGE_PACE", 0)
    return fake


def id_(type_: str, n: int, host: str = feeds.HOST) -> str:
    return f"tag:{host},2005:{type_}/{n}"


def entry(
    n: int,
    type_: str = "Briefing",
    *,
    host: str = feeds.HOST,
    link_host: str = feeds.HOST,
    title: str | None = None,
    content: str = "<p>Body</p>",
    authors: tuple[str, ...] = ("Jane Doe",),
    published: str = "2026-09-20T10:00:00Z",
    updated: str | None = None,
) -> str:
    author_xml = "".join(f"<author><name>{escape(a)}</name></author>" for a in authors)
    return f"""
  <entry>
    <id>{id_(type_, n, host)}</id>
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


# fetch


@pytest.mark.parametrize("status, attempts", [(404, 1), (403, 1), (503, 5), (429, 5)])
def test_fetch_bounds_retries_and_skips_permanent_errors(monkeypatch, status, attempts):
    requested = []
    sleeps = []

    def get(url, **kwargs):
        requested.append(url)
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(feeds.time, "sleep", sleeps.append)
    with pytest.raises(httpx.HTTPStatusError):
        feeds.fetch(feeds.UPSTREAM)
    assert requested == [feeds.UPSTREAM] * attempts
    assert sleeps == [5] * (attempts - 1)


@pytest.mark.parametrize("status", [503, 408, 429, None])
def test_fetch_recovers_from_transient_errors(monkeypatch, status):
    requested = []
    sleeps = []

    def get(url, **kwargs):
        requested.append(url)
        request = httpx.Request("GET", url)
        if len(requested) == 1:
            if status is None:
                raise httpx.ConnectError("connection interrupted", request=request)
            return httpx.Response(status, request=request)
        return httpx.Response(200, content=b"feed", request=request)

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(feeds.time, "sleep", sleeps.append)
    assert feeds.fetch(feeds.UPSTREAM) == b"feed"
    assert requested == [feeds.UPSTREAM, feeds.UPSTREAM]
    assert sleeps == [5]


# parse


def test_parse_real_upstream_payload():
    entries = feeds.parse(UPSTREAM_FIXTURE)
    assert len(entries) == 20
    assert {e["type"] for e in entries.values()} == {"Briefing", "Article"}
    # Content is upstream's HTML string, unescaped and unsanitized.
    assert all(e["content"].startswith("<p>") for e in entries.values())


def test_parse_rejects_html_challenge_page():
    with pytest.raises(feeds.Rejected):
        feeds.parse(
            b"<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
        )


def test_parse_keeps_content_html_verbatim():
    html = (
        '<p style="color:red"><a href="/org-charts/openai">OpenAI</a></p>'
        '<iframe src="https://example.com/x"></iframe>'
    )
    [e] = feeds.parse(
        atom(entry(1, content=html), xml_base="https://www.theinformation.com/")
    ).values()
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
    xml = re.sub(rf"<{field}[ >].*?</{field}>", "", entry(2), count=1, flags=re.DOTALL)
    assert f"<{field}" not in xml
    with pytest.raises(feeds.Rejected, match=f"has no {field}"):
        feeds.parse(atom(entry(1), xml))


@pytest.mark.parametrize("field", ["published", "updated"])
@pytest.mark.parametrize(
    "value", ["2026-09-20T10:00:00", "Sun, 20 Sep 2026 10:00:00 +0000"]
)
def test_parse_rejects_unrenderable_timestamp(field, value):
    with pytest.raises(feeds.Rejected, match=field):
        feeds.parse(atom(entry(1, **{field: value})))


def test_parse_rejects_entry_without_link():
    xml = re.sub(r"<link [^>]*/>", "", entry(2))
    assert "<link" not in xml
    with pytest.raises(feeds.Rejected, match="has no link"):
        feeds.parse(atom(entry(1), xml))


@pytest.mark.parametrize(
    "host", ["info-reader-production.herokuapp.com", "wwwXtheinformation.com"]
)
def test_parse_rejects_foreign_host_in_id(host):
    with pytest.raises(feeds.Rejected, match="entry id"):
        feeds.parse(atom(entry(1), entry(2, host=host)))


def test_parse_rejects_foreign_host_in_link():
    with pytest.raises(feeds.Rejected, match="entry link"):
        feeds.parse(atom(entry(1, link_host="info-reader-production.herokuapp.com")))


@pytest.mark.parametrize("scheme", ["http", "ftp", ""])
@pytest.mark.parametrize("type_", ["Briefing", "Article"])
def test_parse_rejects_non_https_links(scheme, type_):
    xml = entry(1, type_).replace(
        'href="https:', f'href="{scheme + ":" if scheme else ""}'
    )
    with pytest.raises(feeds.Rejected, match="entry link"):
        feeds.parse(atom(xml))


# merge


def test_merge_reports_new_entries():
    archive = {}
    changes = feeds.merge(archive, feeds.parse(atom(entry(1), entry(2, "Article"))))
    assert [(c.entry["title"], c.edited) for c in changes] == [
        ("Story 1", ()),
        ("Story 2", ()),
    ]
    assert len(archive) == 2


def test_merge_ignores_reordered_authors():
    archive = feeds.parse(atom(entry(1, authors=("Zoe", "Adam"))))
    fresh = feeds.parse(
        atom(entry(1, authors=("Adam", "Zoe"), updated="2026-09-21T09:00:00Z"))
    )
    assert feeds.merge(archive, fresh) == []
    assert archive[id_("Briefing", 1)]["authors"] == [
        "Adam",
        "Zoe",
    ]


def test_merge_takes_published_correction():
    archive = feeds.parse(atom(entry(1)))
    fresh = feeds.parse(atom(entry(1, published="2026-09-19T10:00:00Z")))
    [change] = feeds.merge(archive, fresh)
    assert change.edited == ("published",)
    assert archive[id_("Briefing", 1)]["published"] == "2026-09-19T10:00:00Z"


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
    assert archive[id_("Briefing", 1)]["updated"] == "2026-09-21T09:00:00Z"


def test_merge_keeps_label_through_edits():
    archive = feeds.parse(atom(entry(2, "Article")))
    archive[id_("Article", 2)]["label"] = "Dealmaker"
    fresh = feeds.parse(atom(entry(2, "Article", title="New headline")))
    [c] = feeds.merge(archive, fresh)
    assert c.edited == ("title",)
    assert archive[id_("Article", 2)]["label"] == "Dealmaker"


# label


def test_page_label_reads_real_page():
    assert feeds.page_label(ARTICLE_PAGE.decode(), "link") == "The Briefing"


@pytest.mark.parametrize(
    "sections, want",
    [
        (("Exclusive",), "Exclusive"),
        (("Q&A",), "Q&A"),
        # What the page says for an article the site shows no label for.
        (("technology",), None),
        # What the page says for partner articles, which their byline labels instead.
        ((), None),
    ],
)
def test_page_label(sections, want):
    assert feeds.page_label(page(*sections).decode(), "link") == want


@pytest.mark.parametrize(
    "sections", ["AI Agenda", 42, False, None, {}, ["AI Agenda", 42]]
)
def test_page_label_rejects_unexpected_sections(sections):
    ld = json.dumps({"@type": "NewsArticle", "articleSection": sections})
    html = f'<script type="application/ld+json">{ld}</script>'
    with pytest.raises(feeds.Rejected, match="unexpected articleSection on link"):
        feeds.page_label(html, "link")


def test_page_label_allows_missing_section():
    assert (
        feeds.page_label(
            '<script type="application/ld+json">{"@type":"NewsArticle"}</script>',
            "link",
        )
        is None
    )


def test_page_label_rejects_page_without_news_article():
    with pytest.raises(feeds.Rejected, match="no NewsArticle JSON-LD on link"):
        feeds.page_label(
            "<html><head><title>Just a moment...</title></head></html>", "link"
        )


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
    archive = feeds.parse(
        atom(*(entry(n, published=f"2026-09-{n:02d}T10:00:00Z") for n in range(1, 8)))
    )
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
        atom(
            entry(1, updated="2026-09-20T12:00:00Z"),
            entry(2, published="2026-09-19T10:00:00Z"),
        )
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
        suffix = " [Briefing]" if o["type"] == "Briefing" else ""
        assert e.title == o["title"] + suffix
        assert e.link == o["link"]
        assert e.content[0].value == o["content"]
        assert [a.name for a in e.authors] == o["authors"]
        assert datetime.fromisoformat(e.published) == datetime.fromisoformat(
            o["published"]
        )


def test_render_labels_what_the_feed_does_not_say():
    archive = feeds.parse(
        atom(entry(1), entry(2, "Article"), entry(3, "Article"), entry(4, "Article"))
    )
    archive[id_("Article", 2)]["label"] = "Q&A"
    archive[id_("Article", 3)]["label"] = None
    archive[id_("Article", 4)]["label"] = "The Information Finance"

    def labels(name: str) -> dict[str, tuple[str, str, list[str]]]:
        return {
            e.id.rsplit(":", 1)[1]: (
                e.title,
                e.content[0].value,
                [t.term for t in e.get("tags", [])],
            )
            for e in rendered(archive, name).entries
        }

    assert labels("all.xml") == {
        "Briefing/1": ("Story 1 [Briefing]", "<p>Body</p>", ["Briefing"]),
        "Article/2": ("Story 2 [Q&A]", "<p>Body</p>", ["Q&A"]),
        "Article/3": ("Story 3", "<p>Body</p>", []),
        "Article/4": ("Story 4 [Finance]", "<p>Body</p>", ["The Information Finance"]),
    }
    assert labels("briefings.xml") == {"Briefing/1": ("Story 1", "<p>Body</p>", [])}
    assert labels("articles.xml") == {
        "Article/2": ("Story 2 [Q&A]", "<p>Body</p>", ["Q&A"]),
        "Article/3": ("Story 3", "<p>Body</p>", []),
        "Article/4": ("Story 4 [Finance]", "<p>Body</p>", ["The Information Finance"]),
    }


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
    code_urls = [
        code.replace("<wbr>", "")
        for code in re.findall(r"<code>(.*?)</code>", html, flags=re.DOTALL)
    ]
    assert found == [
        ("application/atom+xml", f.title, f"{feeds.PAGES}/{f.name}")
        for f in feeds.FEEDS
    ]
    assert code_urls == [
        f"{feeds.PAGES}/",
        *(f"{feeds.PAGES}/{f.name}" for f in feeds.FEEDS),
    ]


# run and summary


class Site:
    """A temporary archive and its published feeds."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.archive_path = root / "entries.json"
        self.out_dir = root / "feeds"

    def run(self, payload: bytes = UPSTREAM_FIXTURE) -> str:
        return feeds.run(payload, self.archive_path, self.out_dir)

    def archive(self) -> feeds.Entries:
        return json.loads(self.archive_path.read_text())

    def snapshot(self) -> dict[Path, bytes]:
        return {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


def test_run_publishes_fixture_and_replay_keeps_bytes(site):
    assert site.run().startswith("Add ")
    archived_entries = site.archive()
    assert archived_entries == {
        id_: e | ({"label": "The Briefing"} if e["type"] == "Article" else {})
        for id_, e in feeds.parse(UPSTREAM_FIXTURE).items()
    }
    for feed in feeds.FEEDS:
        published = feedparser.parse((site.out_dir / feed.name).read_bytes())
        assert not published.bozo
        assert {e.id for e in published.entries} == {
            id_
            for id_, e in archived_entries.items()
            if feed.type is None or e["type"] == feed.type
        }

    before = site.snapshot()
    assert site.run() == "No changes"
    assert site.snapshot() == before


def test_run_rejects_whole_payload_before_touching_archive_or_feeds(site):
    site.run()
    before = site.snapshot()

    with pytest.raises(feeds.Rejected, match="entry link"):
        site.run(atom(entry(21), entry(22, link_host="wrong.example")))

    assert site.snapshot() == before


def test_run_renders_all_feeds_before_writing(site, monkeypatch):
    site.run()
    before = site.snapshot()
    original_render = feeds.render

    def fail_on_articles(archive, feed):
        if feed.name == "articles.xml":
            raise ValueError("cannot render articles")
        return original_render(archive, feed)

    monkeypatch.setattr(feeds, "render", fail_on_articles)
    with pytest.raises(ValueError, match="cannot render articles"):
        site.run(atom(entry(21, "Article")))
    assert site.snapshot() == before


def test_run_labels_new_articles_once(site, pages):
    link = "https://www.theinformation.com/articles/story-2"
    source_link = "https://info-reader-production.herokuapp.com/articles/story-2"
    pages.served[source_link] = page("AI Agenda")
    payload = atom(entry(1), entry(2, "Article"))

    assert site.run(payload) == (
        "Add 1 article, 1 briefing\n\n+ Briefing: Story 1\n+ Article: [AI Agenda] Story 2"
    )
    archived_entries = site.archive()
    assert archived_entries[id_("Article", 2)]["label"] == "AI Agenda"
    assert "label" not in archived_entries[id_("Briefing", 1)]

    assert site.run(atom(entry(2, "Article", title="New headline"))) == (
        "Edit 1 article\n\n~ Article: [AI Agenda] New headline (title)"
    )
    assert pages.fetched == [source_link]
    assert archived_entries[id_("Article", 2)]["link"] == link


def test_run_labels_partner_articles_by_byline(site, pages):
    payload = atom(entry(2, "Article", authors=("The Information Partnerships",)))

    assert site.run(payload) == "Add 1 article\n\n+ Article: [Partner Content] Story 2"
    assert site.archive()[id_("Article", 2)]["label"] == "Partner Content"
    assert pages.fetched == []


def test_run_labels_articles_archived_before_labels(site, pages):
    site.run(atom(entry(1, "Article")))
    archived_entries = site.archive()
    del archived_entries[id_("Article", 1)]["label"]
    site.archive_path.write_text(json.dumps(archived_entries))

    assert site.run(atom(entry(2))) == "Add 1 briefing\n\n+ Briefing: Story 2"
    assert site.archive()[id_("Article", 1)]["label"] == "The Briefing"
    assert len(pages.fetched) == 2


def test_run_rejects_unreadable_page_before_touching_archive_or_feeds(site, pages):
    site.run()
    before = site.snapshot()
    pages.served["https://info-reader-production.herokuapp.com/articles/story-21"] = (
        b"<title>Just a moment...</title>"
    )

    with pytest.raises(feeds.Rejected, match="no NewsArticle JSON-LD"):
        site.run(atom(entry(21, "Article")))

    assert site.snapshot() == before


def test_run_keeps_entries_upstream_dropped(site):
    site.run()

    assert site.run(atom(entry(21))).startswith("Add 1 briefing")
    assert len(site.archive()) == 21
    assert len(feedparser.parse((site.out_dir / "all.xml").read_bytes()).entries) == 21


def test_run_ignores_restamped_updated(site):
    site.run(atom(entry(1)))
    before = site.snapshot()

    assert site.run(atom(entry(1, updated="2026-09-21T09:00:00Z"))) == "No changes"
    assert site.snapshot() == before
    assert site.archive()[id_("Briefing", 1)]["updated"] == "2026-09-20T10:00:00Z"


def test_main_reports_rejected_payload(site, pages, monkeypatch):
    pages.served[feeds.UPSTREAM] = atom(entry(1, link_host="wrong.example"))
    monkeypatch.setattr(feeds, "ARCHIVE_PATH", site.archive_path)
    monkeypatch.setattr(feeds, "OUT_DIR", site.out_dir)

    with pytest.raises(SystemExit, match="refusing to update: unexpected entry link"):
        feeds.main()
    assert pages.fetched == [feeds.UPSTREAM]
    assert site.snapshot() == {}


def change(
    type_: str, title: str, *edited: str, label: str | None = None
) -> feeds.Change:
    [e] = feeds.parse(atom(entry(1, type_, title=title))).values()
    if type_ == "Article":
        e["label"] = label
    return feeds.Change(e, edited)


@pytest.mark.parametrize(
    "changes, want",
    [
        ([], "No changes"),
        (
            [
                change("Briefing", "B1"),
                change("Article", "A1"),
                change("Briefing", "B2"),
            ],
            "Add 1 article, 2 briefings\n\n+ Briefing: B1\n+ Article: A1\n+ Briefing: B2",
        ),
        (
            [change("Article", "A1", "content")],
            "Edit 1 article\n\n~ Article: A1 (content)",
        ),
        (
            [change("Article", "A1", label="AI Agenda")],
            "Add 1 article\n\n+ Article: [AI Agenda] A1",
        ),
        (
            [change("Article", "A1", label="The Information Finance")],
            "Add 1 article\n\n+ Article: [Finance] A1",
        ),
        (
            [change("Briefing", "B1", "title", "authors"), change("Briefing", "B2")],
            "Add 1 briefing, edit 1 briefing\n\n+ Briefing: B2\n~ Briefing: B1 (title, authors)",
        ),
    ],
)
def test_message(changes, want):
    assert feeds.message(changes) == want
