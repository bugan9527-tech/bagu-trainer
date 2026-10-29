"""File-loading adapter for the pure matching algorithm, retaining Matcher(kb)."""
from typing import Optional
from bagu_trainer.infrastructure.concepts import load_concept_profiles
from bagu_trainer.domain import matching as _rules
from bagu_trainer.domain.matching import SemanticRetriever
from bagu_trainer.domain.matching import tokenize
from bagu_trainer.domain.matching import STOP_GRAMS
from bagu_trainer.domain.matching import STOP_EN
from bagu_trainer.domain.matching import _tokenize_cached
from bagu_trainer.domain.matching import _domain_profile


class Matcher(_rules.Matcher):
    def __init__(self, knowledge_base, k1: float = 1.5, b: float = 0.75,
                 idf_cap: float = 7.0, conf_min: float = 0.05,
                 raw_min: float = 10.0, margin_min: float = 0.01,
                 conf_strong: float = 0.4, pivot: float = 25.0,
                 use_tags: bool = True, use_concepts: bool = True,
                 semantic: Optional[SemanticRetriever] = None,
                 semantic_weight: float = 0.3,
                 concept_mode: str = "sum",
                 skip_english_concepts: bool = False,
                 w_concept_terms: float = 1.8,
                 w_concept_summary: float = 0.8,
                 concept_damp: float = 1.0,
                 concept_damp_floor: float = 5.0,
                 concept_gram_cap: Optional[float] = None,
                 concept_overlap_discount: float = 0.5):
        self.kb = knowledge_base
        profiles = load_concept_profiles(getattr(knowledge_base, "concepts_path", None)) if use_concepts else {}
        super().__init__(knowledge_base.all(),
            k1=k1,
            b=b,
            idf_cap=idf_cap,
            conf_min=conf_min,
            raw_min=raw_min,
            margin_min=margin_min,
            conf_strong=conf_strong,
            pivot=pivot,
            use_tags=use_tags,
            use_concepts=use_concepts,
            semantic=semantic,
            semantic_weight=semantic_weight,
            concept_mode=concept_mode,
            skip_english_concepts=skip_english_concepts,
            w_concept_terms=w_concept_terms,
            w_concept_summary=w_concept_summary,
            concept_damp=concept_damp,
            concept_damp_floor=concept_damp_floor,
            concept_gram_cap=concept_gram_cap,
            concept_overlap_discount=concept_overlap_discount,
            concept_profiles=profiles)


def __getattr__(name):
    # Historical evaluation scripts still import the algorithm's helper names.
    return getattr(_rules, name)

# Explicit compatibility exports used by evaluation/legacy callers.
__all__ = ['Matcher', 'SemanticRetriever', 'tokenize', 'STOP_GRAMS', 'STOP_EN', '_tokenize_cached', '_domain_profile']
