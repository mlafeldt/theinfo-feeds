# theinfo-feeds

A better way to read [The Information](https://www.theinformation.com).

Separate feeds for briefings and articles, up to 500 stories each, with articles labeled like `[AI Agenda]`.

Paste <https://mlafeldt.github.io/theinfo-feeds/> into your feed reader, then pick a feed. Or subscribe directly:

| Feed | URL |
| --- | --- |
| All | <https://mlafeldt.github.io/theinfo-feeds/all.xml> |
| Briefings | <https://mlafeldt.github.io/theinfo-feeds/briefings.xml> |
| Articles | <https://mlafeldt.github.io/theinfo-feeds/articles.xml> |

## Why

Upstream serves only its latest 20 entries, briefings and articles mixed — about a day and a half's worth.
These feeds:

- keep every entry seen since 2026-09-08 and publish the newest 500 per feed
- split briefings from articles
- label articles, which upstream's feed doesn't
- drop the noise upstream adds between renders:
  - a `?cb=` cache buster in the feed's id and self link, new on every fetch
  - entries re-stamped in bulk: unrelated briefings converge on one `<updated>` while staying byte-identical
  - author lists in a different order on every render

A label is what the site shows above an article's headline: a newsletter (AI Agenda), a kicker (Exclusive), or Partner Content for sponsored articles.
Labels are:

- appended to titles, as in `OpenAI Math Result Stokes Data-Sharing Concerns [AI Agenda]`, and added as an Atom `<category>`
- shortened in titles only: `[Finance]` for The Information Finance
- `[Briefing]` for briefings in the All feed, where they mix with articles
- missing where the site shows none
- never added to the content, which stays as upstream wrote it

## How

`scripts/feeds.py`, run hourly:

- fetches the feed and new article pages
- folds entries into `data/entries.json`, the only thing committed; nothing is ever removed
- ignores changes to `updated` alone, so re-stamps aren't edits
- sorts authors, since upstream's order isn't stable
- labels each new article once, from its page's JSON-LD `articleSection`, 5 s between fetches; partner articles from their byline instead
- renders the three feeds into `public/`, redeployed when the archive or code changed

The run fails rather than publish something bad:

- a payload that isn't well-formed Atom with entries
- an entry whose id or link points to a host other than `www.theinformation.com`, as upstream served on 2026-09-10
- an article page that can't be fetched or has no article JSON-LD
- failing tests, which CI runs first

`public/index.html` and `public/og.png` (from `scripts/og.html`) are made by hand; a test keeps the page's links in step with the feeds.

Run it locally with [uv](https://docs.astral.sh/uv/):

```console
$ uv run scripts/feeds.py
Add 1 article, edit 1 briefing

+ Article: [AI Agenda] …
~ Briefing: … (title)
$ uv run pytest
```

## Scope

These feeds carry the **public** feed only: the headlines and summaries upstream serves any visitor, plus labels from public article pages and bylines.
No article bodies, nothing from behind the paywall; full articles need a subscription.
Unofficial, not affiliated with or endorsed by The Information.
