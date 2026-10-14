"""Site-wide responsive guard rails are embedded in every page (base layout and the standalone universities page)."""
from django.test import TestCase
from django.urls import reverse


class ResponsiveLayoutTests(TestCase):
    def _assert_guard_rails(self, page):
        self.assertContains(page, 'id="layout-core"', count=1)
        for rule in ("overflow-x: clip", "100dvh", "safe-area-inset-left", "--container: 1400px", "calc(100vw - 24px)"):
            self.assertContains(page, rule)

    def test_base_layout_pages(self):
        for name in ("landing", "privacy", "login"):
            with self.subTest(page=name):
                self._assert_guard_rails(self.client.get(reverse(name)))

    def test_standalone_universities_page(self):
        page = self.client.get(reverse("university_landing"))
        self._assert_guard_rails(page)
        self.assertContains(page, "safe-area-inset-bottom")
