from __future__ import print_function, division, absolute_import

import contextlib
import pickle

from llvmlite import ir
import llvmlite.binding as ll
from llvmlite.llvmpy.core import Type, Constant, LLVMException
import llvmlite.llvmpy.core as lc

from numba.config import PYVERSION
import numba.ctypes_support as ctypes
from numba import types, utils, cgutils, _helperlib, assume


class NativeValue(object):
    """
    Encapsulate the result of converting a Python object to a native value,
    recording whether the conversion was successful and how to cleanup.
    """

    def __init__(self, value, is_error=None, cleanup=None):
        self.value = value
        self.is_error = is_error if is_error is not None else cgutils.false_bit
        self.cleanup = cleanup


class PythonAPI(object):
    """
    Code generation facilities to call into the CPython C API (and related
    helpers).
    """

    def __init__(self, context, builder):
        """
        Note: Maybe called multiple times when lowering a function
        """
        self.context = context
        self.builder = builder

        self.module = builder.basic_block.function.module
        # A unique mapping of serialized objects in this module
        try:
            self.module.__serialized
        except AttributeError:
            self.module.__serialized = {}

        # Initialize types
        self.pyobj = self.context.get_argument_type(types.pyobject)
        self.voidptr = Type.pointer(Type.int(8))
        self.long = Type.int(ctypes.sizeof(ctypes.c_long) * 8)
        self.ulonglong = Type.int(ctypes.sizeof(ctypes.c_ulonglong) * 8)
        self.longlong = self.ulonglong
        self.double = Type.double()
        self.py_ssize_t = self.context.get_value_type(types.intp)
        self.cstring = Type.pointer(Type.int(8))
        self.gil_state = Type.int(_helperlib.py_gil_state_size * 8)
        self.py_buffer_t = ir.ArrayType(ir.IntType(8), _helperlib.py_buffer_size)

    # ------ Python API -----

    #
    # Basic object API
    #

    def incref(self, obj):
        fnty = Type.function(Type.void(), [self.pyobj])
        fn = self._get_function(fnty, name="Py_IncRef")
        self.builder.call(fn, [obj])

    def decref(self, obj):
        fnty = Type.function(Type.void(), [self.pyobj])
        fn = self._get_function(fnty, name="Py_DecRef")
        self.builder.call(fn, [obj])

    #
    # Argument unpacking
    #

    def parse_tuple_and_keywords(self, args, kws, fmt, keywords, *objs):
        charptr = Type.pointer(Type.int(8))
        charptrary = Type.pointer(charptr)
        argtypes = [self.pyobj, self.pyobj, charptr, charptrary]
        fnty = Type.function(Type.int(), argtypes, var_arg=True)
        fn = self._get_function(fnty, name="PyArg_ParseTupleAndKeywords")
        return self.builder.call(fn, [args, kws, fmt, keywords] + list(objs))

    def parse_tuple(self, args, fmt, *objs):
        charptr = Type.pointer(Type.int(8))
        argtypes = [self.pyobj, charptr]
        fnty = Type.function(Type.int(), argtypes, var_arg=True)
        fn = self._get_function(fnty, name="PyArg_ParseTuple")
        return self.builder.call(fn, [args, fmt] + list(objs))

    #
    # Exception and errors
    #

    def err_occurred(self):
        fnty = Type.function(self.pyobj, ())
        fn = self._get_function(fnty, name="PyErr_Occurred")
        return self.builder.call(fn, ())

    def err_clear(self):
        fnty = Type.function(Type.void(), ())
        fn = self._get_function(fnty, name="PyErr_Clear")
        return self.builder.call(fn, ())

    def err_set_string(self, exctype, msg):
        fnty = Type.function(Type.void(), [self.pyobj, self.cstring])
        fn = self._get_function(fnty, name="PyErr_SetString")
        if isinstance(exctype, str):
            exctype = self.get_c_object(exctype)
        if isinstance(msg, str):
            msg = self.context.insert_const_string(self.module, msg)
        return self.builder.call(fn, (exctype, msg))

    def raise_object(self, exc=None):
        """
        Raise an arbitrary exception (type or value or (type, args)
        or None - if reraising).  A reference to the argument is consumed.
        """
        fnty = Type.function(Type.void(), [self.pyobj])
        fn = self._get_function(fnty, name="numba_do_raise")
        if exc is None:
            exc = self.get_null_object()
        return self.builder.call(fn, (exc,))

    def err_set_object(self, exctype, excval):
        fnty = Type.function(Type.void(), [self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PyErr_SetObject")
        if isinstance(exctype, str):
            exctype = self.get_c_object(exctype)
        return self.builder.call(fn, (exctype, excval))

    def err_set_none(self, exctype):
        fnty = Type.function(Type.void(), [self.pyobj])
        fn = self._get_function(fnty, name="PyErr_SetNone")
        if isinstance(exctype, str):
            exctype = self.get_c_object(exctype)
        return self.builder.call(fn, (exctype,))

    def err_write_unraisable(self, obj):
        fnty = Type.function(Type.void(), [self.pyobj])
        fn = self._get_function(fnty, name="PyErr_WriteUnraisable")
        return self.builder.call(fn, (obj,))

    def get_c_object(self, name):
        """
        Get a Python object through its C-accessible *name*
        (e.g. "PyExc_ValueError").  The underlying variable must be
        a `PyObject *`, and the value of that pointer is returned.
        """
        # A LLVM global variable is implicitly a pointer to the declared
        # type, so fix up by using pyobj.pointee.
        return self.context.get_c_value(self.builder, self.pyobj.pointee, name)

    def raise_missing_global_error(self, name):
        msg = "global name '%s' is not defined" % name
        cstr = self.context.insert_const_string(self.module, msg)
        self.err_set_string("PyExc_NameError", cstr)

    def raise_missing_name_error(self, name):
        msg = "name '%s' is not defined" % name
        cstr = self.context.insert_const_string(self.module, msg)
        self.err_set_string("PyExc_NameError", cstr)

    def fatal_error(self, msg):
        fnty = Type.function(Type.void(), [self.cstring])
        fn = self._get_function(fnty, name="Py_FatalError")
        cstr = self.context.insert_const_string(self.module, msg)
        self.builder.call(fn, (cstr,))

    #
    # Concrete dict API
    #

    def dict_getitem_string(self, dic, name):
        """Lookup name inside dict

        Returns a borrowed reference
        """
        fnty = Type.function(self.pyobj, [self.pyobj, self.cstring])
        fn = self._get_function(fnty, name="PyDict_GetItemString")
        cstr = self.context.insert_const_string(self.module, name)
        return self.builder.call(fn, [dic, cstr])

    def dict_getitem(self, dic, name):
        """Lookup name inside dict

        Returns a borrowed reference
        """
        fnty = Type.function(self.pyobj, [self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PyDict_GetItem")
        return self.builder.call(fn, [dic, name])

    def dict_new(self, presize=0):
        if presize == 0:
            fnty = Type.function(self.pyobj, ())
            fn = self._get_function(fnty, name="PyDict_New")
            return self.builder.call(fn, ())
        else:
            fnty = Type.function(self.pyobj, [self.py_ssize_t])
            fn = self._get_function(fnty, name="_PyDict_NewPresized")
            return self.builder.call(fn,
                                     [Constant.int(self.py_ssize_t, presize)])

    def dict_setitem(self, dictobj, nameobj, valobj):
        fnty = Type.function(Type.int(), (self.pyobj, self.pyobj,
                                          self.pyobj))
        fn = self._get_function(fnty, name="PyDict_SetItem")
        return self.builder.call(fn, (dictobj, nameobj, valobj))

    def dict_setitem_string(self, dictobj, name, valobj):
        fnty = Type.function(Type.int(), (self.pyobj, self.cstring,
                                          self.pyobj))
        fn = self._get_function(fnty, name="PyDict_SetItemString")
        cstr = self.context.insert_const_string(self.module, name)
        return self.builder.call(fn, (dictobj, cstr, valobj))

    def dict_pack(self, keyvalues):
        """
        Args
        -----
        keyvalues: iterable of (str, llvm.Value of PyObject*)
        """
        dictobj = self.dict_new()
        with self.if_object_ok(dictobj):
            for k, v in keyvalues:
                self.dict_setitem_string(dictobj, k, v)
        return dictobj

    #
    # Concrete number APIs
    #

    def float_from_double(self, fval):
        fnty = Type.function(self.pyobj, [self.double])
        fn = self._get_function(fnty, name="PyFloat_FromDouble")
        return self.builder.call(fn, [fval])

    def number_as_ssize_t(self, numobj):
        fnty = Type.function(self.py_ssize_t, [self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PyNumber_AsSsize_t")
        # We don't want any clipping, so pass OverflowError as the 2nd arg
        exc_class = self.get_c_object("PyExc_OverflowError")
        return self.builder.call(fn, [numobj, exc_class])

    def number_long(self, numobj):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PyNumber_Long")
        return self.builder.call(fn, [numobj])

    def long_as_ulonglong(self, numobj):
        fnty = Type.function(self.ulonglong, [self.pyobj])
        fn = self._get_function(fnty, name="PyLong_AsUnsignedLongLong")
        return self.builder.call(fn, [numobj])

    def long_as_longlong(self, numobj):
        fnty = Type.function(self.ulonglong, [self.pyobj])
        fn = self._get_function(fnty, name="PyLong_AsLongLong")
        return self.builder.call(fn, [numobj])

    def long_as_voidptr(self, numobj):
        """
        Convert the given Python integer to a void*.  This is recommended
        over number_as_ssize_t as it isn't affected by signedness.
        """
        fnty = Type.function(self.voidptr, [self.pyobj])
        fn = self._get_function(fnty, name="PyLong_AsVoidPtr")
        return self.builder.call(fn, [numobj])

    def _long_from_native_int(self, ival, func_name, native_int_type,
                              signed):
        fnty = Type.function(self.pyobj, [native_int_type])
        fn = self._get_function(fnty, name=func_name)
        resptr = cgutils.alloca_once(self.builder, self.pyobj)

        if PYVERSION < (3, 0):
            # Under Python 2, we try to return a PyInt object whenever
            # the given number fits in a C long.
            pyint_fnty = Type.function(self.pyobj, [self.long])
            pyint_fn = self._get_function(pyint_fnty, name="PyInt_FromLong")
            long_max = Constant.int(native_int_type, _helperlib.long_max)
            if signed:
                long_min = Constant.int(native_int_type, _helperlib.long_min)
                use_pyint = self.builder.and_(
                    self.builder.icmp(lc.ICMP_SGE, ival, long_min),
                    self.builder.icmp(lc.ICMP_SLE, ival, long_max),
                    )
            else:
                use_pyint = self.builder.icmp(lc.ICMP_ULE, ival, long_max)

            with cgutils.ifelse(self.builder, use_pyint) as (then, otherwise):
                with then:
                    downcast_ival = self.builder.trunc(ival, self.long)
                    res = self.builder.call(pyint_fn, [downcast_ival])
                    self.builder.store(res, resptr)
                with otherwise:
                    res = self.builder.call(fn, [ival])
                    self.builder.store(res, resptr)
        else:
            fn = self._get_function(fnty, name=func_name)
            self.builder.store(self.builder.call(fn, [ival]), resptr)

        return self.builder.load(resptr)

    def long_from_long(self, ival):
        if PYVERSION < (3, 0):
            func_name = "PyInt_FromLong"
        else:
            func_name = "PyLong_FromLong"
        fnty = Type.function(self.pyobj, [self.long])
        fn = self._get_function(fnty, name=func_name)
        return self.builder.call(fn, [ival])

    def long_from_ssize_t(self, ival):
        return self._long_from_native_int(ival, "PyLong_FromSsize_t",
                                          self.py_ssize_t, signed=True)

    def long_from_longlong(self, ival):
        return self._long_from_native_int(ival, "PyLong_FromLongLong",
                                          self.longlong, signed=True)

    def long_from_ulonglong(self, ival):
        return self._long_from_native_int(ival, "PyLong_FromUnsignedLongLong",
                                          self.ulonglong, signed=False)

    def _get_number_operator(self, name):
        fnty = Type.function(self.pyobj, [self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PyNumber_%s" % name)
        return fn

    def _call_number_operator(self, name, lhs, rhs, inplace=False):
        if inplace:
            name = "InPlace" + name
        fn = self._get_number_operator(name)
        return self.builder.call(fn, [lhs, rhs])

    def number_add(self, lhs, rhs, inplace=False):
        return self._call_number_operator("Add", lhs, rhs, inplace=inplace)

    def number_subtract(self, lhs, rhs, inplace=False):
        return self._call_number_operator("Subtract", lhs, rhs, inplace=inplace)

    def number_multiply(self, lhs, rhs, inplace=False):
        return self._call_number_operator("Multiply", lhs, rhs, inplace=inplace)

    def number_divide(self, lhs, rhs, inplace=False):
        assert PYVERSION < (3, 0)
        return self._call_number_operator("Divide", lhs, rhs, inplace=inplace)

    def number_truedivide(self, lhs, rhs, inplace=False):
        return self._call_number_operator("TrueDivide", lhs, rhs, inplace=inplace)

    def number_floordivide(self, lhs, rhs, inplace=False):
        return self._call_number_operator("FloorDivide", lhs, rhs, inplace=inplace)

    def number_remainder(self, lhs, rhs, inplace=False):
        return self._call_number_operator("Remainder", lhs, rhs, inplace=inplace)

    def number_lshift(self, lhs, rhs, inplace=False):
        return self._call_number_operator("Lshift", lhs, rhs, inplace=inplace)

    def number_rshift(self, lhs, rhs, inplace=False):
        return self._call_number_operator("Rshift", lhs, rhs, inplace=inplace)

    def number_and(self, lhs, rhs, inplace=False):
        return self._call_number_operator("And", lhs, rhs, inplace=inplace)

    def number_or(self, lhs, rhs, inplace=False):
        return self._call_number_operator("Or", lhs, rhs, inplace=inplace)

    def number_xor(self, lhs, rhs, inplace=False):
        return self._call_number_operator("Xor", lhs, rhs, inplace=inplace)

    def number_power(self, lhs, rhs, inplace=False):
        fnty = Type.function(self.pyobj, [self.pyobj] * 3)
        fname = "PyNumber_InPlacePower" if inplace else "PyNumber_Power"
        fn = self._get_function(fnty, fname)
        return self.builder.call(fn, [lhs, rhs, self.borrow_none()])

    def number_negative(self, obj):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PyNumber_Negative")
        return self.builder.call(fn, (obj,))

    def number_positive(self, obj):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PyNumber_Positive")
        return self.builder.call(fn, (obj,))

    def number_float(self, val):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PyNumber_Float")
        return self.builder.call(fn, [val])

    def number_invert(self, obj):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PyNumber_Invert")
        return self.builder.call(fn, (obj,))

    def float_as_double(self, fobj):
        fnty = Type.function(self.double, [self.pyobj])
        fn = self._get_function(fnty, name="PyFloat_AsDouble")
        return self.builder.call(fn, [fobj])

    def bool_from_bool(self, bval):
        """
        Get a Python bool from a LLVM boolean.
        """
        longval = self.builder.zext(bval, self.long)
        return self.bool_from_long(longval)

    def bool_from_long(self, ival):
        fnty = Type.function(self.pyobj, [self.long])
        fn = self._get_function(fnty, name="PyBool_FromLong")
        return self.builder.call(fn, [ival])

    def complex_from_doubles(self, realval, imagval):
        fnty = Type.function(self.pyobj, [Type.double(), Type.double()])
        fn = self._get_function(fnty, name="PyComplex_FromDoubles")
        return self.builder.call(fn, [realval, imagval])

    def complex_real_as_double(self, cobj):
        fnty = Type.function(Type.double(), [self.pyobj])
        fn = self._get_function(fnty, name="PyComplex_RealAsDouble")
        return self.builder.call(fn, [cobj])

    def complex_imag_as_double(self, cobj):
        fnty = Type.function(Type.double(), [self.pyobj])
        fn = self._get_function(fnty, name="PyComplex_ImagAsDouble")
        return self.builder.call(fn, [cobj])

    #
    # List and sequence APIs
    #

    def sequence_getslice(self, obj, start, stop):
        fnty = Type.function(self.pyobj, [self.pyobj, self.py_ssize_t,
                                          self.py_ssize_t])
        fn = self._get_function(fnty, name="PySequence_GetSlice")
        return self.builder.call(fn, (obj, start, stop))

    def sequence_tuple(self, obj):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PySequence_Tuple")
        return self.builder.call(fn, [obj])

    def list_new(self, szval):
        fnty = Type.function(self.pyobj, [self.py_ssize_t])
        fn = self._get_function(fnty, name="PyList_New")
        return self.builder.call(fn, [szval])

    def list_setitem(self, seq, idx, val):
        """
        Warning: Steals reference to ``val``
        """
        fnty = Type.function(Type.int(), [self.pyobj, self.py_ssize_t,
                                          self.pyobj])
        fn = self._get_function(fnty, name="PyList_SetItem")
        return self.builder.call(fn, [seq, idx, val])

    def list_getitem(self, lst, idx):
        """
        Returns a borrowed reference.
        """
        fnty = Type.function(self.pyobj, [self.pyobj, self.py_ssize_t])
        fn = self._get_function(fnty, name="PyList_GetItem")
        if isinstance(idx, int):
            idx = self.context.get_constant(types.intp, idx)
        return self.builder.call(fn, [lst, idx])

    #
    # Concrete tuple API
    #

    def tuple_getitem(self, tup, idx):
        """
        Borrow reference
        """
        fnty = Type.function(self.pyobj, [self.pyobj, self.py_ssize_t])
        fn = self._get_function(fnty, name="PyTuple_GetItem")
        idx = self.context.get_constant(types.intp, idx)
        return self.builder.call(fn, [tup, idx])

    def tuple_pack(self, items):
        fnty = Type.function(self.pyobj, [self.py_ssize_t], var_arg=True)
        fn = self._get_function(fnty, name="PyTuple_Pack")
        n = self.context.get_constant(types.intp, len(items))
        args = [n]
        args.extend(items)
        return self.builder.call(fn, args)

    def tuple_size(self, tup):
        fnty = Type.function(self.py_ssize_t, [self.pyobj])
        fn = self._get_function(fnty, name="PyTuple_Size")
        return self.builder.call(fn, [tup])

    def tuple_new(self, count):
        fnty = Type.function(self.pyobj, [Type.int()])
        fn = self._get_function(fnty, name='PyTuple_New')
        return self.builder.call(fn, [self.context.get_constant(types.int32,
                                                                count)])

    def tuple_setitem(self, tuple_val, index, item):
        """
        Steals a reference to `item`.
        """
        fnty = Type.function(Type.int(), [self.pyobj, Type.int(), self.pyobj])
        setitem_fn = self._get_function(fnty, name='PyTuple_SetItem')
        index = self.context.get_constant(types.int32, index)
        self.builder.call(setitem_fn, [tuple_val, index, item])

    #
    # Concrete set API
    #

    def set_new(self, iterable=None):
        if iterable is None:
            iterable = self.get_null_object()
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PySet_New")
        return self.builder.call(fn, [iterable])

    def set_add(self, set, value):
        fnty = Type.function(Type.int(), [self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PySet_Add")
        return self.builder.call(fn, [set, value])

    #
    # GIL APIs
    #

    def gil_ensure(self):
        """
        Ensure the GIL is acquired.
        The returned value must be consumed by gil_release().
        """
        gilptrty = Type.pointer(self.gil_state)
        fnty = Type.function(Type.void(), [gilptrty])
        fn = self._get_function(fnty, "numba_gil_ensure")
        gilptr = cgutils.alloca_once(self.builder, self.gil_state)
        self.builder.call(fn, [gilptr])
        return gilptr

    def gil_release(self, gil):
        """
        Release the acquired GIL by gil_ensure().
        Must be paired with a gil_ensure().
        """
        gilptrty = Type.pointer(self.gil_state)
        fnty = Type.function(Type.void(), [gilptrty])
        fn = self._get_function(fnty, "numba_gil_release")
        return self.builder.call(fn, [gil])

    def save_thread(self):
        """
        Release the GIL and return the former thread state
        (an opaque non-NULL pointer).
        """
        fnty = Type.function(self.voidptr, [])
        fn = self._get_function(fnty, name="PyEval_SaveThread")
        return self.builder.call(fn, [])

    def restore_thread(self, thread_state):
        """
        Restore the given thread state by reacquiring the GIL.
        """
        fnty = Type.function(Type.void(), [self.voidptr])
        fn = self._get_function(fnty, name="PyEval_RestoreThread")
        self.builder.call(fn, [thread_state])


    #
    # Other APIs (organize them better!)
    #

    def import_module_noblock(self, modname):
        fnty = Type.function(self.pyobj, [self.cstring])
        fn = self._get_function(fnty, name="PyImport_ImportModuleNoBlock")
        return self.builder.call(fn, [modname])

    def call_function_objargs(self, callee, objargs):
        fnty = Type.function(self.pyobj, [self.pyobj], var_arg=True)
        fn = self._get_function(fnty, name="PyObject_CallFunctionObjArgs")
        args = [callee] + list(objargs)
        args.append(self.context.get_constant_null(types.pyobject))
        return self.builder.call(fn, args)

    def call(self, callee, args=None, kws=None):
        if args is None:
            args = self.get_null_object()
        if kws is None:
            kws = self.get_null_object()
        fnty = Type.function(self.pyobj, [self.pyobj] * 3)
        fn = self._get_function(fnty, name="PyObject_Call")
        return self.builder.call(fn, (callee, args, kws))

    def object_istrue(self, obj):
        fnty = Type.function(Type.int(), [self.pyobj])
        fn = self._get_function(fnty, name="PyObject_IsTrue")
        return self.builder.call(fn, [obj])

    def object_not(self, obj):
        fnty = Type.function(Type.int(), [self.pyobj])
        fn = self._get_function(fnty, name="PyObject_Not")
        return self.builder.call(fn, [obj])

    def object_richcompare(self, lhs, rhs, opstr):
        """
        Refer to Python source Include/object.h for macros definition
        of the opid.
        """
        ops = ['<', '<=', '==', '!=', '>', '>=']
        if opstr in ops:
            opid = ops.index(opstr)
            fnty = Type.function(self.pyobj, [self.pyobj, self.pyobj, Type.int()])
            fn = self._get_function(fnty, name="PyObject_RichCompare")
            lopid = self.context.get_constant(types.int32, opid)
            return self.builder.call(fn, (lhs, rhs, lopid))
        elif opstr == 'is':
            bitflag = self.builder.icmp(lc.ICMP_EQ, lhs, rhs)
            return self.from_native_value(bitflag, types.boolean)
        elif opstr == 'is not':
            bitflag = self.builder.icmp(lc.ICMP_NE, lhs, rhs)
            return self.from_native_value(bitflag, types.boolean)
        else:
            raise NotImplementedError("Unknown operator {op!r}".format(
                op=opstr))

    def iter_next(self, iterobj):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PyIter_Next")
        return self.builder.call(fn, [iterobj])

    def object_getiter(self, obj):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PyObject_GetIter")
        return self.builder.call(fn, [obj])

    def object_getattr_string(self, obj, attr):
        cstr = self.context.insert_const_string(self.module, attr)
        fnty = Type.function(self.pyobj, [self.pyobj, self.cstring])
        fn = self._get_function(fnty, name="PyObject_GetAttrString")
        return self.builder.call(fn, [obj, cstr])

    def object_getattr(self, obj, attr):
        fnty = Type.function(self.pyobj, [self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PyObject_GetAttr")
        return self.builder.call(fn, [obj, attr])

    def object_setattr_string(self, obj, attr, val):
        cstr = self.context.insert_const_string(self.module, attr)
        fnty = Type.function(Type.int(), [self.pyobj, self.cstring, self.pyobj])
        fn = self._get_function(fnty, name="PyObject_SetAttrString")
        return self.builder.call(fn, [obj, cstr, val])

    def object_setattr(self, obj, attr, val):
        fnty = Type.function(Type.int(), [self.pyobj, self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PyObject_SetAttr")
        return self.builder.call(fn, [obj, attr, val])

    def object_delattr_string(self, obj, attr):
        # PyObject_DelAttrString() is actually a C macro calling
        # PyObject_SetAttrString() with value == NULL.
        return self.object_setattr_string(obj, attr, self.get_null_object())

    def object_delattr(self, obj, attr):
        # PyObject_DelAttr() is actually a C macro calling
        # PyObject_SetAttr() with value == NULL.
        return self.object_setattr(obj, attr, self.get_null_object())

    def object_getitem(self, obj, key):
        fnty = Type.function(self.pyobj, [self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PyObject_GetItem")
        return self.builder.call(fn, (obj, key))

    def object_setitem(self, obj, key, val):
        fnty = Type.function(Type.int(), [self.pyobj, self.pyobj, self.pyobj])
        fn = self._get_function(fnty, name="PyObject_SetItem")
        return self.builder.call(fn, (obj, key, val))

    def string_as_string(self, strobj):
        fnty = Type.function(self.cstring, [self.pyobj])
        if PYVERSION >= (3, 0):
            fname = "PyUnicode_AsUTF8"
        else:
            fname = "PyString_AsString"
        fn = self._get_function(fnty, name=fname)
        return self.builder.call(fn, [strobj])

    def string_from_string_and_size(self, string, size):
        fnty = Type.function(self.pyobj, [self.cstring, self.py_ssize_t])
        if PYVERSION >= (3, 0):
            fname = "PyUnicode_FromStringAndSize"
        else:
            fname = "PyString_FromStringAndSize"
        fn = self._get_function(fnty, name=fname)
        return self.builder.call(fn, [string, size])

    def string_from_string(self, string):
        fnty = Type.function(self.pyobj, [self.cstring])
        if PYVERSION >= (3, 0):
            fname = "PyUnicode_FromString"
        else:
            fname = "PyString_FromString"
        fn = self._get_function(fnty, name=fname)
        return self.builder.call(fn, [string])

    def bytes_from_string_and_size(self, string, size):
        fnty = Type.function(self.pyobj, [self.cstring, self.py_ssize_t])
        if PYVERSION >= (3, 0):
            fname = "PyBytes_FromStringAndSize"
        else:
            fname = "PyString_FromStringAndSize"
        fn = self._get_function(fnty, name=fname)
        return self.builder.call(fn, [string, size])

    def object_str(self, obj):
        fnty = Type.function(self.pyobj, [self.pyobj])
        fn = self._get_function(fnty, name="PyObject_Str")
        return self.builder.call(fn, [obj])

    def make_none(self):
        obj = self.get_c_object("Py_None")
        self.incref(obj)
        return obj

    def borrow_none(self):
        obj = self.get_c_object("Py_None")
        return obj

    def sys_write_stdout(self, fmt, *args):
        fnty = Type.function(Type.void(), [self.cstring], var_arg=True)
        fn = self._get_function(fnty, name="PySys_WriteStdout")
        return self.builder.call(fn, (fmt,) + args)

    def object_dump(self, obj):
        """
        Dump a Python object on C stderr.  For debugging purposes.
        """
        fnty = Type.function(Type.void(), [self.pyobj])
        fn = self._get_function(fnty, name="_PyObject_Dump")
        return self.builder.call(fn, (obj,))

    # ------ utils -----

    def _get_function(self, fnty, name):
        return self.module.get_or_insert_function(fnty, name=name)

    def alloca_obj(self):
        return self.builder.alloca(self.pyobj)

    def alloca_buffer(self):
        """
        Return a pointer to a stack-allocated, zero-initialized Py_buffer.
        """
        # Treat the buffer as an opaque array of bytes
        ptr = cgutils.alloca_once_value(self.builder,
                                        lc.Constant.null(self.py_buffer_t))
        return ptr

    @contextlib.contextmanager
    def if_object_ok(self, obj):
        with cgutils.if_likely(self.builder,
                               cgutils.is_not_null(self.builder, obj)):
            yield

    def print_object(self, obj):
        strobj = self.object_str(obj)
        cstr = self.string_as_string(strobj)
        fmt = self.context.insert_const_string(self.module, "%s")
        self.sys_write_stdout(fmt, cstr)
        self.decref(strobj)

    def print_string(self, text):
        fmt = self.context.insert_const_string(self.module, text)
        self.sys_write_stdout(fmt)

    def get_null_object(self):
        return Constant.null(self.pyobj)

    def return_none(self):
        none = self.make_none()
        self.builder.ret(none)

    def list_pack(self, items):
        n = len(items)
        seq = self.list_new(self.context.get_constant(types.intp, n))
        with self.if_object_ok(seq):
            for i in range(n):
                idx = self.context.get_constant(types.intp, i)
                self.incref(items[i])
                self.list_setitem(seq, idx, items[i])
        return seq

    def unserialize(self, structptr):
        """
        Unserialize some data.  *structptr* should be a pointer to
        a {i8* data, i32 length} structure.
        """
        fnty = Type.function(self.pyobj, (self.voidptr, ir.IntType(32)))
        fn = self._get_function(fnty, name="numba_unpickle")
        ptr = self.builder.extract_value(self.builder.load(structptr), 0)
        n = self.builder.extract_value(self.builder.load(structptr), 1)
        return self.builder.call(fn, (ptr, n))

    def serialize_object(self, obj):
        """
        Serialize the given object in the bitcode, and return it
        as a pointer to a {i8* data, i32 length}, structure constant
        (suitable for passing to unserialize()).
        """
        try:
            gv = self.module.__serialized[obj]
        except KeyError:
            # First make the array constant
            data = pickle.dumps(obj, protocol=-1)
            name = ".const.pickledata.%s" % (id(obj))
            bdata = cgutils.make_bytearray(data)
            arr = self.context.insert_unique_const(self.module, name, bdata)
            # Then populate the structure constant
            struct = ir.Constant.literal_struct([
                arr.bitcast(self.voidptr),
                ir.Constant(ir.IntType(32), arr.type.pointee.count),
                ])
            name = ".const.picklebuf.%s" % (id(obj))
            gv = self.context.insert_unique_const(self.module, name, struct)
            # Make the id() (and hence the name) unique while populating the module.
            self.module.__serialized[obj] = gv
        return gv

    def to_native_value(self, obj, typ):
        builder = self.builder
        def c_api_error():
            return cgutils.is_not_null(builder, self.err_occurred())

        if isinstance(typ, types.Object) or typ == types.pyobject:
            return NativeValue(obj)

        elif typ == types.boolean:
            istrue = self.object_istrue(obj)
            zero = Constant.null(istrue.type)
            val = builder.icmp(lc.ICMP_NE, istrue, zero)
            return NativeValue(val, is_error=c_api_error())

        elif isinstance(typ, types.Integer):
            val = self.to_native_int(obj, typ)
            return NativeValue(val, is_error=c_api_error())

        elif typ == types.float32:
            fobj = self.number_float(obj)
            fval = self.float_as_double(fobj)
            self.decref(fobj)
            val = builder.fptrunc(fval,
                                  self.context.get_argument_type(typ))
            return NativeValue(val, is_error=c_api_error())

        elif typ == types.float64:
            fobj = self.number_float(obj)
            val = self.float_as_double(fobj)
            self.decref(fobj)
            return NativeValue(val, is_error=c_api_error())

        elif typ in (types.complex128, types.complex64):
            cplxcls = self.context.make_complex(types.complex128)
            cplx = cplxcls(self.context, builder)
            pcplx = cplx._getpointer()
            ok = self.complex_adaptor(obj, pcplx)
            failed = cgutils.is_false(builder, ok)

            with cgutils.if_unlikely(builder, failed):
                self.err_set_string("PyExc_TypeError",
                                    "conversion to %s failed" % (typ,))

            if typ == types.complex64:
                c64cls = self.context.make_complex(typ)
                c64 = c64cls(self.context, builder)
                freal = self.context.cast(builder, cplx.real,
                                          types.float64, types.float32)
                fimag = self.context.cast(builder, cplx.imag,
                                          types.float64, types.float32)
                c64.real = freal
                c64.imag = fimag
                return NativeValue(c64._getvalue(), is_error=failed)
            else:
                return NativeValue(cplx._getvalue(), is_error=failed)

        elif isinstance(typ, types.NPDatetime):
            val = self.extract_np_datetime(obj)
            return NativeValue(val, is_error=c_api_error())

        elif isinstance(typ, types.NPTimedelta):
            val = self.extract_np_timedelta(obj)
            return NativeValue(val, is_error=c_api_error())

        elif isinstance(typ, types.Record):
            buf = self.alloca_buffer()
            ptr = self.extract_record_data(obj, buf)
            is_error = cgutils.is_null(self.builder, ptr)

            ltyp = self.context.get_value_type(typ)
            val = builder.bitcast(ptr, ltyp)

            def cleanup():
                self.release_buffer(buf)
            return NativeValue(val, cleanup=cleanup, is_error=is_error)

        elif isinstance(typ, types.Array):
            val, failed = self.to_native_array(obj, typ)
            return NativeValue(val, is_error=failed)

        elif isinstance(typ, types.Buffer):
            return self.to_native_buffer(obj, typ)

        elif isinstance(typ, types.Optional):
            return self.to_native_optional(obj, typ)

        elif isinstance(typ, (types.Tuple, types.UniTuple)):
            return self.to_native_tuple(obj, typ)

        elif isinstance(typ, types.Generator):
            return self.to_native_generator(obj, typ)

        elif isinstance(typ, types.ExternalFunctionPointer):
            if typ.get_pointer is not None:
                # Call get_pointer() on the object to get the raw pointer value
                ptrty = self.context.get_function_pointer_type(typ)
                ret = cgutils.alloca_once_value(builder,
                                                Constant.null(ptrty),
                                                name='fnptr')
                ser = self.serialize_object(typ.get_pointer)
                get_pointer = self.unserialize(ser)
                with cgutils.if_likely(builder,
                                       cgutils.is_not_null(builder, get_pointer)):
                    intobj = self.call_function_objargs(get_pointer, (obj,))
                    self.decref(get_pointer)
                    with cgutils.if_likely(builder,
                                           cgutils.is_not_null(builder, intobj)):
                        ptr = self.long_as_voidptr(intobj)
                        self.decref(intobj)
                        builder.store(builder.bitcast(ptr, ptrty), ret)
                return NativeValue(builder.load(ret), is_error=c_api_error())

        raise NotImplementedError("cannot convert %s to native value" % (typ,))

    def from_native_return(self, val, typ):
        return self.from_native_value(val, typ)

    def from_native_value(self, val, typ):
        if typ == types.pyobject:
            return val

        elif typ == types.boolean:
            longval = self.builder.zext(val, self.long)
            return self.bool_from_long(longval)

        elif typ in types.unsigned_domain:
            ullval = self.builder.zext(val, self.ulonglong)
            return self.long_from_ulonglong(ullval)

        elif typ in types.signed_domain:
            ival = self.builder.sext(val, self.longlong)
            return self.long_from_longlong(ival)

        elif typ == types.float32:
            dbval = self.builder.fpext(val, self.double)
            return self.float_from_double(dbval)

        elif typ == types.float64:
            return self.float_from_double(val)

        elif typ == types.complex128:
            cmplxcls = self.context.make_complex(typ)
            cval = cmplxcls(self.context, self.builder, value=val)
            return self.complex_from_doubles(cval.real, cval.imag)

        elif typ == types.complex64:
            cmplxcls = self.context.make_complex(typ)
            cval = cmplxcls(self.context, self.builder, value=val)
            freal = self.context.cast(self.builder, cval.real,
                                      types.float32, types.float64)
            fimag = self.context.cast(self.builder, cval.imag,
                                      types.float32, types.float64)
            return self.complex_from_doubles(freal, fimag)

        elif isinstance(typ, types.NPDatetime):
            return self.create_np_datetime(val, typ.unit_code)

        elif isinstance(typ, types.NPTimedelta):
            return self.create_np_timedelta(val, typ.unit_code)

        elif typ == types.none:
            ret = self.make_none()
            return ret

        elif isinstance(typ, types.Optional):
            return self.from_native_return(val, typ.type)

        elif isinstance(typ, types.Array):
            return self.from_native_array(val, typ)

        elif isinstance(typ, types.Record):
            # Note we will create a copy of the record
            # This is the only safe way.
            size = Constant.int(Type.int(), val.type.pointee.count)
            ptr = self.builder.bitcast(val, Type.pointer(Type.int(8)))
            # Note: this will only work for CPU mode
            #       The following requires access to python object
            dtype_addr = Constant.int(self.py_ssize_t, id(typ.dtype))
            dtypeobj = dtype_addr.inttoptr(self.pyobj)
            return self.recreate_record(ptr, size, dtypeobj)

        elif isinstance(typ, (types.Tuple, types.UniTuple)):
            return self.from_native_tuple(val, typ)

        elif isinstance(typ, types.Generator):
            return self.from_native_generator(val, typ)

        elif isinstance(typ, types.CharSeq):
            return self.from_native_charseq(val, typ)

        raise NotImplementedError(typ)

    def to_native_int(self, obj, typ):
        ll_type = self.context.get_argument_type(typ)
        val = cgutils.alloca_once(self.builder, ll_type)
        longobj = self.number_long(obj)
        with self.if_object_ok(longobj):
            if typ.signed:
                llval = self.long_as_longlong(longobj)
            else:
                llval = self.long_as_ulonglong(longobj)
            self.decref(longobj)
            self.builder.store(self.builder.trunc(llval, ll_type), val)
        return self.builder.load(val)

    def to_native_buffer(self, obj, typ):
        buf = self.alloca_buffer()
        res = self.get_buffer(obj, buf)
        is_error = cgutils.is_not_null(self.builder, res)

        nativearycls = self.context.make_array(typ)
        nativeary = nativearycls(self.context, self.builder)
        aryptr = nativeary._getpointer()

        with cgutils.if_likely(self.builder, self.builder.not_(is_error)):
            ptr = self.builder.bitcast(aryptr, self.voidptr)
            self.numba_buffer_adaptor(buf, ptr)

        def cleanup():
            self.release_buffer(buf)

        return NativeValue(self.builder.load(aryptr), is_error=is_error,
                           cleanup=cleanup)

    def to_native_array(self, ary, typ):
        # TODO check matching dtype.
        #      currently, mismatching dtype will still work and causes
        #      potential memory corruption
        nativearycls = self.context.make_array(typ)
        nativeary = nativearycls(self.context, self.builder)
        aryptr = nativeary._getpointer()
        ptr = self.builder.bitcast(aryptr, self.voidptr)
        errcode = self.numba_array_adaptor(ary, ptr)
        failed = cgutils.is_not_null(self.builder, errcode)
        return self.builder.load(aryptr), failed

    def from_native_array(self, ary, typ):
        assert assume.return_argument_array_only
        nativearycls = self.context.make_array(typ)
        nativeary = nativearycls(self.context, self.builder, value=ary)
        parent = nativeary.parent
        self.incref(parent)
        return parent

    def to_native_optional(self, obj, typ):
        """
        Convert object *obj* to a native optional structure.
        """
        noneval = self.context.make_optional_none(self.builder, typ.type)
        is_not_none = self.builder.icmp(lc.ICMP_NE, obj, self.borrow_none())

        retptr = cgutils.alloca_once(self.builder, noneval.type)
        errptr = cgutils.alloca_once_value(self.builder, cgutils.false_bit)

        with cgutils.ifelse(self.builder, is_not_none) as (then, orelse):
            with then:
                native = self.to_native_value(obj, typ.type)
                just = self.context.make_optional_value(self.builder,
                                                        typ.type, native.value)
                self.builder.store(just, retptr)
                self.builder.store(native.is_error, errptr)

            with orelse:
                self.builder.store(ir.Constant(noneval.type, ir.Undefined),
                                   retptr)
                self.builder.store(noneval, retptr)

        if native.cleanup is not None:
            def cleanup():
                with cgutils.ifthen(self.builder, is_not_none):
                    native.cleanup()
        else:
            cleanup = None

        ret = self.builder.load(retptr)
        return NativeValue(ret, is_error=self.builder.load(errptr),
                           cleanup=cleanup)

    def to_native_tuple(self, obj, typ):
        """
        Convert tuple *obj* to a native array (if homogenous) or structure.
        """
        n = len(typ)
        values = []
        cleanups = []
        is_error = cgutils.false_bit
        for i, eltype in enumerate(typ):
            elem = self.tuple_getitem(obj, i)
            native = self.to_native_value(elem, eltype)
            values.append(native.value)
            is_error = self.builder.or_(is_error, native.is_error)
            if native.cleanup is not None:
                cleanups.append(native.cleanup)

        if cleanups:
            def cleanup():
                for func in reversed(cleanups):
                    func()
        else:
            cleanup = None

        if isinstance(typ, types.UniTuple):
            value = cgutils.pack_array(self.builder, values)
        else:
            value = cgutils.make_anonymous_struct(self.builder, values)
        return NativeValue(value, is_error=is_error, cleanup=cleanup)

    def from_native_tuple(self, val, typ):
        """
        Convert native array or structure *val* to a tuple object.
        """
        tuple_val = self.tuple_new(typ.count)

        for i, dtype in enumerate(typ):
            item = self.builder.extract_value(val, i)
            obj = self.from_native_value(item,  dtype)
            self.tuple_setitem(tuple_val, i, obj)

        return tuple_val

    def to_native_generator(self, obj, typ):
        """
        Extract the generator structure pointer from a generator *obj*
        (a _dynfunc.Generator instance).
        """
        gen_ptr_ty = Type.pointer(self.context.get_data_type(typ))
        value = self.context.get_generator_state(self.builder, obj, gen_ptr_ty)
        return NativeValue(value)

    def from_native_generator(self, val, typ, env=None):
        """
        Make a Numba generator (a _dynfunc.Generator instance) from a
        generator structure pointer *val*.
        *env* is an optional _dynfunc.Environment instance to be wrapped
        in the generator.
        """
        llty = self.context.get_data_type(typ)
        assert not llty.is_pointer
        gen_struct_size = self.context.get_abi_sizeof(llty)

        gendesc = self.context.get_generator_desc(typ)

        # This is the PyCFunctionWithKeywords generated by PyCallWrapper
        genfnty = Type.function(self.pyobj, [self.pyobj, self.pyobj, self.pyobj])
        genfn = self._get_function(genfnty, name=gendesc.llvm_cpython_wrapper_name)

        # This is the raw finalizer generated by _lower_generator_finalize_func()
        finalizerty = Type.function(Type.void(), [self.voidptr])
        if typ.has_finalizer:
            finalizer = self._get_function(finalizerty, name=gendesc.llvm_finalizer_name)
        else:
            finalizer = Constant.null(Type.pointer(finalizerty))

        # PyObject *numba_make_generator(state_size, initial_state, nextfunc, finalizer, env)
        fnty = Type.function(self.pyobj, [self.py_ssize_t,
                                          self.voidptr,
                                          Type.pointer(genfnty),
                                          Type.pointer(finalizerty),
                                          self.voidptr])
        fn = self._get_function(fnty, name="numba_make_generator")

        state_size = ir.Constant(self.py_ssize_t, gen_struct_size)
        initial_state = self.builder.bitcast(val, self.voidptr)
        if env is None:
            env = self.get_null_object()
        env = self.builder.bitcast(env, self.voidptr)

        return self.builder.call(fn,
                                 (state_size, initial_state, genfn, finalizer, env))

    def from_native_charseq(self, val, typ):
        builder = self.builder
        rawptr = cgutils.alloca_once_value(builder, value=val)
        strptr = builder.bitcast(rawptr, self.cstring)
        fullsize = self.context.get_constant(types.intp, typ.count)
        zero = self.context.get_constant(types.intp, 0)
        count = cgutils.alloca_once_value(builder, zero)

        bbend = cgutils.append_basic_block(builder, "end.string.count")

        # Find the length of the string
        with cgutils.loop_nest(builder, [fullsize], fullsize.type) as [idx]:
            # Get char at idx
            ch = builder.load(builder.gep(strptr, [idx]))
            # Store the current index as count
            builder.store(idx, count)
            # Check if the char is a null-byte
            ch_is_null = cgutils.is_null(builder, ch)
            # If the char is a null-byte
            with cgutils.ifthen(builder, ch_is_null):
                # Jump to the end
                builder.branch(bbend)

        # This is reached if there is no null-byte in the string
        # Then, set count to the fullsize
        builder.store(fullsize, count)
        # Jump to the end
        builder.branch(bbend)

        builder.position_at_end(bbend)
        strlen = builder.load(count)
        return self.bytes_from_string_and_size(strptr, strlen)

    def numba_array_adaptor(self, ary, ptr):
        fnty = Type.function(Type.int(), [self.pyobj, self.voidptr])
        fn = self._get_function(fnty, name="numba_adapt_ndarray")
        fn.args[0].add_attribute(lc.ATTR_NO_CAPTURE)
        fn.args[1].add_attribute(lc.ATTR_NO_CAPTURE)
        return self.builder.call(fn, (ary, ptr))

    def numba_buffer_adaptor(self, buf, ptr):
        fnty = Type.function(Type.void(),
                             [ir.PointerType(self.py_buffer_t), self.voidptr])
        fn = self._get_function(fnty, name="numba_adapt_buffer")
        fn.args[0].add_attribute(lc.ATTR_NO_CAPTURE)
        fn.args[1].add_attribute(lc.ATTR_NO_CAPTURE)
        return self.builder.call(fn, (buf, ptr))

    def complex_adaptor(self, cobj, cmplx):
        fnty = Type.function(Type.int(), [self.pyobj, cmplx.type])
        fn = self._get_function(fnty, name="numba_complex_adaptor")
        return self.builder.call(fn, [cobj, cmplx])

    def extract_record_data(self, obj, pbuf):
        fnty = Type.function(self.voidptr,
                             [self.pyobj, ir.PointerType(self.py_buffer_t)])
        fn = self._get_function(fnty, name="numba_extract_record_data")
        return self.builder.call(fn, [obj, pbuf])

    def get_buffer(self, obj, pbuf):
        fnty = Type.function(Type.int(),
                             [self.pyobj, ir.PointerType(self.py_buffer_t)])
        fn = self._get_function(fnty, name="numba_get_buffer")
        return self.builder.call(fn, [obj, pbuf])

    def release_buffer(self, pbuf):
        fnty = Type.function(Type.void(), [ir.PointerType(self.py_buffer_t)])
        fn = self._get_function(fnty, name="numba_release_buffer")
        return self.builder.call(fn, [pbuf])

    def extract_np_datetime(self, obj):
        fnty = Type.function(Type.int(64), [self.pyobj])
        fn = self._get_function(fnty, name="numba_extract_np_datetime")
        return self.builder.call(fn, [obj])

    def extract_np_timedelta(self, obj):
        fnty = Type.function(Type.int(64), [self.pyobj])
        fn = self._get_function(fnty, name="numba_extract_np_timedelta")
        return self.builder.call(fn, [obj])

    def create_np_datetime(self, val, unit_code):
        unit_code = Constant.int(Type.int(), unit_code)
        fnty = Type.function(self.pyobj, [Type.int(64), Type.int()])
        fn = self._get_function(fnty, name="numba_create_np_datetime")
        return self.builder.call(fn, [val, unit_code])

    def create_np_timedelta(self, val, unit_code):
        unit_code = Constant.int(Type.int(), unit_code)
        fnty = Type.function(self.pyobj, [Type.int(64), Type.int()])
        fn = self._get_function(fnty, name="numba_create_np_timedelta")
        return self.builder.call(fn, [val, unit_code])

    def recreate_record(self, pdata, size, dtypeaddr):
        fnty = Type.function(self.pyobj, [Type.pointer(Type.int(8)),
                                          Type.int(), self.pyobj])
        fn = self._get_function(fnty, name="numba_recreate_record")
        return self.builder.call(fn, [pdata, size, dtypeaddr])

    def string_from_constant_string(self, string):
        cstr = self.context.insert_const_string(self.module, string)
        sz = self.context.get_constant(types.intp, len(string))
        return self.string_from_string_and_size(cstr, sz)
