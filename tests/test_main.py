import tempfile
import unittest
from pathlib import Path

import main


class BuildDatabaseTests(unittest.TestCase):
    def write_feed(self, directory: Path) -> None:
        files = {
            "agency.txt": "agency_id,agency_name\nkeep,Kept Operator\ndrop,Dropped Operator\n",
            "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nr1,keep,R1,,106\nr2,drop,R2,,106\n",
            "trips.txt": "route_id,service_id,trip_id,trip_headsign,trip_short_name,direction_id\nr1,s1,t1,Central,101,0\nr2,s2,t2,Ignored,202,0\n",
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon,parent_station,platform_code\na,Alpha,46.1,7.1,,1\nb,Beta,46.2,7.2,,2\nunused,Unused,0,0,,\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,08:00:00,08:01:00,a,1\nt1,09:00:00,09:01:00,b,2\nt2,10:00:00,10:01:00,a,1\n",
            "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\ns1,1,1,1,1,1,0,0,20260101,20261231\n",
            "calendar_dates.txt": "service_id,date,exception_type\ns1,20260102,2\ns2,20260103,1\n",
            "frequencies.txt": "trip_id,start_time,end_time,headway_secs,exact_times\nt1,08:00:00,09:00:00,600,1\nt2,10:00:00,11:00:00,600,0\n",
            "feed_info.txt": "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version\nTest,https://example.test,en,20260101,20261231,1\n",
        }
        for name, content in files.items():
            (directory / name).write_text(content, encoding="utf-8")

    def test_build_filters_agency_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            self.write_feed(source)
            database = Path(temp) / "test.sqlite"
            main.build_database(source, database, {"keep"})

            connection = main.sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM agency").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM trip").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM service").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM trip_schedule").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM station_trips").fetchone()[0], 2)
                schedule = connection.execute("SELECT schedule FROM trip_schedule").fetchone()[0]
                self.assertEqual(main.decode_schedule(schedule), [(1, 28800, 28860), (2, 32400, 32460)])
                station_trips = connection.execute("SELECT trip_keys FROM station_trips WHERE stop_key = 1").fetchone()[0]
                self.assertEqual(main.decode_trip_keys(station_trips), [1])
            finally:
                connection.close()
            self.assertEqual(main.validate_database(database), [])


if __name__ == "__main__":
    unittest.main()
