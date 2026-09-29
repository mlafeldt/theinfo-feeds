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

## Scope

These feeds carry the **public** feed only: the headlines and summaries upstream serves any visitor, plus labels from public article pages and bylines.
No article bodies, nothing from behind the paywall; full articles need a subscription.
Unofficial, not affiliated with or endorsed by The Information.

## Development

[`scripts/feeds.py`](scripts/feeds.py) runs hourly in GitHub Actions; its docstrings explain the rest.

Run it, and its tests, locally with [uv](https://docs.astral.sh/uv/):

```console
$ uv run scripts/feeds.py
Add 1 article, edit 1 briefing

+ Article: [AI Agenda] …
~ Briefing: … (title)
$ uv run pytest
```

Lint and format with [Ruff](https://docs.astral.sh/ruff/), and check types with [ty](https://docs.astral.sh/ty/):

```console
$ uv run ruff check --fix
$ uv run ruff format
$ uv run ty check
```
