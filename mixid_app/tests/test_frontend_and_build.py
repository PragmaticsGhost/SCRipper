"""Frontend, container build, Compose, and setup regression coverage."""

from tests.regression_cases import (
    BuildPinTests,
    DependencyFreshnessTests,
    FrontendSafetyTests,
    WindowsSetupTests,
)

__all__ = [
    "BuildPinTests",
    "DependencyFreshnessTests",
    "FrontendSafetyTests",
    "WindowsSetupTests",
]
