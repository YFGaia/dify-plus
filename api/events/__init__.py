"""
Project event package plus compatibility exports for third-party `events.Events`.

This repository has a top-level `events` package for application event handlers, which shadows the
third-party `events` package expected by `opensearchpy`. Some vector store integrations import
`from events import Events` during module import, so we provide a small compatible implementation
here to keep both import styles working.
"""


class _EventSlot:
    def __init__(self, name: str):
        self.targets = []
        self.__name__ = name

    def __repr__(self) -> str:
        return f"event '{self.__name__}'"

    def __call__(self, *args, **kwargs):
        for callback in tuple(self.targets):
            callback(*args, **kwargs)

    def __iadd__(self, callback):
        self.targets.append(callback)
        return self

    def __isub__(self, callback):
        while callback in self.targets:
            self.targets.remove(callback)
        return self

    def __len__(self) -> int:
        return len(self.targets)

    def __iter__(self):
        def gen():
            yield from self.targets

        return gen()

    def __getitem__(self, key):
        return self.targets[key]


# Name intentionally mirrors the third-party `events.EventsException` API for drop-in compatibility.
class EventsException(Exception):  # noqa: N818
    pass


class Events:
    """
    Minimal compatibility implementation of the third-party `events.Events` API.

    This is intentionally small and only supports the behaviour relied on by dependencies such as
    `opensearchpy.metrics.metrics_events`.
    """

    def __init__(self, events=None, event_slot_cls=_EventSlot):
        self.__event_slot_cls__ = event_slot_cls

        if events is not None:
            try:
                iter(events)
            except TypeError as exc:
                raise AttributeError(f"type object {type(events)} is not iterable") from exc
            else:
                self.__events__ = events

    def __getattr__(self, name: str):
        if name.startswith('__'):
            raise AttributeError(f"type object '{self.__class__.__name__}' has no attribute '{name}'")

        if hasattr(self, '__events__') and name not in self.__events__:
            raise EventsException(f"Event '{name}' is not declared")

        if hasattr(self.__class__, '__events__') and name not in self.__class__.__events__:
            raise EventsException(f"Event '{name}' is not declared")

        self.__dict__[name] = event = self.__event_slot_cls__(name)
        return event

    def __getitem__(self, item):
        return self.__dict__[item]

    def __repr__(self) -> str:
        return f'<{self.__class__.__module__}.{self.__class__.__name__} object at {hex(id(self))}>'

    __str__ = __repr__

    def __len__(self) -> int:
        return len(list(self.__iter__()))

    def __iter__(self):
        dictitems = self.__dict__.items()

        def gen():
            for _, value in dictitems:
                if isinstance(value, self.__event_slot_cls__):
                    yield value

        return gen()


__all__ = [
    'Events',
    'EventsException',
]
