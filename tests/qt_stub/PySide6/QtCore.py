from ._base import make_module_getattr
__getattr__ = make_module_getattr(globals())
