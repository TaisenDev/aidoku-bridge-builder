import unittest

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


if __name__ == "__main__":
    unittest.main()
