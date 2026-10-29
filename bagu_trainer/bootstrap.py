"""Application-scoped engine assembly.

Importing this module or :mod:`bagu_trainer.application.learning.engine` does not load a question
bank or open a learning session. The default application engine is assembled
only when an entry point asks for it.
"""
import threading

from bagu_trainer.infrastructure.config import load_config
from bagu_trainer.infrastructure.knowledge_base import KnowledgeBase
from bagu_trainer.infrastructure.matcher import Matcher
from bagu_trainer.application.learning.generator import QuizGenerator
from bagu_trainer.domain.grading import Scorer

_lock = threading.Lock()
_engine = None


def create_engine(*, cfg=None, kb=None, matcher=None, generator=None,
                  scorer=None, session=None, config_loader=None,
                  knowledge_base_factory=None, matcher_factory=None):
    """Assemble one engine from supplied dependencies or local defaults."""
    from bagu_trainer.application.learning.engine import Engine

    configuration = load_config() if cfg is None else cfg
    bank_factory = knowledge_base_factory or KnowledgeBase
    match_factory = matcher_factory or Matcher
    bank = kb if kb is not None else bank_factory()
    instance = Engine(
        cfg=configuration,
        kb=bank,
        matcher=matcher if matcher is not None else match_factory(bank),
        generator=generator,
        scorer=scorer if scorer is not None else Scorer(),
        session=session,
        config_loader=config_loader,
        knowledge_base_factory=bank_factory,
        matcher_factory=match_factory,
    )
    if generator is None:
        instance.generator = QuizGenerator(config_loader=instance.load_config)
    return instance


def get_engine():
    """Return the single lazily assembled engine for this application process."""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                # Resolve the current loader on each use for legacy tests and
                # runtime config reloads while keeping the engine instance fixed.
                from bagu_trainer.application.learning import engine as engine_module
                _engine = create_engine(config_loader=lambda: engine_module.load_config())
    return _engine
