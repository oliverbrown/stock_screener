## What this changes

<!-- What does this do, and why? Link the issue if there is one: "Closes #123". -->

## How it was tested

<!-- New or changed tests, and anything you checked by hand. If you verified
     against live Yahoo data, say which tickers; the tests themselves must
     stay offline. -->

## Checklist

- [ ] `python3 -m pytest` passes
- [ ] Tests added or updated for the change, and they don't contact Yahoo or any other site
- [ ] `README.md` updated if options or behaviour changed
- [ ] If `fetch_stock`'s output fields changed: `TickerCache.VERSION` bumped, and new fields added to `FIELDS`
- [ ] No personal watch lists, reports, logs or cache files included
