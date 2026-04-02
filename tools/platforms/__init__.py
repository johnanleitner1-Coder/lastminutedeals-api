# platforms/ — Platform fetcher plugin registry
# Each fetcher subclasses BaseSlotFetcher from base.py.
# Register new platforms by adding to REGISTRY below.

from tools.platforms.base import BaseSlotFetcher  # noqa: F401

REGISTRY: dict[str, type] = {}


def register(cls):
    """Class decorator that adds a fetcher to REGISTRY keyed on PLATFORM_NAME."""
    REGISTRY[cls.PLATFORM_NAME] = cls
    return cls
