"""Load the integration's HA-free modules as a stub package, without importing homeassistant."""

import pathlib
import sys
import types

TESTS_DIR = pathlib.Path(__file__).parents[1]
PACKAGE_DIR = TESTS_DIR.parent / "custom_components" / "mikrotik_push"

package = types.ModuleType("mikrotik_push")
package.__path__ = [str(PACKAGE_DIR)]
sys.modules["mikrotik_push"] = package
sys.path.insert(0, str(TESTS_DIR))
