# vols-porto-collector

A small scheduled job that reads public Google Flights fares for one route (Montreal YUL and
Porto OPO, both directions) with [fli](https://github.com/punitarani/fli) and writes them to a
private database. The app that reads that database is private, and so is the data.

- `collector/google.py`: the two Google Flights reads (the price calendar, and every flight for
  one date grouped by routing), in CAD for a Canadian point of sale.
- `diagnostics/check_google.py`: checks that a machine can read those prices without being
  blocked. Run it before trusting a new host.
- `.github/workflows/`: the schedule. Keys live in GitHub secrets, never in this repo.

Prices are for 1 adult, economy, without checked bags (Google's default).
