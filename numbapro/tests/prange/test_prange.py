"""
>>> import test_prange
"""

import numbapro
import numba
from numba import utils
from numba import *

import numpy as np

@autojit(warn=False)
def prange_reduction():
    """
    >>> prange_reduction()
    45.0
    """
    sum = 0.0
    for i in numba.prange(10):
        sum += i
    return sum

@autojit(warn=False)
def prange_reduction2():
    """
    >>> prange_reduction2()
    49999995000000.0
    """
    sum = 0.0
    for i in numba.prange(10000000):
        sum += i
    return sum

@autojit(warn=False)
def prange_reduction_error():
    """
    DISABLED.

    >> prange_reduction_error()
    Traceback (most recent call last):
        ...
    NumbaError: 32:8: Local variable  'sum' is not bound yet
    """
    for i in numba.prange(10):
        sum += i

    sum = 0.0
    return sum

@autojit(warn=False)
def prange_reduction_and_privates():
    """
    >>> prange_reduction_and_privates()
    100.0
    """
    sum = 10.0
    for i in numba.prange(10):
        j = i * 2
        sum += j

    return sum

@autojit(warn=False)
def prange_lastprivate():
    """
    >>> prange_lastprivate()
    18
    100.0
    """
    sum = 10.0
    for i in numba.prange(10):
        j = i * 2
        sum += j

    print j
    return sum

@autojit(warn=False)
def prange_shared_privates_reductions(shared):
    """
    >>> prange_shared_privates_reductions(2.0)
    100.0
    """
    sum = 10.0

    for i in numba.prange(10):
        j = i * shared
        sum += j

    shared = 3.0
    return sum


@autojit(warn=False)
def test_sum2d(A):
    """
    >>> a = np.arange(100).reshape(10, 10)
    >>> test_sum2d(a)
    4950.0
    >>> test_sum2d(a.astype(np.complex128))
    (4950+0j)
    >>> np.sum(a)
    4950
    """
    sum = 0.0
    for i in numba.prange(A.shape[0]):
        for j in range(A.shape[1]):
            # print i, j
            sum += A[i, j]

    return sum

@autojit(warn=False)
def test_prange_in_closure(x):
    """
    >>> test_prange_in_closure(2.0)()
    1000.0
    """
    sum = 10.0
    N = 10

    @double()
    def inner():
        sum = 100.0
        for i in numba.prange(N):
            for j in range(N):
                sum += i * x

        return sum

    return inner


@autojit(warn=False)
def test_prange_in_closure2(x):
    """
    >>> test_prange_in_closure2(2.0)()
    10000.0
    """
    sum = 10.0
    N = 10

    @double()
    def inner():
        sum = 100.0
        for i in numba.prange(N):
            for j in range(N):
                sum += (i * N + j) * x

        return sum

    return inner

if __name__ == '__main__':
#    prange_reduction_error()

    a = np.arange(100).reshape(10, 10)
    print test_sum2d(a)
#    print test_sum2d(a.astype(np.complex128))

import numba
numba.testmod()