# Sample data

Everything here is invented. It exists so the pipeline can be tried end to
end without a Facebook session:

| file | what it stands in for |
|------|------------------------|
| `sample_listings.json` | nine scraped ads in the shape `scrape` writes (the ninth is a repost of the first, so `seed` records a duplicate) |
| `sample_ai_result.json` | the result file the `/analyze` skill would write after reading those ads against `config/requirements.md` |

```sh
export RENT_ASSISTANT_HOME=$(mktemp -d)
rent-assistant seed examples/sample_listings.json
rent-assistant ai-apply examples/sample_ai_result.json
rent-assistant rate 9000000000000001 5
rent-assistant export
open "$RENT_ASSISTANT_HOME/rentals.html"
```

Four of the eight ads are rejected by `rules.py` on the facts the result
file carries (Patong, price above 40,000 THB, a studio, pets forbidden);
the rest land in the Review tab with a score, except the one you rated,
which is on the Shortlist tab.
