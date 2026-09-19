# Swiss GTFS to SQLite

Build a compact offline database from the GTFS files in `source/`.

1. Edit `INCLUDED_AGENCY_IDS` near the top of [`main.py`](main.py). Find IDs with:

   ```sh
   python3 main.py list-agencies --source source
   ```

2. Build and validate the database:

   ```sh
   python3 main.py build --source source --output swiss.sqlite
   ```

The builder prints progress while scanning the large feed and validates the result. Re-check an existing database with:

```sh
python3 main.py check swiss.sqlite
```

Run the automated fixture test with:

```sh
python3 -m unittest discover -s tests -v
```

The SQLite file keeps agencies, rail routes, trips, stops, compact service availability, compressed trip schedules, and compressed station indexes. GTFS calendar and stop-time rows are resolved into this app-oriented format to minimize size. Transfers, frequencies, and unused columns are omitted.

## Storage specification

The database is intentionally app-oriented rather than a complete GTFS copy.

| Table | Contents |
|---|---|
| `agency` | Selected operators only |
| `route` | Selected rail routes |
| `service` | Service IDs and daily availability bitmaps |
| `trip` | Train metadata and route/service links |
| `stop` | Station IDs, names, coordinates, and platform data |
| `trip_schedule` | One compressed schedule blob per trip |
| `station_trips` | Compressed list of trips serving each station |
| `feed_info` | Overall feed start and end dates |

`service.available_days` contains one bit for every day between `feed_info.feed_start_date` and `feed_info.feed_end_date`. A set bit means that service operates on that date. Weekly calendar rules are expanded first, then date additions/removals are applied.

Each `trip_schedule.schedule` blob contains an ordered sequence of:

```text
(stop_key, arrival_seconds, departure_seconds)
```

Values are variable-length encoded and compressed with zlib. Stop order is implicit in the sequence, and times are seconds after midnight.

Each `station_trips.trip_keys` blob contains sorted trip keys. They are stored as delta-encoded variable-length integers and then compressed with zlib.

To search offline, look up the origin station in `station_trips`, check each candidate trip’s service bitmap for the requested date, decode its schedule, and check whether the destination station appears later in that schedule.
