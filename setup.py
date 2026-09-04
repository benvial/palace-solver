"""Force a platform wheel: the package ships a binary, not Python extensions.

setuptools would otherwise tag the wheel ``py3-none-any`` because it sees no
extension modules, and ``auditwheel`` refuses purelib wheels. The final
``py3-none-<platform>`` tag is applied by the retag step of
:mod:`wheelbuild.assemble`.
"""

from setuptools import setup
from setuptools.dist import Distribution


class BinaryDistribution(Distribution):
    """Distribution that always reports a platform-specific payload."""

    def has_ext_modules(self) -> bool:  # noqa: D102
        return True


setup(distclass=BinaryDistribution)
