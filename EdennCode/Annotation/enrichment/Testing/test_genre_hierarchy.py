"""
Tests for genre_hierarchy.py and the hierarchical fields on ExtractedTaxonomy.
"""
from __future__ import annotations

import unittest

from EdennCode.Annotation.enrichment.genre_hierarchy import (
    GENRE_HIERARCHY,
    GENRE_L1_VALUES,
    GENRE_L2_VALUES,
    L2_TO_L1,
    hierarchy_prompt_block,
    l1_for_l2,
    l2_children,
    validate_genre_level1,
    validate_genre_level2,
)
from EdennCode.Annotation.enrichment.taxonomy_schema import (
    ExtractedTaxonomy,
    TAXONOMY_JSON_SCHEMA,
)


class TestGenreHierarchyStructure(unittest.TestCase):

    def test_all_l1_values_in_l1_list(self):
        for l1 in GENRE_HIERARCHY:
            self.assertIn(l1, GENRE_L1_VALUES)

    def test_unknown_sentinel_in_both_lists(self):
        self.assertIn("Unknown", GENRE_L1_VALUES)
        self.assertIn("Unknown", GENRE_L2_VALUES)

    def test_l2_values_cover_all_children(self):
        for children in GENRE_HIERARCHY.values():
            for l2 in children:
                self.assertIn(l2, GENRE_L2_VALUES)

    def test_l2_to_l1_mapping_complete(self):
        for l1, children in GENRE_HIERARCHY.items():
            for l2 in children:
                self.assertEqual(L2_TO_L1[l2], l1)

    def test_no_l2_belongs_to_two_l1s(self):
        seen: dict[str, str] = {}
        for l1, children in GENRE_HIERARCHY.items():
            for l2 in children:
                self.assertNotIn(l2, seen, f"{l2!r} appears under both {seen.get(l2)!r} and {l1!r}")
                seen[l2] = l1

    def test_l2_children_returns_correct_subset(self):
        self.assertIn("House", l2_children("Electronic"))
        self.assertIn("Jazz", l2_children("Jazz / Blues"))
        self.assertEqual(l2_children("Nonexistent"), [])

    def test_l1_for_l2_correct(self):
        self.assertEqual(l1_for_l2("Trap"), "Hip-Hop / R&B")
        self.assertEqual(l1_for_l2("Synthwave"), "Electronic")
        self.assertEqual(l1_for_l2("Cinematic"), "Classical / Orchestral")
        self.assertEqual(l1_for_l2("Nonexistent"), "Unknown")

    def test_hierarchy_prompt_block_contains_all_l1s(self):
        block = hierarchy_prompt_block()
        for l1 in GENRE_HIERARCHY:
            self.assertIn(l1, block)


class TestValidation(unittest.TestCase):

    def test_valid_l1_passes_through(self):
        self.assertEqual(validate_genre_level1("Electronic"), "Electronic")
        self.assertEqual(validate_genre_level1("Hip-Hop / R&B"), "Hip-Hop / R&B")
        self.assertEqual(validate_genre_level1("Unknown"), "Unknown")

    def test_invalid_l1_falls_back_to_unknown(self):
        self.assertEqual(validate_genre_level1("trap"), "Unknown")
        self.assertEqual(validate_genre_level1(""), "Unknown")
        self.assertEqual(validate_genre_level1("Grunge"), "Unknown")

    def test_valid_l2_passes_through(self):
        self.assertEqual(validate_genre_level2("House"), "House")
        self.assertEqual(validate_genre_level2("Lo-fi Hip-Hop"), "Lo-fi Hip-Hop")
        self.assertEqual(validate_genre_level2("Unknown"), "Unknown")

    def test_invalid_l2_falls_back_to_unknown(self):
        self.assertEqual(validate_genre_level2("house"), "Unknown")
        self.assertEqual(validate_genre_level2("lo-fi"), "Unknown")
        self.assertEqual(validate_genre_level2(""), "Unknown")


class TestExtractedTaxonomyGenreFields(unittest.TestCase):

    def test_default_values_are_unknown(self):
        t = ExtractedTaxonomy()
        self.assertEqual(t.genre_level1, "Unknown")
        self.assertEqual(t.genre_level2, "Unknown")

    def test_from_llm_dict_valid_hierarchy(self):
        t = ExtractedTaxonomy.from_llm_dict({
            "genre_level1": "Electronic",
            "genre_level2": "Synthwave",
        })
        self.assertEqual(t.genre_level1, "Electronic")
        self.assertEqual(t.genre_level2, "Synthwave")

    def test_from_llm_dict_invalid_l1_falls_back(self):
        t = ExtractedTaxonomy.from_llm_dict({"genre_level1": "Grunge"})
        self.assertEqual(t.genre_level1, "Unknown")

    def test_from_llm_dict_invalid_l2_falls_back(self):
        t = ExtractedTaxonomy.from_llm_dict({
            "genre_level1": "Rock",
            "genre_level2": "lo-fi",
        })
        self.assertEqual(t.genre_level2, "Unknown")

    def test_from_llm_dict_cross_genre_mismatch_accepted(self):
        # LLM may assign Trap (Hip-Hop L2) under genre_level1 = Electronic —
        # we accept the L2 if it's a valid token; validation of cross-hierarchy
        # consistency is intentionally left to the LLM prompt rather than
        # hard-rejected here, to avoid data loss on edge genres.
        t = ExtractedTaxonomy.from_llm_dict({
            "genre_level1": "Electronic",
            "genre_level2": "Trap",
        })
        self.assertEqual(t.genre_level1, "Electronic")
        self.assertEqual(t.genre_level2, "Trap")

    def test_from_llm_dict_missing_fields_default(self):
        t = ExtractedTaxonomy.from_llm_dict({})
        self.assertEqual(t.genre_level1, "Unknown")
        self.assertEqual(t.genre_level2, "Unknown")
        self.assertEqual(t.genre_tags, [])


class TestJsonSchemGenreEnums(unittest.TestCase):
    """Verify that the JSON schema enum lists stay in sync with the hierarchy."""

    def _schema_props(self):
        return TAXONOMY_JSON_SCHEMA["schema"]["properties"]

    def test_genre_level1_enum_matches_l1_values(self):
        schema_enum = set(self._schema_props()["genre_level1"]["enum"])
        self.assertEqual(schema_enum, set(GENRE_L1_VALUES))

    def test_genre_level2_enum_matches_l2_values(self):
        schema_enum = set(self._schema_props()["genre_level2"]["enum"])
        self.assertEqual(schema_enum, set(GENRE_L2_VALUES))

    def test_both_fields_required(self):
        required = TAXONOMY_JSON_SCHEMA["schema"]["required"]
        self.assertIn("genre_level1", required)
        self.assertIn("genre_level2", required)


if __name__ == "__main__":
    unittest.main()
