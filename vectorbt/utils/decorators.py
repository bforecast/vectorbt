"""Class and function decorators."""

from functools import wraps, lru_cache, RLock

from vectorbt.utils import checks


class class_or_instancemethod(classmethod):
    """Function decorator that binds `self` to a class if the function is called as class method,
    otherwise to an instance."""

    def __get__(self, instance, type_):
        descr_get = super().__get__ if instance is None else self.__func__.__get__
        return descr_get(instance, type_)


class classproperty(object):
    """Property that can be called on a class."""

    def __init__(self, f):
        self.f = f

    def __get__(self, obj, owner):
        return self.f(owner)


class custom_property():
    """Custom extensible, read-only property.

    Can be called both as
    ```plaintext
    @custom_property
    def user_function...
    ```
    and
    ```plaintext
    @custom_property(**kwargs)
    def user_function...
    ```

    !!! note
        `custom_property` instances belong to classes, not class instances. Thus changing the property,
        for example, by disabling caching, will do the same for each instance of the class where
        the property has been defined."""

    def __new__(cls, *args, **kwargs):
        if len(args) == 0:
            return lambda func: cls(func, **kwargs)
        elif len(args) == 1:
            return super().__new__(cls)
        else:
            raise ValueError("Either function or keyword arguments must be passed")

    def __init__(self, func, **kwargs):
        self.func = func
        self.name = func.__name__
        self.kwargs = kwargs
        self.__doc__ = getattr(func, '__doc__')

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return self.func(instance)

    def __set__(self, obj, value):
        raise AttributeError("can't set attribute")


def is_caching_enabled(disabled, name, instance, func=None, **kwargs):
    """Check whether caching is enabled for a cacheable property/function.

    Conditions have the following priority:

    ```plaintext
    1) is caching disabled locally?
    2) is function in whitelist/blacklist? (properties are not supported)
    3) is (instance, function name) in whitelist/blacklist?
    4) is function name in whitelist/blacklist?
    5) is instance, its class, or class name in whitelist/blacklist?
    6) is subset of kwargs in whitelist/blacklist?
    7) is caching disabled globally?

    All names are case-sensitive.
    ```"""
    from vectorbt import settings

    if disabled:
        return False
    if len(settings.caching['whitelist']) > 0:
        if func is not None and func in settings.caching['whitelist']:
            return True
        if (instance, name) in settings.caching['whitelist']:
            return True
        if name in settings.caching['whitelist']:
            return True
        if instance in settings.caching['whitelist']:
            return True
        if hasattr(instance, '__class__'):
            if (instance.__class__, name) in settings.caching['whitelist']:
                return True
            if instance.__class__ in settings.caching['whitelist']:
                return True
            if (instance.__class__.__name__ + '.' + name) in settings.caching['whitelist']:
                return True
            if instance.__class__.__name__ in settings.caching['whitelist']:
                return True
        for dct in settings.caching['whitelist']:
            if isinstance(dct, dict):
                if dct.items() <= kwargs.items():
                    return True
    if len(settings.caching['blacklist']) > 0:
        if func is not None and func in settings.caching['blacklist']:
            return False
        if (instance, name) in settings.caching['blacklist']:
            return False
        if name in settings.caching['blacklist']:
            return False
        if instance in settings.caching['blacklist']:
            return False
        if hasattr(instance, '__class__'):
            if (instance.__class__, name) in settings.caching['blacklist']:
                return False
            if instance.__class__ in settings.caching['blacklist']:
                return False
            if (instance.__class__.__name__ + '.' + name) in settings.caching['blacklist']:
                return False
            if instance.__class__.__name__ in settings.caching['blacklist']:
                return False
        for dct in settings.caching['blacklist']:
            if isinstance(dct, dict):
                if dct.items() <= kwargs.items():
                    return False
    return settings.caching['enabled']


_NOT_FOUND = object()


class cached_property(custom_property):
    """Extends `custom_property` with caching.

    Similar to `functools.cached_property`, but without replacing the original attribute
    to be able to re-compute whenever needed.

    Disables caching if `is_caching_enabled` yields False.

    Cache can be cleared by calling `clear_cache` with instance as argument.

    !!! note:
        Assumes that the instance (provided as `self`) won't change. If calculation depends
        upon object attributes that can be changed, it won't notice the change."""

    def __init__(self, func, disabled=False, **kwargs):
        super().__init__(func, **kwargs)
        self.lock = RLock()
        self.disabled = disabled

    def clear_cache(self, instance):
        """Clear the cache for this property belonging to `instance`."""
        if hasattr(instance, self.attrname):
            delattr(instance, self.attrname)

    @property
    def attrname(self):
        """Get name of cached attribute."""
        return '__cached_' + self.name

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        if not is_caching_enabled(self.disabled, self.name, instance, **self.kwargs):
            return super().__get__(instance, owner=owner)
        cache = instance.__dict__
        val = cache.get(self.attrname, _NOT_FOUND)
        if val is _NOT_FOUND:
            with self.lock:
                # check if another thread filled cache while we awaited lock
                val = cache.get(self.attrname, _NOT_FOUND)
                if val is _NOT_FOUND:
                    val = self.func(instance)
                    cache[self.attrname] = val
        return val


def custom_method(*args, **kwargs):
    """Custom extensible method.

    Stores `**kwargs` as attributes of the wrapper function.

    Can be called both as
    ```python-repl
    >>> @cached_method
    ... def user_function(): pass
    ```
    and
    ```python-repl
    >>> @cached_method(maxsize=128, typed=False, disabled=False, **kwargs)
    ... def user_function(): pass
    ```
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper.func = func
        wrapper.kwargs = kwargs

        return wrapper

    if len(args) == 0:
        return decorator
    elif len(args) == 1:
        return decorator(args[0])
    else:
        raise ValueError("Either function or keyword arguments must be passed")


def cached_method(*args, maxsize=128, typed=False, disabled=False, **kwargs):
    """Extends `custom_method` with caching.

    Internally uses `functools.lru_cache`.

    Disables caching if `is_caching_enabled` yields False or a non-hashable object
    as argument has been passed.

    See notes on `cached_property`."""

    def decorator(func):
        @wraps(func)
        def wrapper(instance, *args, **kwargs):
            from vectorbt import settings

            def partial_func(*args, **kwargs):
                # Ignores non-hashable instances
                return func(instance, *args, **kwargs)

            _func = None
            if hasattr(instance, wrapper.name):
                _func = getattr(instance, wrapper.name)
            if not is_caching_enabled(wrapper.disabled, wrapper.name, instance, func=_func, **wrapper.kwargs):
                return func(instance, *args, **kwargs)
            cache = instance.__dict__
            cached_func = cache.get(wrapper.attrname, _NOT_FOUND)
            if cached_func is _NOT_FOUND:
                with wrapper.lock:
                    # check if another thread filled cache while we awaited lock
                    cached_func = cache.get(wrapper.attrname, _NOT_FOUND)
                    if cached_func is _NOT_FOUND:
                        cached_func = lru_cache(maxsize=wrapper.maxsize, typed=wrapper.typed)(partial_func)
                        cache[wrapper.attrname] = cached_func  # store function instead of output

            # Check if object can be hashed
            hashable = True
            for arg in args:
                if not checks.is_hashable(arg):
                    hashable = False
                    break
            for k, v in kwargs.items():
                if not checks.is_hashable(v):
                    hashable = False
                    break
            if not hashable:
                # If not, do not invoke lru_cache
                return func(instance, *args, **kwargs)
            return cached_func(*args, **kwargs)

        wrapper.func = func
        wrapper.maxsize = maxsize
        wrapper.typed = typed
        wrapper.name = func.__name__
        wrapper.attrname = '__cached_' + func.__name__
        wrapper.lock = RLock()
        wrapper.disabled = disabled
        wrapper.kwargs = kwargs

        def clear_cache(instance):
            """Clear the cache for this method belonging to `instance`."""
            if hasattr(instance, wrapper.attrname):
                delattr(instance, wrapper.attrname)

        setattr(wrapper, 'clear_cache', clear_cache)

        return wrapper

    if len(args) == 0:
        return decorator
    elif len(args) == 1:
        return decorator(args[0])
    else:
        raise ValueError("Either function or keyword arguments must be passed")


def traverse_attr_kwargs(cls, key=None, value=None):
    """Traverse `cls` and its children for properties/methods with `kwargs`,
    and optionally a specific `key` and `value`.

    Class attributes acting as children should have a key `child_cls`.

    Returns a nested dict of attributes."""
    checks.assert_type(cls, type)

    if value is not None and not isinstance(value, tuple):
        value = (value,)
    attrs = {}
    for attr in dir(cls):
        prop = getattr(cls, attr)
        if hasattr(prop, 'kwargs'):
            kwargs = getattr(prop, 'kwargs')
            if key is None:
                attrs[attr] = kwargs
            else:
                if key in kwargs:
                    if value is None:
                        attrs[attr] = kwargs
                    else:
                        _value = kwargs[key]
                        if _value in value:
                            attrs[attr] = kwargs
            if 'child_cls' in kwargs:
                child_cls = kwargs['child_cls']
                checks.assert_type(child_cls, type)
                attrs[attr] = kwargs
                attrs[attr]['child_attrs'] = traverse_attr_kwargs(child_cls, key, value)
    return attrs
