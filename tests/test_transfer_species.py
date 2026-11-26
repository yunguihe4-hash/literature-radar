"""A research domain must not masquerade as a paper's source species."""
import unittest

from literature_radar.attention.transfer import _species_hit
from literature_radar.models import Paper


class TransferSpeciesTests(unittest.TestCase):
    def test_generic_domain_does_not_supply_species(self):
        paper = Paper('SYN-1', 'Mammalian Y assembly methods',
                      'A synthetic mammalian Y assembly was studied.', 'fictional')
        bundle = {'topic': {'extensions': {'source_roles': {'technology': ['Mammalian Y']}}}}
        species, _, _ = _species_hit(paper, bundle, {'from_domain': 'Mammalian Y'})
        self.assertIsNone(species)

    def test_explicit_human_domain_reports_taxon_only(self):
        paper = Paper('SYN-2', 'Human Y assembly', 'The human Y was assembled.', 'fictional')
        bundle = {'topic': {'extensions': {'source_roles': {'technology': ['Human Y']}}}}
        species, _, field = _species_hit(paper, bundle, {'from_domain': 'Human Y'})
        self.assertEqual(species, 'Human')
        self.assertEqual(field, 'title')
