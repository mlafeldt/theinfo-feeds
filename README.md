# theinfo-feeds

Feeds for [The Information](https://www.theinformation.com), built from its
[public feed](https://www.theinformation.com/feed), refreshed hourly and served from GitHub Pages:

| Feed       | URL                                                      |
| ---------- | -------------------------------------------------------- |
| All        | <https://mlafeldt.github.io/theinfo-feeds/all.xml>       |
| Briefings  | <https://mlafeldt.github.io/theinfo-feeds/briefings.xml> |
| Articles   | <https://mlafeldt.github.io/theinfo-feeds/articles.xml>  |

## Why

Upstream serves only its latest 20 entries, briefings and articles mixed — about a day and a
half's worth. These feeds keep every entry seen since 2026-09-08 (the newest 500 per feed are
published), offer briefings and articles separately, and leave out the noise upstream adds
between renders:

- a `?cb=` cache buster in the feed's id and self link that changes from fetch to fetch
- entries re-stamped in bulk: unrelated briefings, some a day apart in publication, converge
  on one `<updated>` value while their title, summary and authors stay byte-identical
- author lists that come back in a different order from one render to the next

## How

`scripts/feeds.py` fetches upstream, folds its entries into `data/entries.json` and renders all
three feeds from that archive into `public/`. An archived entry is replaced only when its
title, content, link or authors changed, so the re-stamps above never count as edits, and
authors are kept in alphabetical order, since upstream's own order is not stable. Only the
archive is committed; the feeds are rendered afresh on every run, and redeployed whenever the
archive changed or the code did.

Nothing is ever removed from the archive, so the script refuses a whole payload rather than
let a bad entry in: anything that is not well-formed Atom with at least one entry, and any
entry whose id or link points somewhere other than `www.theinformation.com`. The latter
happened on 2026-09-10, when upstream briefly rendered its Heroku origin hostname into entry
links and ids, pointing readers at a host where no subscriber can sign in. A red run and
stale feeds beat republishing that. For the same reason, the workflow runs the tests before
it lets the script near the archive.

Run it, and its tests, locally with [uv](https://docs.astral.sh/uv/):

```console
$ uv run scripts/feeds.py
Add 1 article, edit 1 briefing

+ Article: …
~ Briefing: … (title)
$ uv run pytest
```

The archive was seeded from this repo's history: it started out as a byte-for-byte mirror of
upstream's feed, a workaround for a Cloudflare rule that answered feed readers with a
challenge page in September 2026. That rule is gone, and so is the mirror.

## Scope

These feeds carry the **public** feed only — the same headlines and summaries upstream serves
any visitor, with no article bodies and nothing from behind the paywall. Unofficial, not
affiliated with or endorsed by The Information.
