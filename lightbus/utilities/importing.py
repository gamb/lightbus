import logging
from typing import Sequence, Tuple, Callable

import pkg_resources

logger = logging.getLogger(__name__)


def import_from_string(name):
    components = name.split('.')
    mod = __import__(components[0])
    for comp in components[1:]:
        mod = getattr(mod, comp)
    return mod


def load_entrypoint_classes(entrypoint_name) -> Sequence[Tuple[str, str, Callable]]:
    """Load classes specified in an entrypoint

    Entrypoints are specified in setup.py, and Lightbus uses them to
    discover plugins & transports.
    """
    found_classes = []
    for entrypoint in pkg_resources.iter_entry_points(entrypoint_name):
        class_ = entrypoint.load()
        found_classes.append((entrypoint.module_name, entrypoint.name, class_))
    return found_classes
