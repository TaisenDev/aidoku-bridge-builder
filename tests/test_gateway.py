import unittest
import io
from unittest.mock import patch
from PIL import Image

from builder import MAX_GLOBAL_INSTALLS_PER_HOUR, _allow_install_request, ikisaku_catalog_data
from builder import _INSTALL_REQUESTS
from gateway import _document_shape_is_allowed, encode_page_tiles


class GatewayDocumentPolicyTests(unittest.TestCase):
    def test_allows_bridge_reads(self):
        query = '''
            mutation {
              bridgeFetchSourceManga(input: {source: "123", type: SEARCH, page: 1}) {
                mangas { id title realUrl }
                hasNextPage
              }
            }
        '''
        self.assertTrue(_document_shape_is_allowed(query))

    def test_allows_filter_metadata_query(self):
        query = '''
            query {
              source(id: "123") {
                filters {
                  __typename
                  ... on CheckBoxFilter { name cbDefault: default }
                  ... on SelectFilter { name selDefault: default values }
                  ... on TextFilter { name }
                }
              }
            }
        '''
        self.assertTrue(_document_shape_is_allowed(query))

    def test_allows_every_source_filter_shape_and_nested_groups(self):
        query = '''
            query {
              source(id: "123") {
                filters {
                  __typename
                  ... on HeaderFilter { name }
                  ... on SeparatorFilter { name }
                  ... on CheckBoxFilter { name cbDefault: default }
                  ... on SelectFilter { name selDefault: default values }
                  ... on TextFilter { name textDefault: default }
                  ... on TriStateFilter { name triDefault: default }
                  ... on SortFilter { name sortDefault: default { index ascending } values }
                  ... on GroupFilter {
                    name
                    groupFilters: filters {
                      __typename
                      ... on TriStateFilter { name triDefault: default }
                      ... on GroupFilter {
                        name
                        groupFilters: filters {
                          __typename
                          ... on TextFilter { name textDefault: default }
                        }
                      }
                    }
                  }
                }
              }
            }
        '''
        self.assertTrue(_document_shape_is_allowed(query))

    def test_rejects_persistent_manga_table_traversal(self):
        query = 'query { source(id: "123") { mangas { nodes { title } } } }'
        self.assertFalse(_document_shape_is_allowed(query))

    def test_rejects_unapproved_mutations(self):
        query = 'mutation { addMangaFromUrl(input: {url: "https://example.org/manga/1"}) { status } }'
        self.assertFalse(_document_shape_is_allowed(query))

    def test_rejects_aliases_and_introspection(self):
        self.assertFalse(_document_shape_is_allowed('mutation { x: bridgeResolveUrl(url: "https://example.org") { status } }'))
        self.assertFalse(_document_shape_is_allowed('query { source(id: "123") { __schema { types { name } } } }'))


class InstallAbuseLimitTests(unittest.TestCase):
    def test_global_install_limit_applies_across_many_client_addresses(self):
        with patch.dict(_INSTALL_REQUESTS, {}, clear=True):
            for index in range(MAX_GLOBAL_INSTALLS_PER_HOUR):
                self.assertTrue(_allow_install_request(f"192.0.2.{index + 1}"))
            self.assertFalse(_allow_install_request("198.51.100.1"))

    def test_per_client_install_limit_remains_in_place(self):
        with patch.dict(_INSTALL_REQUESTS, {}, clear=True):
            for _ in range(6):
                self.assertTrue(_allow_install_request("192.0.2.1"))
            self.assertFalse(_allow_install_request("192.0.2.1"))


class IkisakuCatalogTests(unittest.TestCase):
    def test_catalog_lists_uninstalled_packages_and_installed_source_metadata(self):
        with patch("builder.LANGUAGES", ["all"]):
            result = ikisaku_catalog_data(
                [
                    {"pkgName": "es.example", "name": "Ejemplo", "lang": "es", "versionName": "1.2", "versionCode": 12, "isInstalled": False},
                    {"pkgName": "en.example", "name": "Example", "lang": "en", "isInstalled": True},
                ],
                [
                    {"id": "123", "name": "example", "displayName": "Example Source", "lang": "en", "extension": {"pkgName": "en.example"}},
                ],
                lambda ext, extension_id: f"https://catalog.example/icons/{extension_id}.png",
            )
        self.assertEqual(result["formatVersion"], 1)
        self.assertEqual([item["package"] for item in result["extensions"]], ["en.example", "es.example"])
        self.assertFalse(result["extensions"][1]["isInstalled"])
        self.assertEqual(result["extensions"][0]["sources"][0]["sourceId"], "123")
        self.assertNotIn("token", str(result).lower())


class PageImageOptimizationTests(unittest.TestCase):
    def test_downscales_and_splits_long_pages_without_disk_state(self):
        source = Image.new("RGB", (1600, 3500), "white")
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        width, height, tiles = encode_page_tiles(buffer.getvalue())
        self.assertEqual((width, height), (896, 1960))
        self.assertEqual(len(tiles), 2)
        self.assertEqual(Image.open(io.BytesIO(tiles[0])).size, (896, 1600))
        self.assertEqual(Image.open(io.BytesIO(tiles[1])).size, (896, 360))

    def test_rejects_unbounded_extreme_aspect_ratio(self):
        source = Image.new("RGB", (10, 120000), "white")
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        with self.assertRaises(ValueError):
            encode_page_tiles(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
