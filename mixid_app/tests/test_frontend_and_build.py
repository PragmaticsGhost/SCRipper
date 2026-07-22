"""Frontend, container build, Compose, and setup regression coverage."""

from tests.regression_cases import (
    BuildPinTests,
    FrontendSafetyTests,
    WindowsSetupTests,
)

__all__ = ["BuildPinTests", "FrontendSafetyTests", "WindowsSetupTests"]
