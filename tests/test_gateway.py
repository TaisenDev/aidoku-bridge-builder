import unittest
from unittest.mock import patch

from builder import MAX_GLOBAL_INSTALLS_PER_HOUR, _allow_install_request
from builder import _INSTALL_REQUESTS
from gateway import _document_shape_is_allowed


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


if __name__ == "__main__":
    unittest.main()
