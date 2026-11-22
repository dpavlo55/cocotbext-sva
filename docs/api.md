# API reference

```{eval-rst}
.. module:: cocotbext.sva
```

## Assertion groups

```{eval-rst}
.. autoclass:: Assertions
.. autoclass:: AssertionHandle
.. autofunction:: checked
```

## Clocks

```{eval-rst}
.. autofunction:: posedge
.. autofunction:: negedge
.. autofunction:: edge
.. autoclass:: ClockingEvent
```

## Reusable properties

```{eval-rst}
.. autoclass:: Property
.. autoclass:: Sequence
.. autoclass:: BoundProperty
.. autoclass:: Checker
```

## Results

```{eval-rst}
.. autoclass:: Failure
   :no-members:
```

## Sampling inside the simulator

```{eval-rst}
.. autoclass:: Probe
```

## Offline checking

```{eval-rst}
.. autoclass:: TraceSampler
.. autoclass:: OfflineResult
.. autoclass:: OfflineFailure
```

## Errors

```{eval-rst}
.. autoexception:: SvaError
.. autoexception:: SvaSyntaxError
.. autoexception:: SvaNameError
.. autoexception:: SvaUnsupportedError
```
