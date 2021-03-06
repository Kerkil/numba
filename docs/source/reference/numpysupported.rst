
========================
Supported Numpy features
========================

One objective of Numba is having a seamless integration with `NumPy`_.
NumPy arrays provide an efficient storage method for homogeneous sets of
data.  NumPy dtypes provide type information useful when compiling, and
the regular, structured storage of potentially large amounts of data
in memory provides an ideal memory layout for code generation.  Numba
excels at generating code that executes on top of NumPy arrays.

NumPy support in Numba comes in many forms:

* Numba understands calls to NumPy `ufuncs`_ and is able to generate
  equivalent native code for many of them.

* NumPy arrays are directly supported in Numba.  Access to Numpy arrays
  is very efficient, as indexing is lowered to direct memory accesses
  when possible.

* Numba is able to generate `ufuncs`_ and `gufuncs`_. This means that it
  is possible to implement ufuncs and gufuncs within Python, getting
  speeds comparable to that of ufuncs/gufuncs implemented in C extension
  modules using the NumPy C API.

.. _NumPy: http://www.numpy.org/
.. _ufuncs: http://docs.scipy.org/doc/numpy/reference/ufuncs.html
.. _gufuncs: http://docs.scipy.org/doc/numpy/reference/c-api.generalized-ufuncs.html

The following sections focus on the Numpy features supported in
:term:`nopython mode`, unless otherwise stated.


Scalar types
============

Numba supports the following Numpy scalar types:

* **Integers**: all integers of either signedness, and any width up to 64 bits
* **Booleans**
* **Real numbers:** single-precision (32-bit) and double-precision (64-bit) reals
* **Complex numbers:** single-precision (2x32-bit) and double-precision (2x64-bit) complex numbers
* **Datetimes and timestamps:** of any unit
* **Character sequences** (but no operations are available on them)
* **Structured scalars:** structured scalars made of any of the types above and arrays of the types above

The following scalar types and features are not supported:

* **Arbitrary Python objects**
* **Half-precision and extended-precision** real and complex numbers
* **Nested structured scalars** the fields of structured scalars may not contain other structured scalars

The operations supported on scalar Numpy numbers are the same as on the
equivalent built-in types such as ``int`` or ``float``.  You can use
a type's constructor to convert from a different type or width.

Structured scalars support attribute getting and setting.

.. seealso::
   `Numpy scalars <http://docs.scipy.org/doc/numpy/reference/arrays.scalars.html>`_
   reference.


Array types
===========

Arrays of any of the scalar types above are supported, regardless of the shape
or layout.

Operations
----------

Arrays support iteration and full indexing (i.e. indexing that yields
scalar values).  Partial indexing by a single integer is supported,
but the resulting views can not be returned to Python.  Other kinds of
partial indexing (for example indexing a 3-d array with a 2-tuple)
isn't supported.

Attributes
----------

The following attributes of Numpy arrays are supported:

* :attr:`~numpy.ndarray.flat`
* :attr:`~numpy.ndarray.itemsize`
* :attr:`~numpy.ndarray.ndim`
* :attr:`~numpy.ndarray.shape`
* :attr:`~numpy.ndarray.size`
* :attr:`~numpy.ndarray.strides`

Methods
-------

The following methods of Numpy arrays are supported in their basic form
(without any optional arguments):

* :meth:`~numpy.ndarray.argmax`
* :meth:`~numpy.ndarray.argmin`
* :meth:`~numpy.ndarray.max`
* :meth:`~numpy.ndarray.mean`
* :meth:`~numpy.ndarray.min`
* :meth:`~numpy.ndarray.prod`
* :meth:`~numpy.ndarray.std`
* :meth:`~numpy.ndarray.sum`
* :meth:`~numpy.ndarray.var`

The corresponding top-level Numpy functions (such as :func:`numpy.sum`)
are similarly supported.


Functions
=========

The following top-level functions are supported:

* :class:`numpy.ndenumerate`
* :class:`numpy.ndindex`
* :func:`numpy.round_`

The following constructors are supported, only with a numeric input:

* :class:`numpy.complex64`
* :class:`numpy.complex128`
* :class:`numpy.float32`
* :class:`numpy.float64`
* :class:`numpy.int8`
* :class:`numpy.int16`
* :class:`numpy.int32`
* :class:`numpy.int64`
* :class:`numpy.intc`
* :class:`numpy.intp`
* :class:`numpy.uint8`
* :class:`numpy.uint16`
* :class:`numpy.uint32`
* :class:`numpy.uint64`
* :class:`numpy.uintc`
* :class:`numpy.uintp`


Modules
=======

.. _numpy-random:

``random``
----------

Numba supports top-level functions from the
`numpy.random <http://docs.scipy.org/doc/numpy/reference/routines.random.html>`_
module, but does not allow you to create individual RandomState instances.
The same algorithms are used as for :ref:`the standard
random module <pysupported-random>` (and therefore the same notes apply),
but with an independent internal state: seeding or drawing numbers from
one generator won't affect the other.

The following functions are supported, but only with scalar output: you can't
pass a *size* argument.

Initialization
''''''''''''''

* :func:`numpy.random.seed`: with an integer argument only

Simple random data
''''''''''''''''''

* :func:`numpy.random.rand`: only without argument
* :func:`numpy.random.randint`
* :func:`numpy.random.randn`: only without argument
* :func:`numpy.random.random`
* :func:`numpy.random.random_sample`
* :func:`numpy.random.ranf`
* :func:`numpy.random.sample`

Permutations
''''''''''''

* :func:`numpy.random.shuffle`: the sequence argument must be a one-dimension
  Numpy array or buffer-providing object (such as a :class:`bytearray`
  or :class:`array.array`)

Distributions
'''''''''''''

* :func:`numpy.random.beta`
* :func:`numpy.random.binomial`
* :func:`numpy.random.chisquare`
* :func:`numpy.random.exponential`
* :func:`numpy.random.f`
* :func:`numpy.random.gamma`
* :func:`numpy.random.geometric`
* :func:`numpy.random.gumbel`
* :func:`numpy.random.hypergeometric`
* :func:`numpy.random.laplace`
* :func:`numpy.random.logistic`
* :func:`numpy.random.lognormal`
* :func:`numpy.random.logseries`
* :func:`numpy.random.negative_binomial`
* :func:`numpy.random.normal`
* :func:`numpy.random.pareto`
* :func:`numpy.random.poisson`
* :func:`numpy.random.power`
* :func:`numpy.random.rayleigh`
* :func:`numpy.random.standard_cauchy`
* :func:`numpy.random.standard_exponential`
* :func:`numpy.random.standard_gamma`
* :func:`numpy.random.standard_normal`
* :func:`numpy.random.standard_t`
* :func:`numpy.random.triangular`
* :func:`numpy.random.uniform`
* :func:`numpy.random.vonmises`
* :func:`numpy.random.wald`
* :func:`numpy.random.weibull`
* :func:`numpy.random.zipf`

.. note::
   Calling :func:`numpy.random.seed` from non-Numba code (or from
   :term:`object mode` code) will seed the Numpy random generator, not the
   Numba random generator.

.. note::
   The generator is not thread-safe when :ref:`releasing the GIL <jit-nogil>`.

   Also, under Unix, if creating a child process using :func:`os.fork` or the
   :mod:`multiprocessing` module, the child's random generator will inherit
   the parent's state and will therefore produce the same sequence of
   numbers (except when using the "forkserver" start method under Python 3.4
   and later).


Standard ufuncs
===============

One objective of Numba is having all the
`standard ufuncs in NumPy <http://docs.scipy.org/doc/numpy/reference/ufuncs.html#available-ufuncs>`_
understood by Numba.  When a supported ufunc is found when compiling a
function, Numba maps the ufunc to equivalent native code.  This allows the
use of those ufuncs in Numba code that gets compiled in :term:`nopython mode`.

Limitations
-----------

Right now, only a selection of the standard ufuncs work in :term:`nopython mode`.

Also, in its current implementation ufuncs working on arrays will only
compile in :term:`nopython mode` if their output array is passed explicitly.
This limitation does not apply when working with scalars.

Following is a list of the different standard ufuncs that Numba is aware of,
sorted in the same way as in the NumPy documentation.


Math operations
---------------

==============  =============  ===============
    UFUNC                  MODE
--------------  ------------------------------
    name         object mode    nopython mode
==============  =============  ===============
 add                 Yes          Yes
 subtract            Yes          Yes
 multiply            Yes          Yes
 divide              Yes          Yes
 logaddexp           Yes          Yes
 logaddexp2          Yes          Yes
 true_divide         Yes          Yes
 floor_divide        Yes          Yes
 negative            Yes          Yes
 power               Yes          Yes
 remainder           Yes          Yes
 mod                 Yes          Yes
 fmod                Yes          Yes
 abs                 Yes          Yes
 absolute            Yes          Yes
 fabs                Yes          Yes
 rint                Yes          Yes
 sign                Yes          Yes
 conj                Yes          Yes
 exp                 Yes          Yes
 exp2                Yes          Yes
 log                 Yes          Yes
 log2                Yes          Yes
 log10               Yes          Yes
 expm1               Yes          Yes
 log1p               Yes          Yes
 sqrt                Yes          Yes
 square              Yes          Yes
 reciprocal          Yes          Yes
 conjugate           Yes          Yes
==============  =============  ===============


Trigonometric functions
-----------------------

==============  =============  ===============
    UFUNC                  MODE
--------------  ------------------------------
    name         object mode    nopython mode
==============  =============  ===============
 sin                 Yes          Yes
 cos                 Yes          Yes
 tan                 Yes          Yes
 arcsin              Yes          Yes
 arccos              Yes          Yes
 arctan              Yes          Yes
 arctan2             Yes          Yes
 hypot               Yes          Yes
 sinh                Yes          Yes
 cosh                Yes          Yes
 tanh                Yes          Yes
 arcsinh             Yes          Yes
 arccosh             Yes          Yes
 arctanh             Yes          Yes
 deg2rad             Yes          Yes
 rad2deg             Yes          Yes
 degrees             Yes          Yes
 radians             Yes          Yes
==============  =============  ===============


Bit-twiddling functions
-----------------------

==============  =============  ===============
    UFUNC                  MODE
--------------  ------------------------------
    name         object mode    nopython mode
==============  =============  ===============
 bitwise_and         Yes          Yes
 bitwise_or          Yes          Yes
 bitwise_xor         Yes          Yes
 bitwise_not         Yes          Yes
 invert              Yes          Yes
 left_shift          Yes          Yes
 right_shift         Yes          Yes
==============  =============  ===============


Comparison functions
--------------------

==============  =============  ===============
    UFUNC                  MODE
--------------  ------------------------------
    name         object mode    nopython mode
==============  =============  ===============
 greater             Yes          Yes
 greater_equal       Yes          Yes
 less                Yes          Yes
 less_equal          Yes          Yes
 not_equal           Yes          Yes
 equal               Yes          Yes
 logical_and         Yes          Yes
 logical_or          Yes          Yes
 logical_xor         Yes          Yes
 logical_not         Yes          Yes
 maximum             Yes          Yes
 minimum             Yes          Yes
 fmax                Yes          Yes
 fmin                Yes          Yes
==============  =============  ===============


Floating functions
------------------

==============  =============  ===============
    UFUNC                  MODE
--------------  ------------------------------
    name         object mode    nopython mode
==============  =============  ===============
 isfinite            Yes          Yes
 isinf               Yes          Yes
 isnan               Yes          Yes
 signbit             Yes          Yes
 copysign            Yes          Yes
 nextafter           Yes          Yes
 modf                Yes          No
 ldexp               Yes (*)      Yes
 frexp               Yes          No
 floor               Yes          Yes
 ceil                Yes          Yes
 trunc               Yes          Yes
 spacing             Yes          Yes
==============  =============  ===============

(\*) not supported on windows 32 bit
