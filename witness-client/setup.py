"""Platform wheel containing a C ABI library, independent of Python's ABI."""

import sys
from pathlib import Path

from setuptools import Distribution, setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py


class Build(build_py):
    def run(self) -> None:
        suffix = "dylib" if sys.platform == "darwin" else "so"
        if not (Path("src/witness_client") / f"libwitnessproof.{suffix}").is_file():
            raise RuntimeError("Build the native library first: python build_wheel.py")
        super().run()


class BinaryDistribution(Distribution):
    def has_ext_modules(self) -> bool:
        return True


class Wheel(bdist_wheel):
    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self) -> tuple[str, str, str]:
        _, _, platform = super().get_tag()
        return "py3", "none", platform


setup(distclass=BinaryDistribution, cmdclass={"build_py": Build, "bdist_wheel": Wheel})
