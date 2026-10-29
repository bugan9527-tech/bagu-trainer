"""Supported legacy engine import; no assembly or state lives here.

The implementation and its lazy aliases share bootstrap's application instance.
"""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("bagu_trainer.application.learning.engine")
