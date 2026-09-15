# SPDX-License-Identifier: Apache-2.0
"""Tests for reckon/install_check.py: RECKON-1.1-SPEC.md decision 1."""

import sys
import unittest
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reckon import install_check  # noqa: E402


class FakeDistribution:
    def __init__(self, direct_url_text=None):
        self._direct_url_text = direct_url_text

    def read_text(self, name):
        if name == "direct_url.json":
            return self._direct_url_text
        return None


class TestRequireLinkedInstall(unittest.TestCase):
    def test_an_editable_install_passes_and_returns_the_distribution(self):
        dist = FakeDistribution('{"url": "file:///wherever", "dir_info": {"editable": true}}')
        result = install_check.require_linked_install(distribution_fn=lambda: dist)
        self.assertIs(result, dist)

    def test_not_installed_at_all_refuses_and_names_the_install_line(self):
        def boom():
            raise metadata.PackageNotFoundError()
        with self.assertRaises(install_check.NotLinkedInstall) as ctx:
            install_check.require_linked_install(distribution_fn=boom)
        self.assertIn(install_check.INSTALL_LINE, str(ctx.exception))

    def test_a_regular_non_editable_install_refuses(self):
        dist = FakeDistribution('{"url": "file:///wherever"}')
        with self.assertRaises(install_check.NotLinkedInstall) as ctx:
            install_check.require_linked_install(distribution_fn=lambda: dist)
        self.assertIn(install_check.INSTALL_LINE, str(ctx.exception))

    def test_an_install_with_no_direct_url_metadata_refuses(self):
        dist = FakeDistribution(direct_url_text=None)
        with self.assertRaises(install_check.NotLinkedInstall) as ctx:
            install_check.require_linked_install(distribution_fn=lambda: dist)
        self.assertIn(install_check.INSTALL_LINE, str(ctx.exception))

    def test_unparseable_direct_url_metadata_refuses_rather_than_exploding(self):
        dist = FakeDistribution(direct_url_text="not json")
        with self.assertRaises(install_check.NotLinkedInstall):
            install_check.require_linked_install(distribution_fn=lambda: dist)


if __name__ == "__main__":
    unittest.main()
