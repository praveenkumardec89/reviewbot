from .security import SecurityAgent
from .code_quality import CodeQualityAgent
from .architecture import ArchitectureAgent
from .simplification import SimplificationAgent
from .test_coverage import TestCoverageAgent
from .performance import PerformanceAgent

ALL_AGENTS = [
    SecurityAgent(),
    CodeQualityAgent(),
    ArchitectureAgent(),
    SimplificationAgent(),
    TestCoverageAgent(),
    PerformanceAgent(),
]
