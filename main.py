#!/usr/bin/env python3
"""Build a compact offline train timetable database from a GTFS static feed."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import struct
import sys
import zlib
from array import array
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Sequence


SOURCE = {
    "agency": "agency.txt", "routes": "routes.txt", "trips": "trips.txt",
    "stop_times": "stop_times.txt", "stops": "stops.txt", "calendar": "calendar.txt",
    "calendar_dates": "calendar_dates.txt", "feed_info": "feed_info.txt",
}

# Configure the operators to keep here.
INCLUDED_AGENCY_IDS: set[str] = {"11", "65", "33", "72"}
# GTFS extended route types 100-117 are rail services.
INCLUDED_ROUTE_TYPES: set[int] = set(range(100, 118))


def rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        yield from csv.DictReader(source)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def time_to_seconds(value: str | None) -> int:
    if not value:
        return 0
    hours, minutes, seconds = (int(part) for part in value.split(":", 2))
    return hours * 3600 + minutes * 60 + seconds


def date_to_ordinal(value: str) -> int:
    return date(int(value[:4]), int(value[4:6]), int(value[6:])).toordinal()


def put_varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def get_varints(data: bytes) -> Iterator[int]:
    value = 0
    shift = 0
    for byte in data:
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            yield value
            value = 0
            shift = 0
        else:
            shift += 7


def encode_schedule(stops: list[tuple[int, int, int]]) -> bytes:
    """Encode (stop_key, arrival_seconds, departure_seconds) in trip order."""
    raw = bytearray()
    for stop_key, arrival, departure in stops:
        raw.extend(put_varint(stop_key))
        raw.extend(put_varint(arrival + 1))
        raw.extend(put_varint(departure + 1))
    return zlib.compress(bytes(raw), level=9)


def decode_schedule(blob: bytes) -> list[tuple[int, int, int]]:
    values = list(get_varints(zlib.decompress(blob)))
    return [(values[i], values[i + 1] - 1, values[i + 2] - 1) for i in range(0, len(values), 3)]


def encode_trip_keys(keys: array) -> bytes:
    previous = 0
    raw = bytearray()
    for key in sorted(set(keys)):
        raw.extend(put_varint(key - previous))
        previous = key
    return zlib.compress(bytes(raw), level=9)


def decode_trip_keys(blob: bytes) -> list[int]:
    previous = 0
    result = []
    for delta in get_varints(zlib.decompress(blob)):
        previous += delta
        result.append(previous)
    return result


def find_empty_columns(source: Path) -> dict[str, set[str]]:
    """Automatically report empty source columns; output keeps only useful fields."""
    empty_by_file: dict[str, set[str]] = {}
    for path in sorted(source.glob("*.txt")):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            used = {field: False for field in fields}
            for row in reader:
                for field in fields:
                    if (row.get(field) or "").strip():
                        used[field] = True
        empty = [field for field, present in used.items() if not present]
        empty_by_file[path.name] = set(empty)
        if empty:
            print(f"[columns] Dropped empty {path.name} columns: {', '.join(empty)}", flush=True)
    return empty_by_file


def make_schema(connection: sqlite3.Connection, has_feed_info: bool, stop_columns: Sequence[str]) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;

        CREATE TABLE agency (
            agency_key INTEGER PRIMARY KEY,
            agency_id TEXT NOT NULL UNIQUE,
            agency_name TEXT NOT NULL
        );
        CREATE TABLE route (
            route_key INTEGER PRIMARY KEY,
            route_id TEXT NOT NULL UNIQUE,
            agency_key INTEGER NOT NULL,
            short_name TEXT,
            route_type INTEGER NOT NULL
        );
        CREATE TABLE service (
            service_key INTEGER PRIMARY KEY,
            service_id TEXT NOT NULL UNIQUE,
            available_days BLOB NOT NULL
        );
        CREATE TABLE trip (
            trip_key INTEGER PRIMARY KEY,
            route_key INTEGER NOT NULL,
            service_key INTEGER NOT NULL,
            headsign TEXT,
            short_name TEXT,
            direction_id TEXT
        );
        CREATE TABLE stop (
            stop_key INTEGER PRIMARY KEY,
            stop_id TEXT NOT NULL UNIQUE,
            stop_name TEXT NOT NULL
        );
        CREATE TABLE trip_schedule (
            trip_key INTEGER PRIMARY KEY,
            schedule BLOB NOT NULL
        );
        CREATE TABLE station_trips (
            stop_key INTEGER PRIMARY KEY,
            trip_keys BLOB NOT NULL
        );
        """
    )
    stop_extra_types = {
        "stop_lat": "REAL", "stop_lon": "REAL",
        "parent_station": "TEXT", "platform_code": "TEXT",
    }
    extra_columns = [column for column in stop_columns if column not in {"stop_id", "stop_name"}]
    for column in extra_columns:
        connection.execute(f"ALTER TABLE stop ADD COLUMN {column} {stop_extra_types[column]}")
    if has_feed_info:
        connection.execute("CREATE TABLE feed_info (feed_start_date TEXT NOT NULL, feed_end_date TEXT NOT NULL)")


def insert_batches(connection: sqlite3.Connection, statement: str, values: Iterable[Sequence[object]], batch_size: int = 10_000) -> None:
    batch: list[Sequence[object]] = []
    for value in values:
        batch.append(value)
        if len(batch) == batch_size:
            connection.executemany(statement, batch)
            batch.clear()
    if batch:
        connection.executemany(statement, batch)


def validate_database(database: Path) -> list[str]:
    required = {"agency", "route", "service", "trip", "stop", "trip_schedule", "station_trips"}
    errors: list[str] = []
    connection = sqlite3.connect(database)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = required - tables
        if missing:
            errors.append(f"missing tables: {', '.join(sorted(missing))}")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            errors.append("SQLite integrity check failed")
        if not missing:
            counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in required}
            for table, count in counts.items():
                if count == 0:
                    errors.append(f"{table} is empty")
            mismatch = connection.execute("SELECT COUNT(*) FROM trip t LEFT JOIN trip_schedule s ON s.trip_key=t.trip_key WHERE s.trip_key IS NULL").fetchone()[0]
            if mismatch:
                errors.append(f"{mismatch} trips have no schedule")
        return errors
    finally:
        connection.close()


def check_database(database: Path) -> int:
    print(f"Checking {database} ...", flush=True)
    if not database.exists():
        print(f"ERROR: database does not exist: {database}", file=sys.stderr)
        return 1
    errors = validate_database(database)
    if errors:
        print("FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("OK: database is structurally valid and contains usable core data.")
    print(f"Database size: {format_size(database.stat().st_size)}")
    return 0


def build_database(source: Path, output: Path, included_ids: set[str] | None = None) -> None:
    empty_columns = find_empty_columns(source)
    stop_columns = tuple(
        column for column in ("stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station", "platform_code")
        if column not in empty_columns.get(SOURCE["stops"], set())
    )
    allowed = INCLUDED_AGENCY_IDS if included_ids is None else included_ids
    agencies = {row["agency_id"]: row for row in rows(source / SOURCE["agency"]) if row["agency_id"] in allowed}
    print(f"[1/8] Selected {len(agencies)} agencies.", flush=True)

    route_keys: dict[str, int] = {}
    routes: list[tuple[int, str, int, str | None, int]] = []
    agency_keys = {agency_id: index for index, agency_id in enumerate(agencies, 1)}
    for row in rows(source / SOURCE["routes"]):
        route_type = int(row["route_type"]) if row.get("route_type") else -1
        if row["agency_id"] not in agencies or route_type not in INCLUDED_ROUTE_TYPES:
            continue
        key = len(routes) + 1
        route_keys[row["route_id"]] = key
        routes.append((key, row["route_id"], agency_keys[row["agency_id"]], row.get("route_short_name") or None, route_type))
    print(f"[2/8] Kept {len(routes)} rail routes. Reading trips ...", flush=True)

    service_keys: dict[str, int] = {}
    trip_keys: dict[str, int] = {}
    trips: list[tuple[int, int, int, str | None, str | None, str | None]] = []
    for row in rows(source / SOURCE["trips"]):
        route_key = route_keys.get(row["route_id"])
        if route_key is None:
            continue
        service_key = service_keys.setdefault(row["service_id"], len(service_keys) + 1)
        trip_key = len(trips) + 1
        trip_keys[row["trip_id"]] = trip_key
        trips.append((trip_key, route_key, service_key, row.get("trip_headsign") or None, row.get("trip_short_name") or None, row.get("direction_id") or None))
    print(f"[3/8] Kept {len(trips):,} trips. Building service availability ...", flush=True)

    feed_info_path = source / SOURCE["feed_info"]
    feed_row = next(rows(feed_info_path), None) if feed_info_path.exists() else None
    if feed_row and feed_row.get("feed_start_date") and feed_row.get("feed_end_date"):
        start = date_to_ordinal(feed_row["feed_start_date"])
        end = date_to_ordinal(feed_row["feed_end_date"])
    else:
        calendar_rows = [row for row in rows(source / SOURCE["calendar"]) if row["service_id"] in service_keys]
        start = min(date_to_ordinal(row["start_date"]) for row in calendar_rows)
        end = max(date_to_ordinal(row["end_date"]) for row in calendar_rows)
    day_count = end - start + 1
    availability = {service_id: bytearray((day_count + 7) // 8) for service_id in service_keys}
    for row in rows(source / SOURCE["calendar"]):
        bitmap = availability.get(row["service_id"])
        if bitmap is None:
            continue
        first = max(start, date_to_ordinal(row["start_date"]))
        last = min(end, date_to_ordinal(row["end_date"]))
        weekdays = [row["monday"], row["tuesday"], row["wednesday"], row["thursday"], row["friday"], row["saturday"], row["sunday"]]
        for ordinal in range(first, last + 1):
            if weekdays[date.fromordinal(ordinal).weekday()] == "1":
                offset = ordinal - start
                bitmap[offset // 8] |= 1 << (offset % 8)

    override_count = 0
    scanned_dates = 0
    date_offsets: dict[str, int] = {}
    for row in rows(source / SOURCE["calendar_dates"]):
        scanned_dates += 1
        if scanned_dates % 1_000_000 == 0:
            print(f"[4/8] Scanned {scanned_dates:,} calendar dates ({override_count:,} kept) ...", flush=True)
        bitmap = availability.get(row["service_id"])
        if bitmap is None:
            continue
        offset = date_offsets.setdefault(row["date"], date_to_ordinal(row["date"]) - start)
        if 0 <= offset < day_count:
            if row["exception_type"] == "1":
                bitmap[offset // 8] |= 1 << (offset % 8)
            else:
                bitmap[offset // 8] &= ~(1 << (offset % 8))
            override_count += 1

    if output.exists():
        output.unlink()
    connection = sqlite3.connect(output)
    try:
        make_schema(connection, bool(feed_row), stop_columns)
        insert_batches(connection, "INSERT INTO agency VALUES (?, ?, ?)", ((key, row["agency_id"], row["agency_name"]) for key, row in zip(agency_keys.values(), agencies.values())))
        insert_batches(connection, "INSERT INTO route VALUES (?, ?, ?, ?, ?)", routes)
        insert_batches(connection, "INSERT INTO trip VALUES (?, ?, ?, ?, ?, ?)", trips)
        insert_batches(connection, "INSERT INTO service VALUES (?, ?, ?)", ((key, service_id, bytes(availability[service_id])) for service_id, key in service_keys.items()))
        if feed_row:
            connection.execute("INSERT INTO feed_info VALUES (?, ?)", (feed_row["feed_start_date"], feed_row["feed_end_date"]))

        stop_keys: dict[str, int] = {}
        schedules: dict[int, list[tuple[int, int, int]]] = {}
        station_trips: dict[int, array] = {}
        scanned = 0
        kept = 0
        for row in rows(source / SOURCE["stop_times"]):
            scanned += 1
            if scanned % 1_000_000 == 0:
                print(f"[5/8] Scanned {scanned:,} stop-time rows ({kept:,} kept) ...", flush=True)
            trip_key = trip_keys.get(row["trip_id"])
            if trip_key is None:
                continue
            stop_key = stop_keys.setdefault(row["stop_id"], len(stop_keys) + 1)
            schedules.setdefault(trip_key, []).append((stop_key, time_to_seconds(row.get("arrival_time")), time_to_seconds(row.get("departure_time"))))
            station_trips.setdefault(stop_key, array("I")).append(trip_key)
            kept += 1
        print(f"[5/8] Kept {kept:,} stop times; compressing schedules ...", flush=True)
        insert_batches(connection, "INSERT INTO trip_schedule VALUES (?, ?)", ((key, encode_schedule(value)) for key, value in schedules.items()))

        stops = []
        for row in rows(source / SOURCE["stops"]):
            key = stop_keys.get(row["stop_id"])
            if key is not None:
                values = {
                    "stop_id": row["stop_id"],
                    "stop_name": row["stop_name"],
                    "stop_lat": float(row["stop_lat"]) if row.get("stop_lat") else None,
                    "stop_lon": float(row["stop_lon"]) if row.get("stop_lon") else None,
                    "parent_station": row.get("parent_station") or None,
                    "platform_code": row.get("platform_code") or None,
                }
                stops.append((key, *(values[column] for column in stop_columns)))
        stop_insert_columns = ("stop_key", *stop_columns)
        stop_placeholders = ", ".join("?" for _ in stop_insert_columns)
        insert_batches(
            connection,
            f"INSERT INTO stop ({', '.join(stop_insert_columns)}) VALUES ({stop_placeholders})",
            stops,
        )
        print(f"[6/8] Wrote {len(stops):,} stops and {len(schedules):,} schedules ...", flush=True)
        insert_batches(connection, "INSERT INTO station_trips VALUES (?, ?)", ((key, encode_trip_keys(value)) for key, value in station_trips.items()))
        print("[7/8] Wrote compact station indexes. Committing ...", flush=True)
        connection.commit()
    finally:
        connection.close()

    errors = validate_database(output)
    if errors:
        print("Validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise RuntimeError("generated database did not pass validation")
    print(f"Wrote {output} ({len(agencies)} agencies, {len(routes)} routes, {len(trips):,} trips, {kept:,} stops times, {override_count:,} calendar overrides).", flush=True)
    print("Validation OK.", flush=True)
    print("[8/8] Database size: " + format_size(output.stat().st_size), flush=True)


def list_agencies(source: Path) -> None:
    for row in sorted(rows(source / SOURCE["agency"]), key=lambda value: (value["agency_id"], value["agency_name"])):
        print(f"{row['agency_id']}\t{row['agency_name']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list-agencies")
    listing.add_argument("--source", type=Path, default=Path("source"))
    builder = subparsers.add_parser("build")
    builder.add_argument("--source", type=Path, default=Path("source"))
    builder.add_argument("--output", type=Path, default=Path("gtfs.sqlite"))
    checker = subparsers.add_parser("check")
    checker.add_argument("database", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "list-agencies":
        list_agencies(args.source)
    elif args.command == "check":
        raise SystemExit(check_database(args.database))
    else:
        build_database(args.source, args.output)


if __name__ == "__main__":
    main()
