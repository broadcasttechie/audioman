"""Run on the container: python3 -m unittest discover -s tests"""
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import requests

from app import geo
from config import Config
from jobs import geocode, photon, providers
from jobs.retry import ServiceRejected, ServiceUnavailable

# Real answers from the user's Photon 1.3.0 (properties only), captured 2026-09-21.
STATION = {"osm_key": "amenity", "osm_value": "bicycle_parking", "type": "house", "name": "Railway Station Platform 1", "street": "Station Road",
           "district": "Shelton", "city": "Stoke-on-Trent", "state": "England", "country": "United Kingdom", "postcode": "ST4 2AA", "countrycode": "GB"}
WOODLAND = {"osm_key": "place", "osm_value": "postcode", "type": "other", "name": "DY14 9UQ", "district": "Rock", "city": "Wyre Forest",
            "county": "Worcestershire", "state": "England", "country": "United Kingdom", "countrycode": "GB"}
STREET = {"osm_key": "highway", "osm_value": "trunk", "type": "street", "name": "City Road", "district": "Shelton", "city": "Stoke-on-Trent",
          "state": "England", "country": "United Kingdom", "postcode": "ST4 1EZ", "countrycode": "GB"}
LONDON = {"osm_key": "place", "osm_value": "city", "type": "city", "name": "London", "state": "England", "country": "United Kingdom", "countrycode": "GB"}


class FormatPlaceTests(unittest.TestCase):
    def test_real_answers(self):
        self.assertEqual(photon.format_place(STATION), "Railway Station Platform 1, Station Road, Shelton, Stoke-on-Trent")
        self.assertEqual(photon.format_place(STREET), "City Road, Shelton, Stoke-on-Trent")
        self.assertEqual(photon.format_place(LONDON), "London, England")

    def test_a_postcode_is_never_the_name(self):
        self.assertEqual(photon.format_place(WOODLAND), "Rock, Wyre Forest, Worcestershire")
        self.assertNotIn("DY14", photon.format_place(WOODLAND))

    def test_country_only_when_abroad(self):
        paris = {"osm_key": "tourism", "name": "Tour Eiffel", "city": "Paris", "state": "Ile-de-France", "country": "France", "countrycode": "FR"}
        self.assertEqual(photon.format_place(paris, home_country="GB"), "Tour Eiffel, Paris, Ile-de-France, France")
        self.assertEqual(photon.format_place(paris, home_country="FR"), "Tour Eiffel, Paris, Ile-de-France")

    def test_near_prefix(self):
        self.assertEqual(photon.format_place(WOODLAND, near=True), "near Rock, Wyre Forest, Worcestershire")

    def test_no_repeats_and_at_most_four_parts(self):
        p = {"osm_key": "place", "name": "Stoke-on-Trent", "city": "Stoke-on-Trent", "district": "stoke-on-trent", "county": "Staffordshire", "state": "England"}
        self.assertEqual(photon.format_place(p), "Stoke-on-Trent, Staffordshire")
        many = {"name": "A", "street": "B", "district": "C", "locality": "D", "city": "E", "county": "F"}
        self.assertEqual(photon.format_place(many).count(","), 3)

    def test_house_without_a_name_uses_number_and_street(self):
        self.assertTrue(photon.format_place({"housenumber": "12", "street": "High Street", "city": "Leek"}).startswith("12 High Street"))

    def test_nothing_usable_is_none(self):
        self.assertIsNone(photon.format_place({}))
        self.assertIsNone(photon.format_place({"osm_key": "place", "osm_value": "postcode", "name": "ST4 2AA"}))


def response(features):
    r = mock.Mock()
    r.json.return_value = {"features": features}
    r.raise_for_status = lambda: None
    return r


class ReverseTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("jobs.photon.get_config", return_value="http://photon.test:2322")
        p.start()
        self.addCleanup(p.stop)

    def test_success_builds_label_info_and_asks_for_one_english_result(self):
        with mock.patch("jobs.photon.requests.get", return_value=response([{"properties": STATION}])) as get:
            out = photon.reverse(53.0082, -2.1812)
        self.assertEqual(out["label"], "Railway Station Platform 1, Station Road, Shelton, Stoke-on-Trent")
        self.assertFalse(out["near"])
        self.assertEqual(out["info"]["city"], "Stoke-on-Trent")
        _, kwargs = get.call_args
        self.assertEqual(get.call_args[0][0], "http://photon.test:2322/reverse")
        self.assertEqual((kwargs["params"]["limit"], kwargs["params"]["lang"]), (1, "en"))
        self.assertNotIn("radius", kwargs["params"])

    def test_nothing_nearby_widens_the_search_and_says_near(self):
        answers = [response([]), response([{"properties": WOODLAND}])]
        with mock.patch("jobs.photon.requests.get", side_effect=answers) as get:
            out = photon.reverse(52.4, -2.4)
        self.assertTrue(out["near"] and out["label"].startswith("near "))
        self.assertEqual(get.call_args_list[1][1]["params"]["radius"], Config.PLACE_FALLBACK_RADIUS_KM)

    def test_truly_nothing_is_none(self):
        with mock.patch("jobs.photon.requests.get", return_value=response([])):
            self.assertIsNone(photon.reverse(30, -40))

    def test_unconfigured(self):
        with mock.patch("jobs.photon.get_config", return_value=""):
            self.assertEqual(photon.reverse(1, 2), "unconfigured")

    def test_a_refusal_is_a_clear_rejection_and_an_outage_is_unavailable(self):
        bad = requests.HTTPError("400", response=mock.Mock(status_code=400, url="http://photon.test:2322/reverse?lat=1&lon=2", text="<title>Nope</title>"))
        with mock.patch("jobs.photon.requests.get", side_effect=bad):
            with self.assertRaises(ServiceRejected) as ctx:
                photon.reverse(1, 2, max_attempts=1)
        self.assertIn("400", str(ctx.exception))
        with mock.patch("jobs.photon.requests.get", side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(ServiceUnavailable):
                photon.reverse(1, 2, max_attempts=1)


class GeoTests(unittest.TestCase):
    def test_distance(self):
        self.assertEqual(geo.distance_m(53, -2, 53, -2), 0)
        self.assertAlmostEqual(geo.distance_m(0, 0, 1, 0) / 1000, 111.19, delta=0.1)
        self.assertAlmostEqual(geo.distance_m(53.0082, -2.1812, 53.0079, -2.1804), 63, delta=5)   # the station pin vs the station: tens of metres

    def test_moved_needs_more_than_the_threshold(self):
        self.assertFalse(geocode.place_moved(53.0082, -2.1812, 53.00822, -2.18121))     # GPS jitter: same name
        self.assertTrue(geocode.place_moved(53.0082, -2.1812, 53.0182, -2.1812))        # about 1 km: rename
        self.assertTrue(geocode.place_moved(None, None, 53, -2))                         # a first location always needs a name

    def test_manual_names_and_resets(self):
        loc = NS(place_name="Rock", place_source="photon", place_info={"a": 1}, place_checked_at="then")
        self.assertFalse(geocode.set_manual_place(loc, "  Jacob's garden  "))
        self.assertEqual((loc.place_name, loc.place_source, loc.place_info), ("Jacob's garden", "manual", None))
        self.assertIsNotNone(loc.place_checked_at)
        self.assertTrue(geocode.set_manual_place(loc, "   "), "clearing asks for a lookup")
        self.assertEqual((loc.place_name, loc.place_source, loc.place_checked_at), (None, None, None))

    def test_a_looked_up_name_is_stored_and_nothing_found_is_remembered(self):
        loc = NS(place_name=None, place_source=None, place_info=None, place_checked_at=None)
        geocode.apply_lookup(loc, {"label": "London, England", "info": {"near": False}})
        self.assertEqual((loc.place_name, loc.place_source), ("London, England", "photon"))
        geocode.apply_lookup(loc, None)
        self.assertIsNone(loc.place_name)
        self.assertIsNotNone(loc.place_checked_at, "an empty answer is an answer: it must not be asked again")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        # Provider names normally come from get_config (DB override, else Config); here just Config.
        p = mock.patch("jobs.providers.get_config", side_effect=lambda key: getattr(Config, key))
        p.start()
        self.addCleanup(p.stop)

    def test_known_providers_resolve_lazily_to_the_right_functions(self):
        from jobs import dawarich, immich
        self.assertIs(providers.get("location"), dawarich.fetch_track_and_pin)
        self.assertIs(providers.get("geocoder"), photon.reverse)
        self.assertIs(providers.get("photos"), immich.fetch_photos_for_recording)

    def test_none_switches_a_feature_off(self):
        for off in ("none", "", "OFF", " disabled "):
            with mock.patch("jobs.providers.get_config", return_value=off):
                self.assertIsNone(providers.get("geocoder"), repr(off))

    def test_an_unknown_name_is_reported_with_the_alternatives(self):
        with mock.patch("jobs.providers.get_config", return_value="nominatim"):
            with self.assertRaises(providers.UnknownProvider) as ctx:
                providers.get("geocoder")
        msg = str(ctx.exception)
        self.assertIn("GEOCODER_PROVIDER", msg)
        self.assertIn("photon", msg)
        self.assertIn("none", msg)

    def test_describe_lists_what_is_active_and_available(self):
        d = providers.describe()
        self.assertEqual(set(d), {"location", "geocoder", "photos"})
        self.assertEqual(d["geocoder"]["available"], ["photon"])
        self.assertEqual(d["location"]["setting"], "LOCATION_PROVIDER")

    def test_every_registered_provider_really_exists(self):
        import importlib
        for kind, names in providers.REGISTRY.items():
            for name, spec in names.items():
                module, func = spec.split(":")
                self.assertTrue(callable(getattr(importlib.import_module(module), func)), f"{kind}/{name}")


if __name__ == "__main__":
    unittest.main()
