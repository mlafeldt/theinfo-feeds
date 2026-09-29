# theinfo-feeds

A better way to read [The Information](https://www.theinformation.com): quick news and deep dives, each in its own feed and 500 stories deep, refreshed hourly.

Paste <https://mlafeldt.github.io/theinfo-feeds/> into your feed reader to pick one, or subscribe directly:

- [All](https://mlafeldt.github.io/theinfo-feeds/all.xml): everything in [The Information's own feed](https://www.theinformation.com/feed), minus the phantom updates
- [Briefings](https://mlafeldt.github.io/theinfo-feeds/briefings.xml): the short news items
- [Articles](https://mlafeldt.github.io/theinfo-feeds/articles.xml): the full-length reporting

## Why

Upstream serves only its latest 20 entries, briefings and articles mixed — about a day and a half's worth.
These feeds keep every entry seen since 2026-09-08 (the newest 500 per feed are published), offer briefings and articles separately, label each article the way the site does, and leave out the noise upstream adds between renders:

- a `?cb=` cache buster in the feed's id and self link that changes from fetch to fetch
- entries re-stamped in bulk: unrelated briefings, some a day apart in publication, converge on one `<updated>` value while their title, summary and authors stay byte-identical
- author lists that come back in a different order from one render to the next

Upstream's feed names no category either, while the site shows a label above each article's headline: its newsletter, such as AI Agenda or The Briefing, or a kicker such as Exclusive or Opinion.
These feeds start each article's content with that label, as in `[AI Agenda]`, which readers show at the head of the snippet under the title, and add it as an Atom `<category>` for readers that filter on one.
Briefings are labelled `[Briefing]` in the All feed, where they mix with articles.
Articles the site shows no label for, sponsored ones among them, get none.

## How

`scripts/feeds.py` fetches the feed and article pages from The Information's Heroku origin, folds the entries into `data/entries.json` and renders all three feeds from that archive into `public/`.
Published entry links still point to `www.theinformation.com`, where subscribers can sign in.
An archived entry is replaced when any field except `updated` changes, so upstream's re-stamps never count as edits.
Each new article's page is fetched once, five seconds apart to space requests to the origin, for the `articleSection` in its JSON-LD; the label is kept in the archive, through later edits.
Authors are kept in alphabetical order, since upstream's own order is not stable.
Only the archive is committed; the feeds are rendered afresh on every run, and redeployed whenever the archive changed or the code did.
The landing page, `public/index.html`, and its link-preview image, `public/og.png` (rendered from `scripts/og.html`), are static and made by hand; a test fails if the page's feed links fall out of step with the feeds.

Nothing is ever removed from the archive, so the script refuses a whole payload rather than let a bad entry in: anything that is not well-formed Atom with at least one entry, and any entry whose id or link points somewhere other than `www.theinformation.com`.
The latter happened on 2026-09-10, when upstream briefly rendered its Heroku origin hostname into entry links and ids, pointing readers at a host where no subscriber can sign in.
A red run and stale feeds beat republishing that.
Likewise, an article page that cannot be fetched or has no article JSON-LD fails the run, and the next run tries again.
For the same reason, the workflow runs the tests before it lets the script near the archive.

Run it, and its tests, locally with [uv](https://docs.astral.sh/uv/):

```console
$ uv run scripts/feeds.py
Add 1 article, edit 1 briefing

+ Article: [AI Agenda] …
~ Briefing: … (title)
$ uv run pytest
```

The archive was seeded from this repo's history: it started out as a byte-for-byte mirror of upstream's feed, a workaround for a Cloudflare rule that answered feed readers with a challenge page in September 2026.
That rule is gone, and so is the mirror.

## Scope

These feeds carry the **public** feed only — the same headlines and summaries upstream serves any visitor, plus the label from each article's public page, with no article bodies and nothing from behind the paywall; reading full articles needs a subscription.
Unofficial, not affiliated with or endorsed by The Information.
