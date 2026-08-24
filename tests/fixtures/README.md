# Test fixtures

Two synthetic `raw_json` records shaped exactly like what `scraper_detail`
writes for a Marketplace listing page: the keys `fb_id`, `url`, `final_url`,
`title` and `text_lines` (the first ~100 non-empty lines of the rendered
page, in order). Runtime data lives outside the repository, so the
extractor tests need these to be reproducible without a logged-in session.

The listings, sellers and ids are invented. The page chrome around them
(navigation, categories, the "Daily pick" sidebar of other people's
listings) mirrors the real English-locale layout, because that chrome is
what the extractors have to cut away.

| file | case |
|------|------|
| `text_lines_good.json` | the ad body sits between `Description` and `Seller information` and is the description the scraper should keep |
| `text_lines_sidebar.json` | a short ad followed by the `Daily pick` sidebar: the longest text block on the page is the sidebar, not the ad, which is the bug the marker slicing exists to avoid |

## What the tests check on them

- `description_from_text_lines` keeps the slice between `Description` and
  `Seller information`, drops a trailing `See more`, and never swallows the
  sidebar;
- `clean_fb_title` strips `(N) Marketplace — … | Facebook` down to the
  listing title;
- `parse_published_at` reads the relative `Listed N hours ago` line.

The Russian-locale markers the scraper also understands are covered by short
inline cases in `tests/test_extractors.py`.

## Updating

If the shape of `raw_json` changes, regenerate both files by hand to the new
shape; keep them synthetic.
