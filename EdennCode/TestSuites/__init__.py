import unittest

from EdennCode.TestSuites.production import PRODUCTION_MODULES
from EdennCode.TestSuites.smoke import SMOKE_MODULES
from EdennCode.TestSuites.suites import build_suite


NON_MEDIA_MODULES = [
    "EdennCode.test_logging",
]

ALL_MODULES = [
    *SMOKE_MODULES,
    *PRODUCTION_MODULES,
    *NON_MEDIA_MODULES,
]


def load_tests(
    loader: unittest.TestLoader,
    tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    return build_suite(loader, ALL_MODULES)
