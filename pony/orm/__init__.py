from pony.orm.core import *
from pony.orm.core import __all__ as _core_all

__all__ = list(_core_all)  # star-import of pony.orm = exactly the old surface
from pony.orm.core import io  # explicit only: not star-exported (must not shadow stdlib io)
