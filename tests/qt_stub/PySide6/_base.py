class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        d = _Meta(name, (_Dummy,), {})
        type.__setattr__(cls, name, d)
        return d
    def __or__(cls, o): return _Dummy()
    __ror__ = __and__ = __rand__ = __or__
    def __invert__(cls): return _Dummy()

class _Dummy(metaclass=_Meta):
    def __init__(self, *a, **k): pass
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        d = _Dummy()
        object.__setattr__(self, name, d)
        return d
    def __call__(self, *a, **k): return _Dummy()
    def __or__(self, o): return _Dummy()
    __ror__ = __and__ = __rand__ = __sub__ = __add__ = __or__
    def __invert__(self): return _Dummy()
    def __bool__(self): return False
    def __iter__(self): return iter(())

class Signal:
    def __init__(self): self.cbs = []
    def connect(self, cb): self.cbs.append(cb)
    def emit(self, *a):
        for cb in self.cbs: cb(*a)

def make_module_getattr(g):
    def __getattr__(name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = _Meta(name, (_Dummy,), {})
        g[name] = cls
        return cls
    return __getattr__
