"""ThriftyFileBasedCache: no directory listing on every write, but the cache is still trimmed and still correct."""
import shutil
import tempfile
from unittest import mock

from django.test import SimpleTestCase, override_settings

from internest_core.cache_backends import ThriftyFileBasedCache


class ThriftyFileBasedCacheTests(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cache = ThriftyFileBasedCache(self.dir, {"OPTIONS": {"MAX_ENTRIES": 10, "CULL_FREQUENCY": 2}})

    def test_most_writes_do_not_list_the_directory(self):
        with mock.patch.object(ThriftyFileBasedCache, "_list_cache_files", wraps=self.cache._list_cache_files) as listing, \
                mock.patch("internest_core.cache_backends.random.random", return_value=0.5):
            for i in range(30):
                self.cache.set(f"k{i}", i)
        self.assertEqual(listing.call_count, 0)  # Django's backend would have listed the folder 30 times

    def test_it_still_trims_when_the_check_runs(self):
        with mock.patch("internest_core.cache_backends.random.random", return_value=0.5):
            for i in range(30):
                self.cache.set(f"k{i}", i)
        self.assertEqual(len(self.cache._list_cache_files()), 30)  # over MAX_ENTRIES until a check runs
        with mock.patch("internest_core.cache_backends.random.random", return_value=0.0):
            self.cache.set("trigger", 1)
        self.assertLessEqual(len(self.cache._list_cache_files()), 20)

    def test_values_and_incr_behave_like_the_file_cache(self):
        self.cache.set("n", 1, 60)
        self.assertEqual(self.cache.incr("n"), 2)
        self.assertEqual(self.cache.get("n"), 2)
        self.assertTrue(self.cache.add("fresh", "x"))
        self.assertFalse(self.cache.add("fresh", "y"))

    def test_rate_limiter_windows_work_on_it(self):
        from internest_core import ratelimit

        caches = {"default": {"BACKEND": "internest_core.cache_backends.ThriftyFileBasedCache", "LOCATION": self.dir}}
        with override_settings(CACHES=caches):
            from django.core.cache import caches as registry
            registry._settings = None
            results = [ratelimit.hit("rl:test", 3, 60) for _ in range(4)]
        self.assertEqual(results, [True, True, True, False])


class BusyCacheFileTests(SimpleTestCase):
    """Windows refuses to delete or replace a file another thread holds open: that must read as a miss, not a 500."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cache = ThriftyFileBasedCache(self.dir, {})

    def test_a_busy_expired_entry_is_a_miss(self):
        self.cache.set("page", "html", 60)
        with mock.patch("internest_core.cache_backends.FileBasedCache._is_expired",
                        side_effect=PermissionError(32, "in use")):
            self.assertEqual(self.cache.get("page", "fallback"), "fallback")
            self.assertFalse(self.cache.has_key("page"))
            self.assertFalse(self.cache.touch("page"))

    def test_a_file_that_cannot_be_opened_is_a_miss(self):
        self.cache.set("page", "html", 60)
        with mock.patch("builtins.open", side_effect=PermissionError(13, "denied")):
            self.assertIsNone(self.cache.get("page"))

    def test_a_write_that_loses_the_race_is_dropped(self):
        with mock.patch("internest_core.cache_backends.FileBasedCache.set", side_effect=PermissionError(32, "in use")):
            self.cache.set("k", 1)  # no exception
        self.assertIsNone(self.cache.get("k"))
        self.cache.set("k", 2)
        self.assertEqual(self.cache.get("k"), 2)

    def test_a_delete_of_a_busy_file_reports_false(self):
        self.cache.set("k", 1)
        with mock.patch("django.core.cache.backends.filebased.os.remove", side_effect=PermissionError(32, "in use")):
            self.assertFalse(self.cache.delete("k"))
        self.assertEqual(self.cache.get("k"), 1)

    def test_other_os_errors_still_surface(self):
        with mock.patch("internest_core.cache_backends.FileBasedCache.set", side_effect=OSError(28, "disk full")):
            with self.assertRaises(OSError):
                self.cache.set("k", 1)
