from packages.modules.ui_testing.capture import CaptureResult, capture_many, capture_page
from packages.modules.ui_testing.diff import PixelDiff, compute_diff
from packages.modules.ui_testing.executor import UiTestingExecutor

__all__ = [
    "CaptureResult",
    "PixelDiff",
    "UiTestingExecutor",
    "capture_many",
    "capture_page",
    "compute_diff",
]
