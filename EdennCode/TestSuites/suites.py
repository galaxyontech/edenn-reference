import unittest


def build_suite(loader: unittest.TestLoader, module_names: list[str]) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for module_name in module_names:
        suite.addTests(loader.loadTestsFromName(module_name))
    return suite
