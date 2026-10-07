from pathlib import Path

import pytest

from EdennCode.TestSuites.production import PRODUCTION_MODULES
from EdennCode.TestSuites.smoke import SMOKE_MODULES

# A hand-run scratch script, not a pytest module: its class and method names do
# not match pytest's conventions, so collecting it imports a stage for no tests.
# It matches `python_files = *_test.py`, so it has to be named here to stay out.
collect_ignore = [
    "EdennCode/WorkflowFactory/VideoVoiceOverWorkflow/Stages/PreprocessStage/preprocess_stage_test.py",
]


def _module_to_path(module_name: str) -> str:
    return str(Path(*module_name.split("."))).replace("\\", "/") + ".py"


SMOKE_PATHS = {_module_to_path(module_name) for module_name in SMOKE_MODULES}
PRODUCTION_PATHS = {_module_to_path(module_name) for module_name in PRODUCTION_MODULES}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    root = Path(str(config.rootpath)).resolve()

    for item in items:
        item_path = Path(str(item.path)).resolve()
        rel_path = item_path.relative_to(root).as_posix()

        if rel_path in SMOKE_PATHS:
            item.add_marker(pytest.mark.smoke)

        if rel_path in PRODUCTION_PATHS:
            item.add_marker(pytest.mark.production)
